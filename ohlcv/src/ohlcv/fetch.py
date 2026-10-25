"""Minute candle fetch: one series at a time, atomic parquet rewrite, catalog watermarks."""

from __future__ import annotations

import csv
import os
import signal
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
from .paths import (
    MINUTE_DIR,
    STAGING_DIR,
    ensure_dir,
    sanitize_underlying,
    series_path,
    ymd,
)
from .snapshot import untraded_tokens
from .universe import SESSION_END, Series

INTERVAL = "minute"
MAX_ERROR_ATTEMPTS = 3
CONSECUTIVE_RETRY_ABORT = 5
LOCK_RETRY_SLEEP_S = 5.0
DUCKDB_MEMORY = "4GB"
DUCKDB_THREADS = 4

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


def expiry_session_end(expiry: date) -> datetime:
    return datetime.combine(expiry, SESSION_END, tzinfo=auth.IST)


def contract_end(now: datetime, expiry: date) -> datetime:
    """Last minute to fetch for a contract: min(fetch_end(now), expiry 15:40 IST)."""
    return min(planner.fetch_end(now), expiry_session_end(expiry))


def classify_status(end: datetime, now: datetime) -> str:
    """done when end is outside the session window; partial when inside."""
    end = _as_aware_ist(end)
    now = _as_aware_ist(now)
    day = end.date()
    session_start = datetime.combine(day, planner.SESSION_START, tzinfo=auth.IST)
    session_end = datetime.combine(day, SESSION_END, tzinfo=auth.IST)
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
) -> bool:
    """True when every weekday in the window has a final snapshot listing token as untraded.

    Requires an existing watermark (caller must check). Missing snapshot → False (must fetch).
    """
    start = _as_aware_ist(start)
    end = _as_aware_ist(end)
    # Days still needed after the watermark. If start is already at/after session
    # end, that calendar day is covered and must not be required again.
    first = start.date()
    if start.time() >= SESSION_END:
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
                    untraded_cache[day] = untraded_tokens(day)
                except FileNotFoundError:
                    untraded_cache[day] = None
        cached = untraded_cache[day]
        if cached is None:
            return False
        if token not in cached:
            return False
    return True


def _chunk_windows(
    start: datetime,
    end: datetime,
    *,
    chunk_days: int = planner.MINUTE_CHUNK_DAYS,
) -> list[tuple[datetime, datetime]]:
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
            chunk_end = datetime.combine(chunk_end_date, SESSION_END, tzinfo=auth.IST)
        if chunk_end <= cursor:
            break
        windows.append((cursor, chunk_end))
        if chunk_end >= end:
            break
        next_day = chunk_end.date() + timedelta(days=1)
        cursor = datetime.combine(next_day, planner.SESSION_START, tzinfo=auth.IST)
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
) -> int:
    """Merge existing parquet + spool CSV into dest via staging. Returns row count written."""
    ensure_dir(dest.parent)
    ensure_dir(staging_dir)
    tmp_dir = ensure_dir(staging_dir / "duckdb_tmp")
    staging = staging_dir / f"minute_{dest.parent.name}_{dest.stem}.parquet"
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
) -> ContractOutcome:
    """Fetch one contract's minute window into the spool. May raise SessionExpired / FetchAborted."""
    end = contract_end(now, contract.expiry)
    start = planner.contract_range_start(state, now=now)
    prev_attempts = state.attempts if state is not None else 0
    prev_rows = state.rows_total if state is not None else 0
    prev_fetched = state.fetched_to if state is not None else None

    if end <= start:
        # Nothing to fetch — keep watermark at max(prev, end).
        watermark = end if prev_fetched is None else max(_as_aware_ist(prev_fetched), end)
        return ContractOutcome(
            token=contract.token,
            status=classify_status(watermark, now),
            fetched_to=_as_naive(watermark),
            rows=prev_rows,
            attempts=prev_attempts,
            last_error=None,
            skipped=True,
            fetched=False,
        )

    # Skip rule: only when a watermark already exists.
    if state is not None and state.fetched_to is not None:
        if can_skip_untraded(con, contract.token, start, end, untraded_cache=untraded_cache):
            return ContractOutcome(
                token=contract.token,
                status=classify_status(end, now),
                fetched_to=_as_naive(end),
                rows=prev_rows,
                attempts=prev_attempts,
                last_error=None,
                skipped=True,
                fetched=False,
            )

    windows = _chunk_windows(start, end)
    rows_written = 0
    try:
        for win_start, win_end in windows:
            candles = client.historical(
                contract.token,
                _as_naive(win_start),
                _as_naive(win_end),
                INTERVAL,
                oi=True,
            )
            for candle in candles:
                row = _candle_row(contract.token, candle)
                if row is None:
                    continue
                # Drop incomplete current minute when fetching during session.
                ts = datetime.fromisoformat(row["ts"])
                if ts.replace(tzinfo=auth.IST) > end:
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
        status=classify_status(end, now),
        fetched_to=_as_naive(end),
        rows=prev_rows + rows_written,
        attempts=prev_attempts + 1,
        last_error=None,
        skipped=False,
        fetched=True,
    )


