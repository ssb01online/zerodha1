"""Download queue planning: expiry-first, traded-value rank, chunk estimates."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Literal, Mapping

from kitetools import auth

from .catalog import FetchState, UnderlyingRank
from .markets import MarketProfile, NFO, get_market
from .universe import SESSION_END, Series, filter_series_by_types


def _resolve_market(market: MarketProfile | str | None) -> MarketProfile:
    if market is None:
        return NFO
    if isinstance(market, MarketProfile):
        return market
    return get_market(market)

IntervalName = Literal["minute", "day"]

BACKFILL_WINDOW_DAYS = 60
MINUTE_CHUNK_DAYS = 60
# Phase 0: day span ≥2001 days OK; use 2000 as safe inclusive chunk/backfill.
DAY_BACKFILL_DAYS = 2000
DAY_CHUNK_DAYS = 2000
# Phase 0 median historical latency ~0.19s; pace floor is 0.34s → use max(pace, median).
HIST_PACE_S = 0.34
HIST_MEDIAN_LATENCY_S = 0.19
# NFO aliases — prefer market= / profile.session_* in new call sites.
SESSION_START = NFO.session_start
# SESSION_END still imported/re-exported from universe (NFO profile).


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


def fetch_end(
    now: datetime,
    *,
    market: MarketProfile | str | None = None,
) -> datetime:
    """Last completed minute (or session_end IST once the session is over)."""
    profile = _resolve_market(market)
    now = _as_aware_ist(now)
    day = now.date()
    session_end = datetime.combine(day, profile.session_end, tzinfo=auth.IST)
    session_start = datetime.combine(day, profile.session_start, tzinfo=auth.IST)
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


def day_fetch_end(
    now: datetime,
    *,
    market: MarketProfile | str | None = None,
) -> datetime:
    """Last complete day bar end (session_end IST). Before session close, exclude today."""
    profile = _resolve_market(market)
    now = _as_aware_ist(now)
    day = now.date()
    session_end = datetime.combine(day, profile.session_end, tzinfo=auth.IST)
    if now >= session_end:
        return session_end
    prev = day - timedelta(days=1)
    return datetime.combine(prev, profile.session_end, tzinfo=auth.IST)


def interval_fetch_end(
    now: datetime,
    interval: IntervalName = "minute",
    *,
    market: MarketProfile | str | None = None,
) -> datetime:
    if interval == "day":
        return day_fetch_end(now, market=market)
    return fetch_end(now, market=market)


def partial_rollback(
    fetched_to: datetime,
    *,
    market: MarketProfile | str | None = None,
) -> datetime:
    """Roll a partial watermark back to session_start IST of that day."""
    profile = _resolve_market(market)
    fetched_to = _as_aware_ist(fetched_to)
    return datetime.combine(fetched_to.date(), profile.session_start, tzinfo=auth.IST)


def day_partial_rollback(
    fetched_to: datetime,
    *,
    market: MarketProfile | str | None = None,
) -> datetime:
    """Roll a partial day watermark back to session end of the previous calendar day.

    Re-fetch includes the watermark's calendar day (incomplete session bar).
    """
    profile = _resolve_market(market)
    fetched_to = _as_aware_ist(fetched_to)
    prev = fetched_to.date() - timedelta(days=1)
    return datetime.combine(prev, profile.session_end, tzinfo=auth.IST)


def parse_days_arg(value: str | int | None) -> int | None:
    """Parse CLI ``--days`` / ``-d``.

    Returns a positive inclusive day count, or ``None`` for ``all``.
    ``None`` input defaults to :data:`BACKFILL_WINDOW_DAYS`.
    """
    if value is None:
        return BACKFILL_WINDOW_DAYS
    if isinstance(value, int):
        if value <= 0:
            raise ValueError(f"days must be a positive integer or 'all', got {value}")
        return value
    text = str(value).strip().lower()
    if text == "all":
        return None
    try:
        n = int(text)
    except ValueError as exc:
        raise ValueError(f"days must be a positive integer or 'all', got {value!r}") from exc
    if n <= 0:
        raise ValueError(f"days must be a positive integer or 'all', got {value}")
    return n


def default_backfill_start(
    now: datetime,
    *,
    backfill_days: int | None = BACKFILL_WINDOW_DAYS,
) -> datetime:
    """Start of an inclusive minute backfill window ending at now.

    ``backfill_days=None`` (``--days all``) uses :data:`DAY_BACKFILL_DAYS` as a
    finite estimate upper bound; fetch tightens from day bars at runtime.
    """
    now = _as_aware_ist(now)
    days = DAY_BACKFILL_DAYS if backfill_days is None else backfill_days
    # Inclusive span of N days ⇒ subtract (N - 1) days.
    return now - timedelta(days=days - 1)


def default_day_backfill_start(now: datetime) -> datetime:
    """Start of a 2000-day inclusive window (full F&O life in one day request)."""
    now = _as_aware_ist(now)
    return now - timedelta(days=DAY_BACKFILL_DAYS - 1)


def chunk_days_for(interval: IntervalName) -> int:
    return DAY_CHUNK_DAYS if interval == "day" else MINUTE_CHUNK_DAYS


def contract_range_start(
    state: FetchState | None,
    *,
    now: datetime,
    interval: IntervalName = "minute",
    backfill_days: int | None = BACKFILL_WINDOW_DAYS,
    market: MarketProfile | str | None = None,
) -> datetime:
    now = _as_aware_ist(now)
    if interval == "day":
        default_start = default_day_backfill_start(now)
    else:
        default_start = default_backfill_start(now, backfill_days=backfill_days)
    if state is None or state.fetched_to is None:
        return default_start
    if state.status == "partial":
        if interval == "day":
            return day_partial_rollback(state.fetched_to, market=market)
        return partial_rollback(state.fetched_to, market=market)
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
    interval: IntervalName = "minute",
    backfill_days: int | None = BACKFILL_WINDOW_DAYS,
    market: MarketProfile | str | None = None,
) -> int:
    end = end if end is not None else interval_fetch_end(now, interval, market=market)
    if state is not None and state.status == "gave_up":
        return 0
    if state is not None and state.status == "done":
        fetched = state.fetched_to
        if fetched is not None and _as_aware_ist(fetched) >= end:
            return 0
        # done but watermark before end → treat as needing a refresh chunk
    start = contract_range_start(
        state,
        now=now,
        interval=interval,
        backfill_days=backfill_days,
        market=market,
    )
    return chunk_count(start, end, chunk_days=chunk_days_for(interval))


def series_pending_requests(
    series: Series,
    fetch_states: Mapping[int, FetchState],
    *,
    now: datetime,
    interval: IntervalName = "minute",
    backfill_days: int | None = BACKFILL_WINDOW_DAYS,
    market: MarketProfile | str | None = None,
) -> int:
    end = interval_fetch_end(now, interval, market=market)
    total = 0
    for c in series.contracts:
        total += contract_pending_requests(
            fetch_states.get(c.token),
            now=now,
            end=end,
            interval=interval,
            backfill_days=backfill_days,
            market=market,
        )
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
    interval: IntervalName = "minute",
    backfill_days: int | None = BACKFILL_WINDOW_DAYS,
    market: MarketProfile | str | None = None,
    types: str | frozenset[str] | None = None,
) -> list[QueueItem]:
    """Order: expiry asc, traded_value desc, underlying name; unranked last within expiry.

    ``types`` (``all|fut|opt``) drops non-matching contracts before estimating pending work.
    """
    now = _as_aware_ist(now)
    items: list[QueueItem] = []
    per_request = request_seconds()
    # Day interval ignores minute backfill_days.
    bf = backfill_days if interval == "minute" else BACKFILL_WINDOW_DAYS
    series = filter_series_by_types(series, types)

    for s in series:
        if expiry_filter is not None and s.expiry != expiry_filter:
            continue
        if underlying_filter is not None and s.underlying.upper() != underlying_filter.upper():
            continue
        rank = ranks.get(s.underlying)
        pending = series_pending_requests(
            s,
            fetch_states,
            now=now,
            interval=interval,
            backfill_days=bf,
            market=market,
        )
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
