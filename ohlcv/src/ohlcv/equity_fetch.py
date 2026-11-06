"""Day-candle download for NSE/BSE cash equity.

One parquet per instrument token. Historical calls use interval=day and oi=False.
Backfill starts at 2000-01-01 and walks forward in 2000-calendar-day chunks.
"""

from __future__ import annotations

import csv
import os
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Sequence

import duckdb

from instruments.equity import EquityRow
from kitetools import auth

from . import planner
from .client import KiteClient, RequestRejected, RetryExhausted, SessionExpired
from .equity_catalog import EquityFetchState, ensure_schema, get_state, upsert_state
from .fetch import _chunk_windows, classify_status
from .markets import MarketProfile, get_market
from .paths import ensure_dir

EQUITY_DAY_FLOOR = date(2000, 1, 1)
_PARQUET_COLUMNS = (
    "token",
    "tradingsymbol",
    "ts",
    "open",
    "high",
    "low",
    "close",
    "volume",
)


@dataclass(frozen=True)
class EquityFetchResult:
    token: int
    tradingsymbol: str
    status: str
    rows: int
    requests: int
    skipped: bool
    error: str | None
    path: Path | None


def day_path(day_dir: Path, token: int) -> Path:
    return day_dir / f"{int(token)}.parquet"


def _resolve(market: MarketProfile | str) -> MarketProfile:
    if isinstance(market, MarketProfile):
        return market
    return get_market(market)


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=auth.IST)
    return value.astimezone(auth.IST)


def _naive(value: datetime) -> datetime:
    return _aware(value).replace(tzinfo=None)


def range_start(
    state: EquityFetchState | None,
    *,
    now: datetime,
    market: MarketProfile | str,
    floor: date | None = None,
) -> datetime:
    """First timestamp still needed. A finished watermark resumes the next session."""
    profile = _resolve(market)
    floor_day = floor if floor is not None else EQUITY_DAY_FLOOR
    floor_dt = datetime.combine(floor_day, profile.session_start, tzinfo=auth.IST)
    if state is None or state.fetched_to is None:
        return floor_dt
    fetched = _aware(state.fetched_to)
    if state.status == "partial":
        return max(planner.day_partial_rollback(fetched, market=profile), floor_dt)
    if fetched.time() >= profile.session_end:
        nxt = fetched.date() + timedelta(days=1)
        return datetime.combine(nxt, profile.session_start, tzinfo=auth.IST)
    return max(fetched, floor_dt)


def _candle_ts(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return _naive(value) if value.tzinfo is not None else value
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day)
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        return _naive(parsed)
    return parsed


def _bar(token: int, candle: dict[str, Any], *, end: datetime) -> dict[str, Any] | None:
    ts = _candle_ts(candle.get("date"))
    if ts is None or ts.date() > end.date():
        return None
    return {
        "token": token,
        "ts": ts.isoformat(sep=" "),
        "open": float(candle["open"]),
        "high": float(candle["high"]),
        "low": float(candle["low"]),
        "close": float(candle["close"]),
        "volume": int(candle.get("volume") or 0),
    }


def _write_spool(path: Path, rows: list[dict[str, Any]]) -> None:
    ensure_dir(path.parent)
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=("token", "ts", "open", "high", "low", "close", "volume"))
        writer.writeheader()
        writer.writerows(rows)


