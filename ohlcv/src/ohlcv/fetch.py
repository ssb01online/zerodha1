"""Candle fetch (minute or day): one series at a time, atomic parquet rewrite, catalog watermarks."""

from __future__ import annotations

import csv
import os
import sys
import tempfile
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

import duckdb

from kitetools import auth

from . import planner
from .catalog import (
    ContractRow,
    FetchState,
    get_snapshot,
    upsert_fetch_states,
)
from .client import (
    KiteClient,
    RequestRejected,
    RetryExhausted,
    SessionExpired,
)
from .markets import MarketProfile
from .paths import (
    DAY_DIR,
    MINUTE_DIR,
    STAGING_DIR,
    ensure_dir,
    paths_for,
    sanitize_underlying,
    series_path,
    ymd,
)
from .planner import IntervalName
from .progress import ContractProgress, InterruptController
from .snapshot import untraded_tokens
from .universe import Series, resolve_market

# Backward-compatible default (minute fetch).
INTERVAL = "minute"
MAX_ERROR_ATTEMPTS = 3
CONSECUTIVE_RETRY_ABORT = 5
LOCK_RETRY_SLEEP_S = 5.0
DUCKDB_MEMORY = "4GB"
DUCKDB_THREADS = 4


def series_root_for(interval: IntervalName) -> Path:
    return DAY_DIR if interval == "day" else MINUTE_DIR


def _spool_name(interval: IntervalName, underlying: str, expiry: date) -> str:
    return f"{interval}_{sanitize_underlying(underlying)}_{ymd(expiry)}.csv"


def _staging_parquet_name(interval: IntervalName, dest: Path) -> str:
    return f"{interval}_{dest.parent.name}_{dest.stem}.parquet"

_SPOOL_COLUMNS = [
    "token",
    "ts",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "oi",
]


