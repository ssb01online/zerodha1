"""Offline series / snapshot status aggregation (no Kite calls)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
import duckdb

from kitetools import auth

from . import planner
from . import rank as rank_mod
from . import read as read_mod
from .catalog import (
    ContractRow,
    FetchState,
    list_contracts,
    list_fetch_states,
    list_snapshots,
)
from .fetch import contract_end
from .markets import MarketProfile
from .paths import DAY_DIR, MINUTE_DIR, sanitize_underlying, ymd
from .planner import IntervalName
from .universe import Series, resolve_market, select_series


@dataclass(frozen=True)
class SeriesStatus:
    underlying: str
    expiry: date
    live: bool
    contracts: int
    done: int
    partial: int
    error: int
    gave_up: int
    missing: int
    complete: int
    pending_requests: int
    min_fetched_to: datetime | None
    max_fetched_to: datetime | None
    file_path: Path | None
    file_bytes: int
    file_rows: int

    @property
    def complete_pct(self) -> float:
        if self.contracts <= 0:
            return 100.0
        return 100.0 * self.complete / self.contracts

    @property
    def estimated_minutes(self) -> float:
        return self.pending_requests * planner.request_seconds() / 60.0


@dataclass(frozen=True)
class StatusTotals:
    series: int
    contracts: int
    complete: int
    pending_requests: int
    estimated_minutes: float
    file_bytes: int

    @property
    def complete_pct(self) -> float:
        if self.contracts <= 0:
            return 100.0
        return 100.0 * self.complete / self.contracts


def _as_aware_ist(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=auth.IST)
    return dt.astimezone(auth.IST)


def _as_naive(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt
    return dt.astimezone(auth.IST).replace(tzinfo=None)


def _is_complete(state: FetchState | None, *, target_end: datetime) -> bool:
    """True when watermark reaches target end (done) or contract gave up."""
    if state is None:
        return False
    if state.status == "gave_up":
        return True
    if state.status != "done" or state.fetched_to is None:
        return False
    fetched = _as_aware_ist(state.fetched_to)
    return fetched >= target_end


def _build_folder_map(contracts: list[ContractRow]) -> dict[str, str]:
    """Map sanitize_underlying(name) → canonical catalog underlying name."""
    out: dict[str, str] = {}
    for c in contracts:
        key = sanitize_underlying(c.underlying)
        # Prefer the first seen; all contracts for one UL share the same name.
        out.setdefault(key, c.underlying)
    return out


def _scan_series_files(
    *,
    series_root: Path,
    folder_map: dict[str, str],
) -> list[tuple[str, date, Path]]:
    """Return (catalog_underlying, expiry, path) for every series/*.parquet file."""
    if not series_root.is_dir():
        return []
    found: list[tuple[str, date, Path]] = []
    for folder in sorted(series_root.iterdir()):
        if not folder.is_dir():
            continue
        catalog_name = folder_map.get(folder.name)
        if catalog_name is None:
            # Unknown folder (no contracts row) — still report using folder name.
            catalog_name = folder.name
        for path in sorted(folder.glob("*.parquet")):
            stem = path.stem
            if len(stem) != 8 or not stem.isdigit():
                continue
            try:
                expiry = date(int(stem[:4]), int(stem[4:6]), int(stem[6:8]))
            except ValueError:
                continue
            found.append((catalog_name, expiry, path))
    return found


def _scan_minute_files(
    *,
    minute_dir: Path,
    folder_map: dict[str, str],
) -> list[tuple[str, date, Path]]:
    """Backward-compatible alias for ``_scan_series_files``."""
    return _scan_series_files(series_root=minute_dir, folder_map=folder_map)


def _status_for_series(
    series: Series,
    *,
    now: datetime,
    fetch_states: dict[int, FetchState],
    live_keys: set[tuple[str, date]],
    series_root: Path,
    interval: IntervalName = "minute",
    market: MarketProfile | str | None = None,
) -> SeriesStatus:
    now = _as_aware_ist(now)
    target_end = contract_end(now, series.expiry, interval=interval, market=market)
    done = partial = error = gave_up = missing = complete = 0
    fetched_times: list[datetime] = []

    for c in series.contracts:
        state = fetch_states.get(c.token)
        if state is None:
            missing += 1
            continue
        if state.status == "done":
            done += 1
        elif state.status == "partial":
            partial += 1
        elif state.status == "error":
            error += 1
        elif state.status == "gave_up":
            gave_up += 1
        else:
            # Unknown / pending — count as missing-like for visibility.
            missing += 1
        if _is_complete(state, target_end=target_end):
            complete += 1
        if state.fetched_to is not None:
            fetched_times.append(_as_naive(state.fetched_to))

    pending = planner.series_pending_requests(
        series, fetch_states, now=now, interval=interval, market=market
    )
    path = read_mod.series_file_path(
        series.underlying, series.expiry, interval=interval, series_root=series_root
    )
    file_bytes = 0
    file_rows = 0
    file_path: Path | None = None
    if path.is_file():
        file_path = path
        try:
            file_bytes, file_rows, _, _ = read_mod.file_info(path)
        except FileNotFoundError:
            file_path = None

    return SeriesStatus(
        underlying=series.underlying,
        expiry=series.expiry,
        live=(series.underlying, series.expiry) in live_keys,
        contracts=len(series.contracts),
        done=done,
        partial=partial,
        error=error,
        gave_up=gave_up,
        missing=missing,
        complete=complete,
        pending_requests=pending,
        min_fetched_to=min(fetched_times) if fetched_times else None,
        max_fetched_to=max(fetched_times) if fetched_times else None,
        file_path=file_path,
        file_bytes=file_bytes,
        file_rows=file_rows,
    )


def series_status(
    con: duckdb.DuckDBPyConnection,
    *,
    now: datetime | None = None,
    include_all: bool = False,
    expiry: date | None = None,
    underlying: str | None = None,
    interval: IntervalName = "minute",
    series_root: Path | None = None,
    minute_dir: Path | None = None,
    day_dir: Path | None = None,
    market: MarketProfile | str | None = None,
    types: str | frozenset[str] | None = None,
) -> list[SeriesStatus]:
    """Aggregate fetch_state + file info for the download universe (or all on-disk series)."""
    from .universe import resolve_types_filter

    now = _as_aware_ist(now if now is not None else datetime.now(auth.IST))
    if series_root is not None:
        root = series_root
    elif minute_dir is not None and interval == "minute":
        root = minute_dir
    elif day_dir is not None and interval == "day":
        root = day_dir
    else:
        root = DAY_DIR if interval == "day" else MINUTE_DIR

    selected = select_series(
        con, today=now.date(), interval=interval, market=market, types=types
    )
    # select_series mixes live + unfinished; mark live as expiry >= today.
    live_keys = {(s.underlying, s.expiry) for s in selected if s.expiry >= now.date()}

    by_key: dict[tuple[str, date], Series] = {s.key: s for s in selected}
    type_filter = resolve_types_filter(types)

    if include_all:
        all_contracts = list_contracts(con)
        if type_filter is not None:
            all_contracts = [c for c in all_contracts if c.instrument_type in type_filter]
        folder_map = _build_folder_map(all_contracts)
        grouped: dict[tuple[str, date], list[ContractRow]] = {}
        for c in all_contracts:
            grouped.setdefault((c.underlying, c.expiry), []).append(c)
        for catalog_name, exp, _path in _scan_series_files(
            series_root=root, folder_map=folder_map
        ):
            key = (catalog_name, exp)
            if key in by_key:
                continue
            contracts = grouped.get(key, [])
            by_key[key] = Series(
                underlying=catalog_name,
                expiry=exp,
                contracts=tuple(sorted(contracts, key=lambda c: c.token)),
            )

    series_list = list(by_key.values())
    if expiry is not None:
        series_list = [s for s in series_list if s.expiry == expiry]
    if underlying is not None:
        ul = underlying.upper()
        series_list = [
            s
            for s in series_list
            if s.underlying.upper() == ul or sanitize_underlying(s.underlying) == sanitize_underlying(underlying)
        ]

    tokens = [c.token for s in series_list for c in s.contracts]
    fetch_states = list_fetch_states(con, interval=interval, tokens=tokens)
    ranks = rank_mod.load_ranks(con)

    rows = [
        _status_for_series(
            s,
            now=now,
            fetch_states=fetch_states,
            live_keys=live_keys,
            series_root=root,
            interval=interval,
            market=market,
        )
        for s in series_list
    ]

    def sort_key(row: SeriesStatus) -> tuple:
        rank = ranks.get(row.underlying)
        value = rank.traded_value if rank is not None else -1.0
        ranked = rank is not None
        return (
            row.expiry,
            0 if ranked else 1,
            -value,
            row.underlying,
        )

    rows.sort(key=sort_key)
    return rows


def snapshot_gaps(
    con: duckdb.DuckDBPyConnection,
    *,
    today: date | None = None,
    now: datetime | None = None,
    market: MarketProfile | str | None = None,
) -> list[date]:
    """Weekdays from first snapshot through last completed session with no final snapshot.

    Market holidays appear here too — callers should label the list accordingly.
    ``now`` defaults to current IST; when set, ``today`` defaults to ``now.date()``.
    """
    profile = resolve_market(market)
    if now is None:
        now = datetime.now(auth.IST)
    else:
        now = _as_aware_ist(now)
    if today is None:
        today = now.date()
    snaps = list_snapshots(con)
    if not snaps:
        return []
    first = min(s.trade_date for s in snaps)
    # Last completed session day: if still before session_end IST on ``today``, use yesterday.
    session_end = datetime.combine(today, profile.session_end, tzinfo=auth.IST)
    if now < session_end:
        last = today - timedelta(days=1)
    else:
        last = today
    if last < first:
        return []

    final_dates = {s.trade_date for s in snaps if s.status == "final"}
    gaps: list[date] = []
    d = first
    while d <= last:
        if d.weekday() < 5 and d not in final_dates:
            gaps.append(d)
        d += timedelta(days=1)
    return gaps


def totals(rows: list[SeriesStatus]) -> StatusTotals:
    series_n = len(rows)
    contracts = sum(r.contracts for r in rows)
    complete = sum(r.complete for r in rows)
    pending = sum(r.pending_requests for r in rows)
    file_bytes = sum(r.file_bytes for r in rows)
    return StatusTotals(
        series=series_n,
        contracts=contracts,
        complete=complete,
        pending_requests=pending,
        estimated_minutes=pending * planner.request_seconds() / 60.0,
        file_bytes=file_bytes,
    )


def format_bytes(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1024**2:
        return f"{n / 1024:.1f} KB"
    if n < 1024**3:
        return f"{n / 1024**2:.1f} MB"
    return f"{n / 1024**3:.2f} GB"


def watermark_label(value: datetime | None) -> str:
    if value is None:
        return "-"
    return value.strftime("%Y-%m-%d %H:%M")


def expiry_label(expiry: date) -> str:
    return ymd(expiry)