def merge_day_parquet(
    *,
    existing: Path | None,
    spool_csv: Path | None,
    dest: Path,
    staging_dir: Path,
    tradingsymbol: str,
) -> int:
    """Merge spool bars into dest and stamp every row with the current symbol."""
    ensure_dir(dest.parent)
    ensure_dir(staging_dir)
    staging = staging_dir / f"{dest.stem}.parquet"
    has_existing = existing is not None and existing.is_file() and existing.stat().st_size > 0
    has_spool = spool_csv is not None and spool_csv.is_file() and spool_csv.stat().st_size > 0
    if not has_existing and not has_spool:
        return 0

    parts: list[str] = []
    params: list[Any] = []
    if has_existing:
        parts.append(
            """
            SELECT CAST(token AS BIGINT) AS token, CAST(ts AS TIMESTAMP) AS ts,
                   CAST(open AS DOUBLE) AS open, CAST(high AS DOUBLE) AS high,
                   CAST(low AS DOUBLE) AS low, CAST(close AS DOUBLE) AS close,
                   CAST(volume AS BIGINT) AS volume, 0 AS src
            FROM read_parquet(?)
            """
        )
        params.append(str(existing))
    if has_spool:
        parts.append(
            """
            SELECT CAST(token AS BIGINT) AS token, CAST(ts AS TIMESTAMP) AS ts,
                   CAST(open AS DOUBLE) AS open, CAST(high AS DOUBLE) AS high,
                   CAST(low AS DOUBLE) AS low, CAST(close AS DOUBLE) AS close,
                   CAST(volume AS BIGINT) AS volume, 1 AS src
            FROM read_csv(?, header = true, auto_detect = true)
            """
        )
        params.append(str(spool_csv))
    union_sql = " UNION ALL ".join(parts)
    sql = f"""
        COPY (
            SELECT token, ? AS tradingsymbol, ts, open, high, low, close, volume
            FROM (
                SELECT token, ts, open, high, low, close, volume, src,
                       row_number() OVER (PARTITION BY ts ORDER BY src DESC) AS rn
                FROM ({union_sql})
            )
            WHERE rn = 1
            ORDER BY ts
        ) TO ? (FORMAT parquet, COMPRESSION zstd)
    """
    con = duckdb.connect()
    try:
        con.execute(sql, [tradingsymbol, *params, str(staging)])
        count = con.execute("SELECT COUNT(*) FROM read_parquet(?)", [str(staging)]).fetchone()
    finally:
        con.close()
    os.replace(staging, dest)
    return int(count[0]) if count else 0


def read_day_bars(path: Path, *, tail: int | None = None) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    limit_sql = ""
    params: list[Any] = [str(path)]
    order = "ASC"
    if tail is not None:
        order = "DESC"
        limit_sql = " LIMIT ?"
        params.append(int(tail))
    con = duckdb.connect()
    try:
        found = con.execute(
            f"""
            SELECT token, tradingsymbol, ts, open, high, low, close, volume
            FROM read_parquet(?)
            ORDER BY ts {order}
            {limit_sql}
            """,
            params,
        ).fetchall()
    finally:
        con.close()
    rows = [
        {
            "token": int(rec[0]),
            "tradingsymbol": str(rec[1]),
            "ts": rec[2],
            "open": float(rec[3]),
            "high": float(rec[4]),
            "low": float(rec[5]),
            "close": float(rec[6]),
            "volume": int(rec[7]),
        }
        for rec in found
    ]
    if tail is not None:
        rows.reverse()
    return rows


def _save(
    con: duckdb.DuckDBPyConnection,
    *,
    token: int,
    status: str,
    fetched_to: datetime | None,
    rows: int,
    attempts: int,
    last_error: str | None,
    now: datetime,
) -> None:
    upsert_state(
        con,
        EquityFetchState(
            token=token,
            interval="day",
            fetched_to=_naive(fetched_to) if fetched_to is not None else None,
            status=status,
            rows_total=rows,
            attempts=attempts,
            last_error=last_error,
            updated_at=_naive(now),
        ),
    )