class FetchAborted(Exception):
    """Hard stop after consecutive network failures; commit current series then exit."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass
class ContractOutcome:
    token: int
    status: str
    fetched_to: datetime | None
    rows: int
    attempts: int
    last_error: str | None
    skipped: bool = False
    fetched: bool = False


@dataclass
class SeriesResult:
    underlying: str
    expiry: date
    contracts: int
    fetched: int
    skipped: int
    errors: int
    gave_up: int
    rows: int
    elapsed_s: float
    locked: bool = False
    outcomes: list[ContractOutcome] = field(default_factory=list)
    path: Path | None = None
    stop_reason: str | None = None


@dataclass
class FetchRunResult:
    series_done: int
    contracts_fetched: int
    contracts_skipped: int
    rows_written: int
    errors: int
    gave_up: int
    locked: list[tuple[str, date]]
    stopped_reason: str | None = None
    results: list[SeriesResult] = field(default_factory=list)


def _as_aware_ist(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=auth.IST)
    return dt.astimezone(auth.IST)


def _as_naive(dt: datetime) -> datetime:
    dt = _as_aware_ist(dt)
    return dt.replace(tzinfo=None)


def expiry_session_end(
    expiry: date,
    *,
    market: MarketProfile | str | None = None,
) -> datetime:
    profile = resolve_market(market)
    return datetime.combine(expiry, profile.session_end, tzinfo=auth.IST)


def contract_end(
    now: datetime,
    expiry: date,
    *,
    interval: IntervalName = "minute",
    market: MarketProfile | str | None = None,
) -> datetime:
    """Last bar end to fetch: min(interval fetch_end(now), expiry session_end IST)."""
    return min(
        planner.interval_fetch_end(now, interval, market=market),
        expiry_session_end(expiry, market=market),
    )


def classify_status(
    end: datetime,
    now: datetime,
    *,
    interval: IntervalName = "minute",
    market: MarketProfile | str | None = None,
) -> str:
    """done when end is outside the session window; partial when inside."""
    profile = resolve_market(market)
    end = _as_aware_ist(end)
    now = _as_aware_ist(now)
    day = end.date()
    session_start = datetime.combine(day, profile.session_start, tzinfo=auth.IST)
    session_end = datetime.combine(day, profile.session_end, tzinfo=auth.IST)
    if interval == "day":
        # Day bars: incomplete only when watermark is today's bar while session still open.
        if now.date() == day and now < session_end and end >= session_end:
            return "partial"
        if now.date() == day and session_start < now < session_end and end.date() == day:
            return "partial"
        return "done"
    if end >= session_end or end <= session_start:
        return "done"
    # During session hours on end's day — only partial if we're still in that session.
    if now.date() == day and session_start < now < session_end:
        return "partial"
    return "done"


def _weekdays(start: date, end: date) -> list[date]:
    if end < start:
        return []
    out: list[date] = []
    d = start
    while d <= end:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def can_skip_untraded(
    con: duckdb.DuckDBPyConnection,
    token: int,
    start: datetime,
    end: datetime,
    *,
    untraded_cache: dict[date, set[int] | None],
    market: MarketProfile | str | None = None,
) -> bool:
    """True when every weekday in the window has a final snapshot listing token as untraded.

    Requires an existing watermark (caller must check). Missing snapshot → False (must fetch).
    """
    profile = resolve_market(market)
    start = _as_aware_ist(start)
    end = _as_aware_ist(end)
    # Days still needed after the watermark. If start is already at/after session
    # end, that calendar day is covered and must not be required again.
    first = start.date()
    if start.time() >= profile.session_end:
        first = first + timedelta(days=1)
    days = _weekdays(first, end.date())
    # No weekdays in the window → no snapshot evidence; do not skip.
    if not days:
        return False
    for day in days:
        if day not in untraded_cache:
            snap = get_snapshot(con, day)
            if snap is None or snap.status != "final":
                untraded_cache[day] = None
            else:
                try:
                    untraded_cache[day] = untraded_tokens(
                        day, dest_dir=paths_for(profile.id).daily_dir
                    )
                except FileNotFoundError:
                    untraded_cache[day] = None
        cached = untraded_cache[day]
        if cached is None:
            return False
        if token not in cached:
            return False
    return True


def traded_day_dates(
    underlying: str,
    expiry: date,
    token: int,
    *,
    day_dir: Path | None = None,
    cache: dict[tuple[str, date, int], set[date] | None] | None = None,
) -> set[date] | None:
    """Calendar dates with day-bar volume > 0 for ``token``.

    Returns ``None`` when the series day parquet is missing or the token has
    no rows yet (unknown — do not day-skip). Returns an empty set when the
    token has day rows but none with ``volume > 0``.
    """
    key = (underlying, expiry, token)
    if cache is not None and key in cache:
        return cache[key]
    root = day_dir if day_dir is not None else DAY_DIR
    path = series_path(root, underlying, expiry)
    if not path.is_file():
        if cache is not None:
            cache[key] = None
        return None
    con = duckdb.connect()
    try:
        any_row = con.execute(
            "SELECT COUNT(*) FROM read_parquet(?) WHERE token = ?",
            [str(path), token],
        ).fetchone()
        if not any_row or int(any_row[0]) == 0:
            result: set[date] | None = None
        else:
            rows = con.execute(
                """
                SELECT DISTINCT CAST(ts AS DATE) AS d
                FROM read_parquet(?)
                WHERE token = ? AND volume > 0
                ORDER BY d
                """,
                [str(path), token],
            ).fetchall()
            result = {
                r[0] if isinstance(r[0], date) else date.fromisoformat(str(r[0]))
                for r in rows
            }
    except Exception:
        result = None
    finally:
        con.close()
    if cache is not None:
        cache[key] = result
    return result


def _minute_start_from_traded(
    start: datetime,
    traded: set[date] | None,
    *,
    backfill_days: int | None,
    market: MarketProfile | str | None = None,
) -> datetime:
    """Tighten minute window start using traded day bars when ``--days all``."""
    profile = resolve_market(market)
    start = _as_aware_ist(start)
    if backfill_days is not None or not traded:
        return start
    first = min(traded)
    day_start = datetime.combine(first, profile.session_start, tzinfo=auth.IST)
    return max(start, day_start)


def can_skip_no_traded_days(
    start: datetime,
    end: datetime,
    traded: set[date] | None,
    *,
    backfill_days: int | None,
    has_watermark: bool,
    market: MarketProfile | str | None = None,
) -> bool:
    """Skip minute fetch when day bars show no volume in the needed window.

    - ``--days all``: skip when traded set is known empty (token has day rows,
      all volume 0); no watermark required.
    - ``--days N``: skip only with an existing watermark when no traded day
      overlaps the remaining window (empty traded set counts as no overlap).
    - Unknown (``traded is None``): never skip via this rule.
    """
    if traded is None:
        return False
    profile = resolve_market(market)
    start = _as_aware_ist(start)
    end = _as_aware_ist(end)
    if backfill_days is None:
        return len(traded) == 0
    if not has_watermark:
        return False
    if not traded:
        return True
    first = start.date()
    if start.time() >= profile.session_end:
        first = first + timedelta(days=1)
    last = end.date()
    return not any(first <= d <= last for d in traded)


def _chunk_windows(
    start: datetime,
    end: datetime,
    *,
    chunk_days: int = planner.MINUTE_CHUNK_DAYS,
    market: MarketProfile | str | None = None,
) -> list[tuple[datetime, datetime]]:
    profile = resolve_market(market)
    start = _as_aware_ist(start)
    end = _as_aware_ist(end)
    if end <= start:
        return []
    windows: list[tuple[datetime, datetime]] = []
    cursor = start
    while cursor < end:
        # Inclusive calendar span of chunk_days ending at chunk_end.
        chunk_end_date = cursor.date() + timedelta(days=chunk_days - 1)
        chunk_end = datetime.combine(chunk_end_date, end.timetz().replace(tzinfo=None), tzinfo=auth.IST)
        # Preserve end's time-of-day on the final chunk; for intermediate use session end.
        if chunk_end_date >= end.date():
            chunk_end = end
        else:
            chunk_end = datetime.combine(chunk_end_date, profile.session_end, tzinfo=auth.IST)
        if chunk_end <= cursor:
            break
        windows.append((cursor, chunk_end))
        if chunk_end >= end:
            break
        next_day = chunk_end.date() + timedelta(days=1)
        cursor = datetime.combine(next_day, profile.session_start, tzinfo=auth.IST)
    return windows


def _candle_ts(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value))
        except ValueError:
            return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(auth.IST)
    return dt.replace(tzinfo=None)


def _candle_row(token: int, candle: dict[str, Any]) -> dict[str, Any] | None:
    ts = _candle_ts(candle.get("date"))
    if ts is None:
        return None
    oi_raw = candle.get("oi")
    if oi_raw is None:
        oi_raw = candle.get("OI")
    return {
        "token": token,
        "ts": ts.isoformat(sep=" "),
        "open": float(candle["open"]),
        "high": float(candle["high"]),
        "low": float(candle["low"]),
        "close": float(candle["close"]),
        "volume": int(candle.get("volume") or 0),
        "oi": int(oi_raw or 0),
    }


def _write_contracts_csv(contracts: Sequence[ContractRow], path: Path) -> None:
    with open(path, "w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(
            [
                "token",
                "underlying",
                "expiry",
                "tradingsymbol",
                "instrument_type",
                "strike",
                "lot_size",
            ]
        )
        for c in contracts:
            writer.writerow(
                [
                    c.token,
                    c.underlying,
                    c.expiry.isoformat(),
                    c.tradingsymbol,
                    c.instrument_type,
                    c.strike,
                    c.lot_size,
                ]
            )


def merge_series_parquet(
    *,
    existing: Path | None,
    spool_csv: Path | None,
    contracts: Sequence[ContractRow],
    dest: Path,
    staging_dir: Path,
    interval: IntervalName = "minute",
) -> int:
    """Merge existing parquet + spool CSV into dest via staging. Returns row count written."""
    ensure_dir(dest.parent)
    ensure_dir(staging_dir)
    tmp_dir = ensure_dir(staging_dir / "duckdb_tmp")
    staging = staging_dir / _staging_parquet_name(interval, dest)
    fd, contracts_name = tempfile.mkstemp(prefix="ohlcv_contracts_", suffix=".csv")
    os.close(fd)
    contracts_path = Path(contracts_name)
    try:
        _write_contracts_csv(contracts, contracts_path)
        con = duckdb.connect()
        try:
            con.execute(f"SET memory_limit='{DUCKDB_MEMORY}'")
            con.execute(f"SET threads={DUCKDB_THREADS}")
            con.execute(f"SET temp_directory='{tmp_dir.as_posix()}'")

            has_existing = existing is not None and existing.is_file()
            has_spool = spool_csv is not None and spool_csv.is_file() and spool_csv.stat().st_size > 0

            if not has_existing and not has_spool:
                # Empty series file still useful as a marker; write zero rows with schema.
                con.execute(
                    """
                    COPY (
                        SELECT
                            CAST(NULL AS VARCHAR) AS underlying,
                            CAST(NULL AS DATE) AS expiry,
                            CAST(NULL AS BIGINT) AS token,
                            CAST(NULL AS VARCHAR) AS tradingsymbol,
                            CAST(NULL AS VARCHAR) AS instrument_type,
                            CAST(NULL AS DOUBLE) AS strike,
                            CAST(NULL AS INTEGER) AS lot_size,
                            CAST(NULL AS TIMESTAMP) AS ts,
                            CAST(NULL AS DOUBLE) AS open,
                            CAST(NULL AS DOUBLE) AS high,
                            CAST(NULL AS DOUBLE) AS low,
                            CAST(NULL AS DOUBLE) AS close,
                            CAST(NULL AS BIGINT) AS volume,
                            CAST(NULL AS BIGINT) AS oi
                        WHERE FALSE
                    ) TO ? (FORMAT parquet, COMPRESSION zstd)
                    """,
                    [str(staging)],
                )
                os.replace(staging, dest)
                return 0

            parts: list[str] = []
            params: list[Any] = []
            if has_existing:
                parts.append(
                    """
                    SELECT
                        underlying, expiry, token, tradingsymbol, instrument_type,
                        strike, lot_size, ts, open, high, low, close, volume, oi,
                        0 AS src
                    FROM read_parquet(?)
                    """
                )
                params.append(str(existing))
            if has_spool:
                parts.append(
                    """
                    SELECT
                        c.underlying,
                        CAST(c.expiry AS DATE) AS expiry,
                        CAST(s.token AS BIGINT) AS token,
                        c.tradingsymbol,
                        c.instrument_type,
                        CAST(c.strike AS DOUBLE) AS strike,
                        CAST(c.lot_size AS INTEGER) AS lot_size,
                        CAST(s.ts AS TIMESTAMP) AS ts,
                        CAST(s.open AS DOUBLE) AS open,
                        CAST(s.high AS DOUBLE) AS high,
                        CAST(s.low AS DOUBLE) AS low,
                        CAST(s.close AS DOUBLE) AS close,
                        CAST(s.volume AS BIGINT) AS volume,
                        CAST(s.oi AS BIGINT) AS oi,
                        1 AS src
                    FROM read_csv(?, header = true, auto_detect = true) s
                    JOIN read_csv(?, header = true, auto_detect = true) c
                      ON CAST(s.token AS BIGINT) = CAST(c.token AS BIGINT)
                    """
                )
                params.extend([str(spool_csv), str(contracts_path)])

            union_sql = " UNION ALL ".join(parts)
            sql = f"""
                COPY (
                    SELECT underlying, expiry, token, tradingsymbol, instrument_type,
                           strike, lot_size, ts, open, high, low, close, volume, oi
                    FROM (
                        SELECT *,
                               row_number() OVER (
                                   PARTITION BY token, ts ORDER BY src DESC
                               ) AS rn
                        FROM ({union_sql})
                    )
                    WHERE rn = 1
                    ORDER BY token, ts
                ) TO ? (FORMAT parquet, COMPRESSION zstd)
            """
            params.append(str(staging))
            con.execute(sql, params)
            count_row = con.execute(
                "SELECT COUNT(*) FROM read_parquet(?)", [str(staging)]
            ).fetchone()
            row_count = int(count_row[0]) if count_row else 0
        finally:
            con.close()
        os.replace(staging, dest)
        return row_count
    finally:
        try:
            contracts_path.unlink()
        except OSError:
            pass
        if staging.exists():
            try:
                staging.unlink()
            except OSError:
                pass


def _open_spool(path: Path) -> tuple[Any, csv.DictWriter]:
    ensure_dir(path.parent)
    new_file = not path.exists() or path.stat().st_size == 0
    fh = open(path, "a", encoding="utf-8", newline="")
    writer = csv.DictWriter(fh, fieldnames=_SPOOL_COLUMNS)
    if new_file:
        writer.writeheader()
    return fh, writer


def process_contract(
    client: KiteClient,
    contract: ContractRow,
    state: FetchState | None,
    *,
    now: datetime,
    con: duckdb.DuckDBPyConnection,
    untraded_cache: dict[date, set[int] | None],
    spool_writer: csv.DictWriter,
    today: date,
    interval: IntervalName = "minute",
    backfill_days: int | None = planner.BACKFILL_WINDOW_DAYS,
    traded_days: set[date] | None = None,
    market: MarketProfile | str | None = None,
) -> ContractOutcome:
    """Fetch one contract's window into the spool. May raise SessionExpired / FetchAborted."""
    end = contract_end(now, contract.expiry, interval=interval, market=market)
    start = planner.contract_range_start(
        state,
        now=now,
        interval=interval,
        backfill_days=backfill_days if interval == "minute" else planner.BACKFILL_WINDOW_DAYS,
        market=market,
    )
    if interval == "minute":
        start = _minute_start_from_traded(
            start, traded_days, backfill_days=backfill_days, market=market
        )
    prev_attempts = state.attempts if state is not None else 0
    prev_rows = state.rows_total if state is not None else 0
    prev_fetched = state.fetched_to if state is not None else None
    has_watermark = state is not None and state.fetched_to is not None

    if end <= start:
        # Nothing to fetch — keep watermark at max(prev, end).
        watermark = end if prev_fetched is None else max(_as_aware_ist(prev_fetched), end)
        return ContractOutcome(
            token=contract.token,
            status=classify_status(watermark, now, interval=interval, market=market),
            fetched_to=_as_naive(watermark),
            rows=prev_rows,
            attempts=prev_attempts,
            last_error=None,
            skipped=True,
            fetched=False,
        )

    if interval == "minute" and can_skip_no_traded_days(
        start,
        end,
        traded_days,
        backfill_days=backfill_days,
        has_watermark=has_watermark,
        market=market,
    ):
        return ContractOutcome(
            token=contract.token,
            status=classify_status(end, now, interval=interval, market=market),
            fetched_to=_as_naive(end),
            rows=prev_rows,
            attempts=prev_attempts,
            last_error=None,
            skipped=True,
            fetched=False,
        )

    # Untraded skip: only when a watermark already exists.
    if has_watermark:
        if can_skip_untraded(
            con,
            contract.token,
            start,
            end,
            untraded_cache=untraded_cache,
            market=market,
        ):
            return ContractOutcome(
                token=contract.token,
                status=classify_status(end, now, interval=interval, market=market),
                fetched_to=_as_naive(end),
                rows=prev_rows,
                attempts=prev_attempts,
                last_error=None,
                skipped=True,
                fetched=False,
            )

    windows = _chunk_windows(
        start, end, chunk_days=planner.chunk_days_for(interval), market=market
    )
    rows_written = 0
    try:
        for win_start, win_end in windows:
            candles = client.historical(
                contract.token,
                _as_naive(win_start),
                _as_naive(win_end),
                interval,
                oi=True,
            )
            for candle in candles:
                row = _candle_row(contract.token, candle)
                if row is None:
                    continue
                # Drop bars past the requested end (incomplete minute / future day).
                ts = datetime.fromisoformat(row["ts"])
                ts_aware = ts.replace(tzinfo=auth.IST)
                if interval == "day":
                    if ts_aware.date() > end.date():
                        continue
                elif ts_aware > end:
                    continue
                spool_writer.writerow(row)
                rows_written += 1
    except SessionExpired:
        raise
    except RequestRejected as exc:
        attempts = prev_attempts + 1
        status = "error"
        if contract.expiry < today and attempts >= MAX_ERROR_ATTEMPTS:
            status = "gave_up"
        return ContractOutcome(
            token=contract.token,
            status=status,
            fetched_to=_as_naive(prev_fetched) if prev_fetched is not None else None,
            rows=prev_rows,
            attempts=attempts,
            last_error=str(exc)[:500],
            skipped=False,
            fetched=False,
        )
    except RetryExhausted as exc:
        attempts = prev_attempts + 1
        status = "error"
        if contract.expiry < today and attempts >= MAX_ERROR_ATTEMPTS:
            status = "gave_up"
        return ContractOutcome(
            token=contract.token,
            status=status,
            fetched_to=_as_naive(prev_fetched) if prev_fetched is not None else None,
            rows=prev_rows,
            attempts=attempts,
            last_error=str(exc)[:500],
            skipped=False,
            fetched=False,
        )

    return ContractOutcome(
        token=contract.token,
        status=classify_status(end, now, interval=interval, market=market),
        fetched_to=_as_naive(end),
        rows=prev_rows + rows_written,
        attempts=prev_attempts + 1,
        last_error=None,
        skipped=False,
        fetched=True,
    )


