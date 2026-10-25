"""Download queue planning: expiry-first, traded-value rank, 60-day chunk estimates."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Mapping

from kitetools import auth

from .catalog import FetchState, UnderlyingRank
from .universe import SESSION_END, Series

BACKFILL_WINDOW_DAYS = 60
MINUTE_CHUNK_DAYS = 60
# Phase 0 median historical latency ~0.19s; pace floor is 0.34s → use max(pace, median).
HIST_PACE_S = 0.34
HIST_MEDIAN_LATENCY_S = 0.19
SESSION_START = time(9, 15)


@dataclass(frozen=True)
class QueueItem:
    underlying: str
    expiry: date
    contracts: int
    pending_requests: int
    traded_value: float | None
    ranked: bool
    estimated_seconds: float

    @property
    def estimated_minutes(self) -> float:
        return self.estimated_seconds / 60.0


def request_seconds() -> float:
    return max(HIST_PACE_S, HIST_MEDIAN_LATENCY_S)


def _as_aware_ist(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=auth.IST)
    return dt.astimezone(auth.IST)


def fetch_end(now: datetime) -> datetime:
    """Last completed minute (or 15:40 IST once the session is over)."""
    now = _as_aware_ist(now)
    day = now.date()
    session_end = datetime.combine(day, SESSION_END, tzinfo=auth.IST)
    session_start = datetime.combine(day, SESSION_START, tzinfo=auth.IST)
    if now >= session_end:
        return session_end
    if now <= session_start:
        # Before open: previous calendar day's session end is fine for planning;
        # still clamp to now - 1 minute for safety when estimating.
        prev = now - timedelta(minutes=1)
        return prev
    # During session: only completed minutes (floor to previous minute).
    floored = now.replace(second=0, microsecond=0) - timedelta(minutes=1)
    if floored < session_start:
        return session_start
    return floored


def partial_rollback(fetched_to: datetime) -> datetime:
    """Roll a partial watermark back to 09:15 IST of that day."""
    fetched_to = _as_aware_ist(fetched_to)
    return datetime.combine(fetched_to.date(), SESSION_START, tzinfo=auth.IST)


def default_backfill_start(now: datetime) -> datetime:
    """Start of a 60-day inclusive window ending at now (fits one minute request)."""
    now = _as_aware_ist(now)
    # Inclusive span of BACKFILL_WINDOW_DAYS ⇒ subtract (N - 1) days.
    return now - timedelta(days=BACKFILL_WINDOW_DAYS - 1)


def contract_range_start(
    state: FetchState | None,
    *,
    now: datetime,
) -> datetime:
    now = _as_aware_ist(now)
    default_start = default_backfill_start(now)
    if state is None or state.fetched_to is None:
        return default_start
    if state.status == "partial":
        return partial_rollback(state.fetched_to)
    if state.status == "done":
        # Already complete for planning purposes — start == end ⇒ 0 chunks.
        return _as_aware_ist(state.fetched_to)
    # pending / error: resume from watermark if present, else full window.
    return max(_as_aware_ist(state.fetched_to), default_start)


def chunk_count(start: datetime, end: datetime, *, chunk_days: int = MINUTE_CHUNK_DAYS) -> int:
    """Number of inclusive calendar-day chunks of size chunk_days covering [start, end]."""
    start = _as_aware_ist(start)
    end = _as_aware_ist(end)
    if end <= start:
        return 0
    # Inclusive span in calendar days between start.date and end.date.
    span_days = (end.date() - start.date()).days + 1
    if span_days <= 0:
        return 0
    return (span_days + chunk_days - 1) // chunk_days


def contract_pending_requests(
    state: FetchState | None,
    *,
    now: datetime,
    end: datetime | None = None,
) -> int:
    end = end if end is not None else fetch_end(now)
    if state is not None and state.status == "gave_up":
        return 0
    if state is not None and state.status == "done":
        fetched = state.fetched_to
        if fetched is not None and _as_aware_ist(fetched) >= end:
            return 0
        # done but watermark before end → treat as needing a refresh chunk
    start = contract_range_start(state, now=now)
    return chunk_count(start, end)


def series_pending_requests(
    series: Series,
    fetch_states: Mapping[int, FetchState],
    *,
    now: datetime,
) -> int:
    end = fetch_end(now)
    total = 0
    for c in series.contracts:
        total += contract_pending_requests(fetch_states.get(c.token), now=now, end=end)
    return total


def build_queue(
    series: list[Series],
    ranks: Mapping[str, UnderlyingRank],
    fetch_states: Mapping[int, FetchState],
    now: datetime,
    *,
    expiry_filter: date | None = None,
    underlying_filter: str | None = None,
    limit: int | None = None,
) -> list[QueueItem]:
    """Order: expiry asc, traded_value desc, underlying name; unranked last within expiry."""
    now = _as_aware_ist(now)
    items: list[QueueItem] = []
    per_request = request_seconds()

    for s in series:
        if expiry_filter is not None and s.expiry != expiry_filter:
            continue
        if underlying_filter is not None and s.underlying.upper() != underlying_filter.upper():
            continue
        rank = ranks.get(s.underlying)
        pending = series_pending_requests(s, fetch_states, now=now)
        items.append(
            QueueItem(
                underlying=s.underlying,
                expiry=s.expiry,
                contracts=len(s.contracts),
                pending_requests=pending,
                traded_value=rank.traded_value if rank else None,
                ranked=rank is not None,
                estimated_seconds=pending * per_request,
            )
        )

    def sort_key(item: QueueItem) -> tuple:
        # Unranked → sort after ranked (True > False when using not ranked as first key
        # within expiry). We want ranked first, so use (not ranked).
        value = item.traded_value if item.traded_value is not None else -1.0
        return (
            item.expiry,
            0 if item.ranked else 1,
            -value,
            item.underlying,
        )

    items.sort(key=sort_key)
    if limit is not None and limit >= 0:
        items = items[:limit]
    return items
