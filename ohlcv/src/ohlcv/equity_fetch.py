"""Day-candle download for NSE/BSE cash equity.

Bars land in that market's ``<id>_eq.duckdb``. Historical calls use interval=day
and oi=False. Backfill starts at 2000-01-01 and walks forward in
2000-calendar-day chunks. Each chunk is written to a staging CSV and bulk-loaded
in one short transaction. No transaction is open during the Kite call.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Callable, Sequence

import duckdb

from instruments.equity import EquityRow
from kitetools import auth

from . import planner
from .client import KiteClient, RequestRejected, RetryExhausted, SessionExpired
from .equity_catalog import (
    EquityFetchState,
    EquitySymbol,
    commit_chunk,
    ensure_schema,
    get_state,
)
from .fetch import _chunk_windows, classify_status
from .markets import MarketProfile, get_market
from .paths import ensure_dir

EQUITY_DAY_FLOOR = date(2000, 1, 1)

ProgressFn = Callable[..., None]


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


def _calendar_bounds(start: datetime, end: datetime) -> tuple[datetime, datetime]:
    """Inclusive calendar span of a chunk as a half-open naive timestamp range."""
    lo = datetime.combine(start.date(), time.min)
    hi = datetime.combine(end.date() + timedelta(days=1), time.min)
    return lo, hi


def _notify(on_progress: ProgressFn | None, **kwargs: Any) -> None:
    if on_progress is not None:
        on_progress(**kwargs)


def _symbol(row: EquityRow, now: datetime) -> EquitySymbol:
    return EquitySymbol(
        token=row.instrument_token,
        exchange_token=row.exchange_token,
        tradingsymbol=row.tradingsymbol,
        prev_tradingsymbol=row.prev_tradingsymbol,
        name=row.name,
        updated_at=_naive(now),
    )


def _state(
    *,
    token: int,
    status: str,
    fetched_to: datetime | None,
    attempts: int,
    last_error: str | None,
    now: datetime,
) -> EquityFetchState:
    return EquityFetchState(
        token=token,
        interval="day",
        fetched_to=_naive(fetched_to) if fetched_to is not None else None,
        status=status,
        rows_total=0,
        attempts=attempts,
        last_error=last_error,
        updated_at=_naive(now),
    )


def fetch_one(
    client: KiteClient,
    con: duckdb.DuckDBPyConnection,
    row: EquityRow,
    *,
    now: datetime,
    market: MarketProfile | str,
    staging_dir: Path,
    index: int,
    total: int,
    db_path: Path | None = None,
    floor: date | None = None,
    should_stop: Callable[[], str | None] | None = None,
    on_progress: ProgressFn | None = None,
) -> EquityFetchResult | None:
    """Fetch one symbol. Raises SessionExpired. Per-symbol Kite errors are recorded.

    Returns None when stopping before this symbol's first request, so the caller
    can leave it for the next run. A stop after a committed chunk returns the
    stored prefix.
    """
    profile = _resolve(market)
    now = _aware(now)
    end = planner.day_fetch_end(now, market=profile)
    state = get_state(con, row.instrument_token, "day")
    start = range_start(state, now=now, market=profile, floor=floor)
    prev_attempts = state.attempts if state is not None else 0
    prev_fetched = _aware(state.fetched_to) if state is not None and state.fetched_to is not None else None
    symbol = _symbol(row, now)

    def progress(rows: int, requests: int, status: str, *, finished: bool) -> None:
        _notify(
            on_progress,
            index=index,
            total=total,
            tradingsymbol=row.tradingsymbol,
            rows=rows,
            requests=requests,
            status=status,
            finished=finished,
        )

    if end <= start:
        watermark = end if prev_fetched is None else max(prev_fetched, end)
        status = classify_status(watermark, now, interval="day", market=profile)
        stored = commit_chunk(
            con,
            symbol=symbol,
            state=_state(
                token=row.instrument_token,
                status=status,
                fetched_to=watermark,
                attempts=prev_attempts,
                last_error=None,
                now=now,
            ),
            csv_path=None,
            range_start=None,
            range_end=None,
        )
        progress(stored, 0, status, finished=True)
        return EquityFetchResult(
            token=row.instrument_token,
            tradingsymbol=row.tradingsymbol,
            status=status,
            rows=stored,
            requests=0,
            skipped=True,
            error=None,
            path=db_path,
        )

    windows = _chunk_windows(start, end, chunk_days=planner.DAY_CHUNK_DAYS, market=profile)
    requests = 0
    stored = state.rows_total if state is not None else 0
    started = False
    stopped = False
    for chunk_i, (win_start, win_end) in enumerate(windows, start=1):
        if should_stop is not None and should_stop():
            stopped = True
            break
        label = f"chunk {chunk_i}/{len(windows)}"
        progress(stored, requests, label, finished=False)
        spool: Path | None = None
        try:
            candles = client.historical(
                row.instrument_token,
                _naive(win_start),
                _naive(win_end),
                "day",
                oi=False,
            )
        except SessionExpired:
            raise
        except (RequestRejected, RetryExhausted) as exc:
            attempts = prev_attempts + 1
            stored = commit_chunk(
                con,
                symbol=symbol,
                state=_state(
                    token=row.instrument_token,
                    status="error",
                    fetched_to=prev_fetched,
                    attempts=attempts,
                    last_error=str(exc)[:500],
                    now=now,
                ),
                csv_path=None,
                range_start=None,
                range_end=None,
            )
            progress(stored, requests, "error", finished=True)
            return EquityFetchResult(
                token=row.instrument_token,
                tradingsymbol=row.tradingsymbol,
                status="error",
                rows=stored,
                requests=requests,
                skipped=False,
                error=str(exc)[:500],
                path=db_path,
            )
        started = True
        requests += 1
        bars = []
        for candle in candles:
            parsed = _bar(row.instrument_token, candle, end=end)
            if parsed is not None:
                bars.append(parsed)
        last = win_end >= end
        watermark = end if last else win_end
        status = (
            classify_status(end, now, interval="day", market=profile) if last else "done"
        )
        try:
            if bars:
                spool = staging_dir / f"day_{row.instrument_token}_{chunk_i}.csv"
                _write_spool(spool, bars)
                lo, hi = _calendar_bounds(win_start, win_end)
            else:
                lo, hi = None, None
            stored = commit_chunk(
                con,
                symbol=symbol,
                state=_state(
                    token=row.instrument_token,
                    status=status,
                    fetched_to=watermark,
                    attempts=prev_attempts,
                    last_error=None,
                    now=now,
                ),
                csv_path=spool,
                range_start=lo,
                range_end=hi,
            )
            prev_fetched = watermark
        finally:
            if spool is not None:
                try:
                    spool.unlink(missing_ok=True)
                except OSError:
                    pass
        progress(stored, requests, label, finished=False)

    if stopped and not started:
        return None
    final = get_state(con, row.instrument_token, "day")
    status = final.status if final is not None else "done"
    rows = final.rows_total if final is not None else stored
    if stopped:
        status_label = "stopped"
    else:
        status_label = status
    progress(rows, requests, status_label, finished=True)
    return EquityFetchResult(
        token=row.instrument_token,
        tradingsymbol=row.tradingsymbol,
        status=status,
        rows=rows,
        requests=requests,
        skipped=False,
        error=None,
        path=db_path,
    )


def fetch_equity(
    client: KiteClient,
    con: duckdb.DuckDBPyConnection,
    rows: Sequence[EquityRow],
    *,
    now: datetime,
    market: MarketProfile | str,
    staging_dir: Path,
    db_path: Path | None = None,
    floor: date | None = None,
    should_stop: Callable[[], str | None] | None = None,
    on_progress: ProgressFn | None = None,
) -> list[EquityFetchResult]:
    """Fetch day candles for ``rows``. Stops cleanly when ``should_stop`` returns a reason.

    The current chunk is committed before the stop takes effect. The next run
    continues from that watermark.
    """
    ensure_schema(con)
    out: list[EquityFetchResult] = []
    total = len(rows)
    for index, row in enumerate(rows, start=1):
        if should_stop is not None:
            reason = should_stop()
            if reason:
                break
        item = fetch_one(
            client,
            con,
            row,
            now=now,
            market=market,
            staging_dir=staging_dir,
            index=index,
            total=total,
            db_path=db_path,
            floor=floor,
            should_stop=should_stop,
            on_progress=on_progress,
        )
        if item is None:
            break
        out.append(item)
    return out