def _outcomes_to_states(
    outcomes: Iterable[ContractOutcome],
    *,
    updated_at: datetime,
    interval: IntervalName = "minute",
) -> list[FetchState]:
    naive_updated = _as_naive(updated_at)
    return [
        FetchState(
            token=o.token,
            interval=interval,
            fetched_to=o.fetched_to,
            status=o.status,
            rows_total=o.rows,
            attempts=o.attempts,
            last_error=o.last_error,
            updated_at=naive_updated,
        )
        for o in outcomes
    ]


def commit_series(
    con: duckdb.DuckDBPyConnection,
    *,
    series: Series,
    outcomes: list[ContractOutcome],
    spool_csv: Path | None,
    series_root: Path,
    staging_dir: Path,
    now: datetime,
    interval: IntervalName = "minute",
    minute_dir: Path | None = None,
) -> tuple[Path, int]:
    """Merge spool into series parquet and commit fetch_state. Returns (path, rows)."""
    root = minute_dir if minute_dir is not None else series_root
    dest = series_path(root, series.underlying, series.expiry)
    existing = dest if dest.is_file() else None
    # Only merge contracts that produced spool rows or already exist — always rewrite with all series contracts meta available.
    row_count = merge_series_parquet(
        existing=existing,
        spool_csv=spool_csv,
        contracts=series.contracts,
        dest=dest,
        staging_dir=staging_dir,
        interval=interval,
    )
    upsert_fetch_states(con, _outcomes_to_states(outcomes, updated_at=now, interval=interval))
    if spool_csv is not None and spool_csv.exists():
        try:
            spool_csv.unlink()
        except OSError:
            pass
    return dest, row_count


