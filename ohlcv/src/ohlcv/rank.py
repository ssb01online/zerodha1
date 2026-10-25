"""Underlying ranking from nearest-FUT day candles (weekend-safe)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

import duckdb

from kitetools import auth

from .catalog import (
    UnderlyingRank,
    latest_rank_trade_date,
    list_underlying_ranks,
    upsert_underlying_ranks,
)
from .client import KiteClient, RequestRejected, RetryExhausted
from .universe import nearest_fut_tokens

RANK_LOOKBACK_DAYS = 10


@dataclass(frozen=True)
class RankRefreshResult:
    trade_date: date | None
    upserted: int
    skipped: bool
    errors: list[str]


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


def refresh_rank(
    client: KiteClient,
    con: duckdb.DuckDBPyConnection,
    today: date | None = None,
    *,
    force: bool = False,
) -> RankRefreshResult:
    """Fetch nearest-FUT day candles and store traded_value = volume * close.

    Skips Kite calls when a rank for the latest completed session already exists,
    unless force=True. "Latest completed session" is inferred as the max trade_date
    already stored when force is False and that date is within the lookback window
    relative to today (i.e. ranks are fresh enough for queue ordering).
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
        )

    futs = nearest_fut_tokens(con, today)
    if not futs:
        return RankRefreshResult(trade_date=None, upserted=0, skipped=False, errors=["no live FUTs"])

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

    # Prefer the most common latest session date so one stale FUT doesn't split ranks.
    if not ranks:
        return RankRefreshResult(trade_date=None, upserted=0, skipped=False, errors=errors)

    # Keep rows whose trade_date equals the max observed (latest session across underlyings).
    max_day = max(session_dates)
    kept = [r for r in ranks if r.trade_date == max_day]
    upserted = upsert_underlying_ranks(con, kept)
    return RankRefreshResult(
        trade_date=max_day,
        upserted=upserted,
        skipped=False,
        errors=errors,
    )


def load_ranks(con: duckdb.DuckDBPyConnection) -> dict[str, UnderlyingRank]:
    return list_underlying_ranks(con)
