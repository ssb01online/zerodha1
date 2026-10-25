"""Underlying ranking from quote snapshots (preferred) or nearest-FUT day candles."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import duckdb

from kitetools import auth

from .catalog import (
    UnderlyingRank,
    latest_rank_trade_date,
    latest_snapshot,
    list_underlying_ranks,
    upsert_underlying_ranks,
)
from .client import KiteClient, RequestRejected, RetryExhausted
from .paths import DAILY_DIR, ymd
from .universe import nearest_fut_tokens

RANK_LOOKBACK_DAYS = 10


@dataclass(frozen=True)
class RankRefreshResult:
    trade_date: date | None
    upserted: int
    skipped: bool
    errors: list[str]
    source: str = "fut_day"


def _today_ist() -> date:
    return datetime.now(auth.IST).date()


def _candle_date(candle: dict[str, Any]) -> date | None:
    d = candle.get("date")
    if d is None:
        return None
    if isinstance(d, datetime):
        return d.date()
    if isinstance(d, date):
        return d
    try:
        return datetime.fromisoformat(str(d)).date()
    except ValueError:
        return None


def _latest_session_candle(candles: list[dict[str, Any]]) -> dict[str, Any] | None:
    best: dict[str, Any] | None = None
    best_day: date | None = None
    for c in candles:
        day = _candle_date(c)
        if day is None:
            continue
        if best_day is None or day > best_day:
            best = c
            best_day = day
    return best


def rank_from_snapshot(
    con: duckdb.DuckDBPyConnection,
    trade_date: date,
    *,
    dest_dir: Path | None = None,
) -> RankRefreshResult:
    """Rank underlyings from daily/yyyymmdd.parquet (sum of volume * average_price)."""
    root = dest_dir if dest_dir is not None else DAILY_DIR
    path = root / f"{ymd(trade_date)}.parquet"
    if not path.is_file():
        return RankRefreshResult(
            trade_date=None,
            upserted=0,
            skipped=False,
            errors=[f"snapshot not found: {path}"],
            source="quote",
        )

    mem = duckdb.connect()
    try:
        mem.execute("SET memory_limit='4GB'")
        rows = mem.execute(
            """
            SELECT underlying, volume, average_price, close, instrument_type, token, expiry
            FROM read_parquet(?)
            """,
            [str(path)],
        ).fetchall()
    finally:
        mem.close()

    # Aggregate traded value / volume; track nearest FUT for fut_token/close.
    agg_value: dict[str, float] = defaultdict(float)
    agg_volume: dict[str, int] = defaultdict(int)
    nearest_fut: dict[str, tuple[date, int, float]] = {}

    for underlying, volume, avg_price, close, itype, token, expiry in rows:
        ul = str(underlying)
        vol = int(volume or 0)
        avg = float(avg_price or 0.0)
        agg_value[ul] += float(vol) * avg
        agg_volume[ul] += vol
        if str(itype) != "FUT":
            continue
        exp = expiry if isinstance(expiry, date) else date.fromisoformat(str(expiry)[:10])
        close_f = float(close or 0.0)
        prev = nearest_fut.get(ul)
        if prev is None or exp < prev[0]:
            nearest_fut[ul] = (exp, int(token), close_f)

    ranks: list[UnderlyingRank] = []
    for ul in sorted(agg_value):
        fut = nearest_fut.get(ul)
        fut_token = fut[1] if fut else 0
        fut_close = fut[2] if fut else 0.0
        ranks.append(
            UnderlyingRank(
                trade_date=trade_date,
                underlying=ul,
                fut_token=fut_token,
                volume=agg_volume[ul],
                close=fut_close,
                traded_value=agg_value[ul],
                source="quote",
            )
        )
    upserted = upsert_underlying_ranks(con, ranks)
    return RankRefreshResult(
        trade_date=trade_date,
        upserted=upserted,
        skipped=False,
        errors=[],
        source="quote",
    )


def _refresh_from_fut_day(
    client: KiteClient,
    con: duckdb.DuckDBPyConnection,
    today: date,
) -> RankRefreshResult:
    futs = nearest_fut_tokens(con, today)
    if not futs:
        return RankRefreshResult(
            trade_date=None,
            upserted=0,
            skipped=False,
            errors=["no live FUTs"],
            source="fut_day",
        )

    start = today - timedelta(days=RANK_LOOKBACK_DAYS - 1)
    ranks: list[UnderlyingRank] = []
    errors: list[str] = []
    session_dates: set[date] = set()

    for underlying, fut in sorted(futs.items()):
        try:
            candles = client.historical(
                fut.token,
                start.isoformat(),
                today.isoformat(),
                "day",
                oi=True,
            )
        except (RequestRejected, RetryExhausted) as exc:
            errors.append(f"{underlying}/{fut.tradingsymbol}: {exc}")
            continue
        latest = _latest_session_candle(candles)
        if latest is None:
            errors.append(f"{underlying}/{fut.tradingsymbol}: no day candles")
            continue
        trade_date = _candle_date(latest)
        if trade_date is None:
            errors.append(f"{underlying}/{fut.tradingsymbol}: bad candle date")
            continue
        volume = int(latest.get("volume") or 0)
        close = float(latest.get("close") or 0.0)
        ranks.append(
            UnderlyingRank(
                trade_date=trade_date,
                underlying=underlying,
                fut_token=fut.token,
                volume=volume,
                close=close,
                traded_value=float(volume) * close,
                source="fut_day",
            )
        )
        session_dates.add(trade_date)

    if not ranks:
        return RankRefreshResult(
            trade_date=None,
            upserted=0,
            skipped=False,
            errors=errors,
            source="fut_day",
        )

    max_day = max(session_dates)
    kept = [r for r in ranks if r.trade_date == max_day]
    upserted = upsert_underlying_ranks(con, kept)
    return RankRefreshResult(
        trade_date=max_day,
        upserted=upserted,
        skipped=False,
        errors=errors,
        source="fut_day",
    )


def refresh_rank(
    client: KiteClient | None,
    con: duckdb.DuckDBPyConnection,
    today: date | None = None,
    *,
    force: bool = False,
    dest_dir: Path | None = None,
) -> RankRefreshResult:
    """Prefer a final quote snapshot; else fall back to nearest-FUT day candles.

    Skips work when a rank for a recent session already exists, unless force=True.
    When a final snapshot exists for the latest session, no Kite calls are made
    (client may be None).
    """
    today = today if today is not None else _today_ist()
    existing_date = latest_rank_trade_date(con)
    if (
        not force
        and existing_date is not None
        and existing_date >= today - timedelta(days=RANK_LOOKBACK_DAYS)
    ):
        return RankRefreshResult(
            trade_date=existing_date,
            upserted=0,
            skipped=True,
            errors=[],
            source="cached",
        )

    snap = latest_snapshot(con, status="final")
    if snap is not None and snap.trade_date >= today - timedelta(days=RANK_LOOKBACK_DAYS):
        return rank_from_snapshot(con, snap.trade_date, dest_dir=dest_dir)

    if client is None:
        return RankRefreshResult(
            trade_date=None,
            upserted=0,
            skipped=False,
            errors=["no final snapshot and no Kite client for fut_day fallback"],
            source="fut_day",
        )
    return _refresh_from_fut_day(client, con, today)


def load_ranks(con: duckdb.DuckDBPyConnection) -> dict[str, UnderlyingRank]:
    return list_underlying_ranks(con)