def process_series(
    client: KiteClient,
    con: duckdb.DuckDBPyConnection,
    series: Series,
    fetch_states: dict[int, FetchState],
    *,
    now: datetime,
    interval: IntervalName = "minute",
    series_root: Path | None = None,
    minute_dir: Path | None = None,
    day_dir: Path | None = None,
    staging_dir: Path = STAGING_DIR,
    untraded_cache: dict[date, set[int] | None] | None = None,
    traded_days_cache: dict[tuple[str, date, int], set[date] | None] | None = None,
    backfill_days: int | None = planner.BACKFILL_WINDOW_DAYS,
    should_stop: Callable[[], str | None] | None = None,
    on_contract: Callable[[ContractProgress], None] | None = None,
    series_index: int = 1,
    series_total: int = 1,
    market: MarketProfile | str | None = None,
) -> SeriesResult:
    """Fetch all contracts in one series; merge + commit. Raises SessionExpired after commit attempt."""
    t0 = time.monotonic()
    today = _as_aware_ist(now).date()
    cache = untraded_cache if untraded_cache is not None else {}
    traded_cache = traded_days_cache if traded_days_cache is not None else {}
    day_root = day_dir if day_dir is not None else DAY_DIR
    root = series_root if series_root is not None else (
        minute_dir if minute_dir is not None else series_root_for(interval)
    )
    ensure_dir(staging_dir)
    spool_path = staging_dir / _spool_name(interval, series.underlying, series.expiry)
    if spool_path.exists():
        try:
            spool_path.unlink()
        except OSError:
            pass

    outcomes: list[ContractOutcome] = []
    consecutive_retry_fails = 0
    interrupt_reason: str | None = None
    session_exc: SessionExpired | None = None
    abort_exc: FetchAborted | None = None
    contract_total = len(series.contracts)

    fh, writer = _open_spool(spool_path)
    try:
        for idx, contract in enumerate(series.contracts, start=1):
            if should_stop is not None:
                reason = should_stop()
                if reason is not None:
                    interrupt_reason = reason
                    break
            state = fetch_states.get(contract.token)
            traded: set[date] | None = None
            if interval == "minute":
                traded = traded_day_dates(
                    series.underlying,
                    series.expiry,
                    contract.token,
                    day_dir=day_root,
                    cache=traded_cache,
                )
            try:
                outcome = process_contract(
                    client,
                    contract,
                    state,
                    now=now,
                    con=con,
                    untraded_cache=cache,
                    spool_writer=writer,
                    today=today,
                    interval=interval,
                    backfill_days=backfill_days,
                    traded_days=traded,
                    market=market,
                )
            except SessionExpired as exc:
                session_exc = exc
                break
            fh.flush()
            outcomes.append(outcome)
            if on_contract is not None:
                on_contract(
                    ContractProgress(
                        series_index=series_index,
                        series_total=series_total,
                        underlying=series.underlying,
                        expiry=series.expiry,
                        contract_index=idx,
                        contract_total=contract_total,
                        fetched=sum(1 for o in outcomes if o.fetched),
                        skipped=sum(1 for o in outcomes if o.skipped),
                        errors=sum(1 for o in outcomes if o.status == "error"),
                        gave_up=sum(1 for o in outcomes if o.status == "gave_up"),
                        rows=sum(o.rows for o in outcomes),
                        requests=client.counters.requests,
                        retries=client.counters.retries,
                        elapsed_s=time.monotonic() - t0,
                        tradingsymbol=contract.tradingsymbol,
                        last_status=outcome.status,
                    )
                )
            if outcome.status == "error" and outcome.last_error and "retries exhausted" in (outcome.last_error or ""):
                consecutive_retry_fails += 1
                if consecutive_retry_fails >= CONSECUTIVE_RETRY_ABORT:
                    abort_exc = FetchAborted(
                        f"{consecutive_retry_fails} consecutive RetryExhausted; aborting run"
                    )
                    break
            elif outcome.fetched or outcome.skipped:
                consecutive_retry_fails = 0
            elif outcome.status == "error":
                # RequestRejected — not a network death spiral
                consecutive_retry_fails = 0
    finally:
        fh.close()

    fetched = sum(1 for o in outcomes if o.fetched)
    skipped = sum(1 for o in outcomes if o.skipped)
    errors = sum(1 for o in outcomes if o.status == "error")
    gave_up = sum(1 for o in outcomes if o.status == "gave_up")
    elapsed = time.monotonic() - t0

    stop = interrupt_reason
    if abort_exc is not None:
        stop = abort_exc.reason

    if not outcomes:
        result = SeriesResult(
            underlying=series.underlying,
            expiry=series.expiry,
            contracts=len(series.contracts),
            fetched=0,
            skipped=0,
            errors=0,
            gave_up=0,
            rows=0,
            elapsed_s=elapsed,
            outcomes=[],
            stop_reason=stop,
        )
        if session_exc is not None:
            raise session_exc
        return result

    # Try merge + commit. On lock, leave state uncommitted (keep spool for retry).
    try:
        path, row_count = commit_series(
            con,
            series=series,
            outcomes=outcomes,
            spool_csv=spool_path if spool_path.exists() else None,
            series_root=root,
            staging_dir=staging_dir,
            now=now,
            interval=interval,
        )
        for o in outcomes:
            fetch_states[o.token] = FetchState(
                token=o.token,
                interval=interval,
                fetched_to=o.fetched_to,
                status=o.status,
                rows_total=o.rows,
                attempts=o.attempts,
                last_error=o.last_error,
                updated_at=_as_naive(now),
            )
        result = SeriesResult(
            underlying=series.underlying,
            expiry=series.expiry,
            contracts=len(series.contracts),
            fetched=fetched,
            skipped=skipped,
            errors=errors,
            gave_up=gave_up,
            rows=row_count,
            elapsed_s=elapsed,
            locked=False,
            outcomes=outcomes,
            path=path,
            stop_reason=stop,
        )
    except PermissionError:
        result = SeriesResult(
            underlying=series.underlying,
            expiry=series.expiry,
            contracts=len(series.contracts),
            fetched=fetched,
            skipped=skipped,
            errors=errors,
            gave_up=gave_up,
            rows=0,
            elapsed_s=elapsed,
            locked=True,
            outcomes=outcomes,
            path=series_path(root, series.underlying, series.expiry),
            stop_reason=stop,
        )

    if session_exc is not None:
        # Attach result so caller can record progress before re-raising.
        session_exc.result = result  # type: ignore[attr-defined]
        raise session_exc
    return result