def fetch_one(
    client: KiteClient,
    con: duckdb.DuckDBPyConnection,
    row: EquityRow,
    *,
    now: datetime,
    market: MarketProfile | str,
    day_dir: Path,
    staging_dir: Path,
    floor: date | None = None,
) -> EquityFetchResult:
    """Fetch one symbol. Raises SessionExpired. Per-symbol Kite errors are recorded."""
    profile = _resolve(market)
    now = _aware(now)
    end = planner.day_fetch_end(now, market=profile)
    state = get_state(con, row.instrument_token, "day")
    start = range_start(state, now=now, market=profile, floor=floor)
    prev_rows = state.rows_total if state is not None else 0
    prev_attempts = state.attempts if state is not None else 0
    prev_fetched = _aware(state.fetched_to) if state is not None and state.fetched_to is not None else None
    dest = day_path(day_dir, row.instrument_token)

    if end <= start:
        watermark = end if prev_fetched is None else max(prev_fetched, end)
        status = classify_status(watermark, now, interval="day", market=profile)
        _save(
            con,
            token=row.instrument_token,
            status=status,
            fetched_to=watermark,
            rows=prev_rows,
            attempts=prev_attempts,
            last_error=None,
            now=now,
        )
        return EquityFetchResult(
            token=row.instrument_token,
            tradingsymbol=row.tradingsymbol,
            status=status,
            rows=prev_rows,
            requests=0,
            skipped=True,
            error=None,
            path=dest if dest.is_file() else None,
        )

    windows = _chunk_windows(start, end, chunk_days=planner.DAY_CHUNK_DAYS, market=profile)
    bars: list[dict[str, Any]] = []
    requests = 0
    try:
        for win_start, win_end in windows:
            candles = client.historical(
                row.instrument_token,
                _naive(win_start),
                _naive(win_end),
                "day",
                oi=False,
            )
            requests += 1
            for candle in candles:
                parsed = _bar(row.instrument_token, candle, end=end)
                if parsed is not None:
                    bars.append(parsed)
    except SessionExpired:
        raise
    except (RequestRejected, RetryExhausted) as exc:
        attempts = prev_attempts + 1
        _save(
            con,
            token=row.instrument_token,
            status="error",
            fetched_to=prev_fetched,
            rows=prev_rows,
            attempts=attempts,
            last_error=str(exc)[:500],
            now=now,
        )
        return EquityFetchResult(
            token=row.instrument_token,
            tradingsymbol=row.tradingsymbol,
            status="error",
            rows=prev_rows,
            requests=requests,
            skipped=False,
            error=str(exc)[:500],
            path=dest if dest.is_file() else None,
        )

    spool: Path | None = None
    if bars:
        spool = staging_dir / f"day_{row.instrument_token}.csv"
        _write_spool(spool, bars)
    try:
        written = merge_day_parquet(
            existing=dest if dest.is_file() else None,
            spool_csv=spool,
            dest=dest,
            staging_dir=staging_dir,
            tradingsymbol=row.tradingsymbol,
        )
    finally:
        if spool is not None:
            try:
                spool.unlink(missing_ok=True)
            except OSError:
                pass

    status = classify_status(end, now, interval="day", market=profile)
    total_rows = written if written else prev_rows
    _save(
        con,
        token=row.instrument_token,
        status=status,
        fetched_to=end,
        rows=total_rows,
        attempts=prev_attempts,
        last_error=None,
        now=now,
    )
    return EquityFetchResult(
        token=row.instrument_token,
        tradingsymbol=row.tradingsymbol,
        status=status,
        rows=total_rows,
        requests=requests,
        skipped=False,
        error=None,
        path=dest if dest.is_file() else None,
    )


def fetch_equity(
    client: KiteClient,
    con: duckdb.DuckDBPyConnection,
    rows: Sequence[EquityRow],
    *,
    now: datetime,
    market: MarketProfile | str,
    day_dir: Path,
    staging_dir: Path,
    floor: date | None = None,
    should_stop: Callable[[], str | None] | None = None,
) -> list[EquityFetchResult]:
    """Fetch day candles for ``rows``. Stops cleanly when ``should_stop`` returns a reason."""
    ensure_schema(con)
    out: list[EquityFetchResult] = []
    for row in rows:
        if should_stop is not None:
            reason = should_stop()
            if reason:
                break
        out.append(
            fetch_one(
                client,
                con,
                row,
                now=now,
                market=market,
                day_dir=day_dir,
                staging_dir=staging_dir,
                floor=floor,
            )
        )
    return out