def _outcomes_to_states(outcomes: Iterable[ContractOutcome], *, updated_at: datetime) -> list[FetchState]:
    naive_updated = _as_naive(updated_at)
    return [
        FetchState(
            token=o.token,
            interval=INTERVAL,
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
    minute_dir: Path,
    staging_dir: Path,
    now: datetime,
) -> tuple[Path, int]:
    """Merge spool into series parquet and commit fetch_state. Returns (path, rows)."""
    dest = series_path(minute_dir, series.underlying, series.expiry)
    existing = dest if dest.is_file() else None
    # Only merge contracts that produced spool rows or already exist — always rewrite with all series contracts meta available.
    row_count = merge_series_parquet(
        existing=existing,
        spool_csv=spool_csv,
        contracts=series.contracts,
        dest=dest,
        staging_dir=staging_dir,
    )
    upsert_fetch_states(con, _outcomes_to_states(outcomes, updated_at=now))
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
    minute_dir: Path = MINUTE_DIR,
    staging_dir: Path = STAGING_DIR,
    untraded_cache: dict[date, set[int] | None] | None = None,
    should_stop: Callable[[], str | None] | None = None,
) -> SeriesResult:
    """Fetch all contracts in one series; merge + commit. Raises SessionExpired after commit attempt."""
    t0 = time.monotonic()
    today = _as_aware_ist(now).date()
    cache = untraded_cache if untraded_cache is not None else {}
    ensure_dir(staging_dir)
    spool_path = staging_dir / f"minute_{sanitize_underlying(series.underlying)}_{ymd(series.expiry)}.csv"
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

    fh, writer = _open_spool(spool_path)
    try:
        for contract in series.contracts:
            if should_stop is not None:
                reason = should_stop()
                if reason is not None:
                    interrupt_reason = reason
                    break
            state = fetch_states.get(contract.token)
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
                )
            except SessionExpired as exc:
                session_exc = exc
                break
            fh.flush()
            outcomes.append(outcome)
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
            minute_dir=minute_dir,
            staging_dir=staging_dir,
            now=now,
        )
        for o in outcomes:
            fetch_states[o.token] = FetchState(
                token=o.token,
                interval=INTERVAL,
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
            path=series_path(minute_dir, series.underlying, series.expiry),
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
) -> int:
    """How many contracts would be skipped by the untraded rule (for dry-run)."""
    cache = untraded_cache if untraded_cache is not None else {}
    n = 0
    for c in series.contracts:
        state = fetch_states.get(c.token)
        if state is None or state.fetched_to is None:
            continue
        if state.status == "gave_up":
            continue
        end = contract_end(now, c.expiry)
        start = planner.contract_range_start(state, now=now)
        if end <= start:
            continue
        if can_skip_untraded(con, c.token, start, end, untraded_cache=cache):
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
    minute_dir: Path = MINUTE_DIR,
    staging_dir: Path = STAGING_DIR,
    on_series: Callable[[int, int, SeriesResult], None] | None = None,
) -> FetchRunResult:
    """Process queue series in order. Handles Ctrl+C, budgets, lock retries."""
    now = now if now is not None else datetime.now(auth.IST)
    now = _as_aware_ist(now)
    stop_reason: list[str | None] = [None]
    interrupted = {"flag": False}

    def _handle_sigint(signum: int, frame: Any) -> None:
        interrupted["flag"] = True
        stop_reason[0] = "keyboard interrupt"

    prev_handler = signal.signal(signal.SIGINT, _handle_sigint)
    deadline = None if max_minutes is None else time.monotonic() + max_minutes * 60.0
    untraded_cache: dict[date, set[int] | None] = {}
    results: list[SeriesResult] = []
    locked: list[tuple[str, date, Series, list[ContractOutcome], Path]] = []
    series_list = list(queue_series)
    if max_series is not None:
        series_list = series_list[:max_series]
    total = len(series_list)

    def should_stop() -> str | None:
        if interrupted["flag"]:
            return stop_reason[0] or "keyboard interrupt"
        if deadline is not None and time.monotonic() >= deadline:
            return f"max-minutes budget ({max_minutes})"
        return None

    stopped: str | None = None
    try:
        for idx, series in enumerate(series_list, start=1):
            if should_stop() is not None:
                stopped = should_stop()
                break
            try:
                result = process_series(
                    client,
                    con,
                    series,
                    fetch_states,
                    now=now,
                    minute_dir=minute_dir,
                    staging_dir=staging_dir,
                    untraded_cache=untraded_cache,
                    should_stop=should_stop,
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
                                series_path(minute_dir, series.underlying, series.expiry),
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
                        series_path(minute_dir, series.underlying, series.expiry),
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
        signal.signal(signal.SIGINT, prev_handler)

    # Retry locked series once after a short sleep.
    still_locked: list[tuple[str, date]] = []
    if locked and stopped is None:
        time.sleep(LOCK_RETRY_SLEEP_S)
        for underlying, expiry, series, outcomes, _path in locked:
            spool = staging_dir / f"minute_{sanitize_underlying(series.underlying)}_{ymd(series.expiry)}.csv"
            try:
                path, row_count = commit_series(
                    con,
                    series=series,
                    outcomes=outcomes,
                    spool_csv=spool if spool.exists() else None,
                    minute_dir=minute_dir,
                    staging_dir=staging_dir,
                    now=now,
                )
                for o in outcomes:
                    fetch_states[o.token] = FetchState(
                        token=o.token,
                        interval=INTERVAL,
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
                    f"locked: {series_path(minute_dir, underlying, expiry)} "
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