def count_skippable(
    con: duckdb.DuckDBPyConnection,
    series: Series,
    fetch_states: dict[int, FetchState],
    *,
    now: datetime,
    untraded_cache: dict[date, set[int] | None] | None = None,
    traded_days_cache: dict[tuple[str, date, int], set[date] | None] | None = None,
    interval: IntervalName = "minute",
    backfill_days: int | None = planner.BACKFILL_WINDOW_DAYS,
    day_dir: Path | None = None,
    market: MarketProfile | str | None = None,
) -> int:
    """How many contracts would be skipped by untraded / no-traded-day rules (dry-run)."""
    cache = untraded_cache if untraded_cache is not None else {}
    traded_cache = traded_days_cache if traded_days_cache is not None else {}
    day_root = day_dir if day_dir is not None else DAY_DIR
    n = 0
    for c in series.contracts:
        state = fetch_states.get(c.token)
        if state is not None and state.status == "gave_up":
            continue
        end = contract_end(now, c.expiry, interval=interval, market=market)
        start = planner.contract_range_start(
            state,
            now=now,
            interval=interval,
            backfill_days=backfill_days if interval == "minute" else planner.BACKFILL_WINDOW_DAYS,
            market=market,
        )
        traded: set[date] | None = None
        if interval == "minute":
            traded = traded_day_dates(
                series.underlying,
                series.expiry,
                c.token,
                day_dir=day_root,
                cache=traded_cache,
            )
            start = _minute_start_from_traded(
                start, traded, backfill_days=backfill_days, market=market
            )
        if end <= start:
            continue
        has_watermark = state is not None and state.fetched_to is not None
        if interval == "minute" and can_skip_no_traded_days(
            start,
            end,
            traded,
            backfill_days=backfill_days,
            has_watermark=has_watermark,
            market=market,
        ):
            n += 1
            continue
        if not has_watermark:
            continue
        if can_skip_untraded(
            con, c.token, start, end, untraded_cache=cache, market=market
        ):
            n += 1
    return n


def run_fetch(
    client: KiteClient,
    con: duckdb.DuckDBPyConnection,
    queue_series: Sequence[Series],
    fetch_states: dict[int, FetchState],
    *,
    now: datetime | None = None,
    max_series: int | None = None,
    max_minutes: float | None = None,
    interval: IntervalName = "minute",
    series_root: Path | None = None,
    minute_dir: Path | None = None,
    day_dir: Path | None = None,
    staging_dir: Path = STAGING_DIR,
    backfill_days: int | None = planner.BACKFILL_WINDOW_DAYS,
    on_series: Callable[[int, int, SeriesResult], None] | None = None,
    on_contract: Callable[[ContractProgress], None] | None = None,
    on_series_start: Callable[[int, int, Series], None] | None = None,
    interrupt: InterruptController | None = None,
    deadline_monotonic: float | None = None,
    market: MarketProfile | str | None = None,
) -> FetchRunResult:
    """Process queue series in order. Handles Ctrl+C, budgets, lock retries.

    ``deadline_monotonic`` shares a wall-clock budget across multiple passes
    (day then minute). When set, it overrides deriving a deadline from
    ``max_minutes`` alone.
    """
    now = now if now is not None else datetime.now(auth.IST)
    now = _as_aware_ist(now)
    root = series_root if series_root is not None else (
        minute_dir if minute_dir is not None else series_root_for(interval)
    )
    day_root = day_dir if day_dir is not None else DAY_DIR
    own_interrupt = interrupt is None
    ctrl = interrupt if interrupt is not None else InterruptController()
    if own_interrupt:
        ctrl.install()
    if deadline_monotonic is not None:
        deadline = deadline_monotonic
    elif max_minutes is None:
        deadline = None
    else:
        deadline = time.monotonic() + max_minutes * 60.0
    untraded_cache: dict[date, set[int] | None] = {}
    traded_days_cache: dict[tuple[str, date, int], set[date] | None] = {}
    results: list[SeriesResult] = []
    locked: list[tuple[str, date, Series, list[ContractOutcome], Path]] = []
    series_list = list(queue_series)
    if max_series is not None:
        series_list = series_list[:max_series]
    total = len(series_list)

    def should_stop() -> str | None:
        reason = ctrl.should_stop()
        if reason is not None:
            return reason
        if deadline is not None and time.monotonic() >= deadline:
            return f"max-minutes budget ({max_minutes})"
        return None

    stopped: str | None = None
    try:
        for idx, series in enumerate(series_list, start=1):
            if should_stop() is not None:
                stopped = should_stop()
                break
            if on_series_start is not None:
                on_series_start(idx, total, series)
            try:
                result = process_series(
                    client,
                    con,
                    series,
                    fetch_states,
                    now=now,
                    interval=interval,
                    series_root=root,
                    day_dir=day_root,
                    staging_dir=staging_dir,
                    untraded_cache=untraded_cache,
                    traded_days_cache=traded_days_cache,
                    backfill_days=backfill_days,
                    should_stop=should_stop,
                    on_contract=on_contract,
                    series_index=idx,
                    series_total=total,
                    market=market,
                )
            except SessionExpired as exc:
                attached = getattr(exc, "result", None)
                if isinstance(attached, SeriesResult):
                    if attached.locked:
                        locked.append(
                            (
                                series.underlying,
                                series.expiry,
                                series,
                                attached.outcomes,
                                series_path(root, series.underlying, series.expiry),
                            )
                        )
                    results.append(attached)
                    if on_series is not None:
                        on_series(idx, total, attached)
                raise

            if result.locked:
                locked.append(
                    (
                        series.underlying,
                        series.expiry,
                        series,
                        result.outcomes,
                        series_path(root, series.underlying, series.expiry),
                    )
                )
            results.append(result)
            if on_series is not None:
                on_series(idx, total, result)

            if result.stop_reason is not None:
                stopped = result.stop_reason
                break
            if should_stop() is not None:
                stopped = should_stop()
                break
    finally:
        if own_interrupt:
            ctrl.restore()

    # Retry locked series once after a short sleep.
    still_locked: list[tuple[str, date]] = []
    if locked and stopped is None:
        time.sleep(LOCK_RETRY_SLEEP_S)
        for underlying, expiry, series, outcomes, _path in locked:
            spool = staging_dir / _spool_name(interval, series.underlying, series.expiry)
            try:
                path, row_count = commit_series(
                    con,
                    series=series,
                    outcomes=outcomes,
                    spool_csv=spool if spool.exists() else None,
                    series_root=root,
                    staging_dir=staging_dir,
                    now=now,
                    interval=interval,
                )
                for o in outcomes:
                    fetch_states[o.token] = FetchState(
                        token=o.token,
                        interval=interval,
                        fetched_to=o.fetched_to,
                        status=o.status,
                        rows_total=o.rows,
                        attempts=o.attempts,
                        last_error=o.last_error,
                        updated_at=_as_naive(now),
                    )
                # Update matching result
                for r in results:
                    if r.underlying == underlying and r.expiry == expiry:
                        r.locked = False
                        r.rows = row_count
                        r.path = path
                        break
            except PermissionError:
                still_locked.append((underlying, expiry))
                print(
                    f"locked: {series_path(root, underlying, expiry)} "
                    "(close notebooks holding the file)",
                    file=sys.stderr,
                )
    else:
        still_locked = [(u, e) for u, e, *_ in locked]
        for u, e, series, _outcomes, path in locked:
            print(
                f"locked: {path} (close notebooks holding the file)",
                file=sys.stderr,
            )

    return FetchRunResult(
        series_done=sum(1 for r in results if not r.locked),
        contracts_fetched=sum(r.fetched for r in results),
        contracts_skipped=sum(r.skipped for r in results),
        rows_written=sum(r.rows for r in results),
        errors=sum(r.errors for r in results),
        gave_up=sum(r.gave_up for r in results),
        locked=still_locked,
        stopped_reason=stopped,
        results=results,
    )
