"""Continuous day-candle helper for one underlying (nearest live FUT token)."""

from __future__ import annotations

import csv
import os
import tempfile
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import duckdb

from kitetools import auth

from .catalog import ContractRow, list_contracts
from .client import KiteClient
from .markets import MarketProfile
from .paths import ensure_dir, paths_for, sanitize_underlying
from .universe import resolve_market

DEFAULT_LOOKBACK_DAYS = 2000


class ContinuousRejected(Exception):
    """Continuous fetch refused (no live FUT / market disabled / empty)."""


@dataclass(frozen=True)
class ContinuousResult:
    underlying: str
    market_id: str
    token: int
    tradingsymbol: str
    from_date: date
    to_date: date
    rows: int
    path: Path


def _today_ist() -> date:
    return datetime.now(auth.IST).date()


def _as_date(value: Any) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return datetime.fromisoformat(str(value)[:10]).date()
    except ValueError:
        return None


def nearest_live_fut(
    con: duckdb.DuckDBPyConnection,
    underlying: str,
    *,
    seen_on: date | None = None,
) -> ContractRow | None:
    """Nearest expiry FUT for underlying with last_seen == seen_on (default today)."""
    seen_on = seen_on if seen_on is not None else _today_ist()
    name = (underlying or "").strip().upper()
    futs = [
        c
        for c in list_contracts(con)
        if c.instrument_type == "FUT"
        and (c.underlying or "").strip().upper() == name
        and c.last_seen == seen_on
    ]
    if not futs:
        return None
    return sorted(futs, key=lambda c: c.expiry)[0]


def continuous_path(market_id: str, underlying: str) -> Path:
    root = paths_for(market_id).continuous_dir
    return root / f"{sanitize_underlying(underlying)}.parquet"


def _write_continuous_parquet(rows: list[dict[str, Any]], dest: Path) -> None:
    ensure_dir(dest.parent)
    staging = dest.parent / f"._{dest.name}.tmp"
    fd, tmp_name = tempfile.mkstemp(prefix="ohlcv_cont_", suffix=".csv")
    tmp_path = Path(tmp_name)
    cols = ["date", "open", "high", "low", "close", "volume", "oi"]
    try:
        with open(fd, "w", encoding="utf-8", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=cols)
            writer.writeheader()
            for row in rows:
                writer.writerow({c: row.get(c, "") for c in cols})
        con = duckdb.connect()
        try:
            con.execute("SET memory_limit='4GB'")
            con.execute(
                """
                COPY (
                    SELECT
                        TRY_CAST(date AS TIMESTAMP) AS date,
                        CAST(open AS DOUBLE) AS open,
                        CAST(high AS DOUBLE) AS high,
                        CAST(low AS DOUBLE) AS low,
                        CAST(close AS DOUBLE) AS close,
                        CAST(volume AS BIGINT) AS volume,
                        TRY_CAST(oi AS DOUBLE) AS oi
                    FROM read_csv(?, header = true, auto_detect = true, nullstr = '')
                    ORDER BY date
                ) TO ? (FORMAT parquet, COMPRESSION zstd)
                """,
                [str(tmp_path), str(staging)],
            )
        finally:
            con.close()
        os.replace(staging, dest)
    finally:
        try:
            tmp_path.unlink()
        except OSError:
            pass
        if staging.exists():
            try:
                staging.unlink()
            except OSError:
                pass


def fetch_continuous(
    client: KiteClient,
    con: duckdb.DuckDBPyConnection,
    underlying: str,
    *,
    market: MarketProfile | str | None = None,
    from_date: date | None = None,
    to_date: date | None = None,
    seen_on: date | None = None,
    dest_path: Path | None = None,
) -> ContinuousResult:
    """Fetch continuous day candles for nearest live FUT; write parquet."""
    profile = resolve_market(market)
    if not profile.continuous_day:
        raise ContinuousRejected(
            f"market {profile.id} does not support continuous day candles"
        )
    name = (underlying or "").strip().upper()
    if not name:
        raise ContinuousRejected("underlying is required")

    fut = nearest_live_fut(con, name, seen_on=seen_on)
    if fut is None:
        raise ContinuousRejected(
            f"no live FUT for underlying={name!r} on market={profile.id}"
        )

    end = to_date if to_date is not None else _today_ist() - timedelta(days=1)
    start = (
        from_date
        if from_date is not None
        else end - timedelta(days=DEFAULT_LOOKBACK_DAYS - 1)
    )
    if start > end:
        raise ContinuousRejected(f"from_date {start} is after to_date {end}")

    candles = client.historical(
        fut.token,
        start.isoformat(),
        end.isoformat(),
        "day",
        oi=True,
        continuous=True,
    )
    if not candles:
        raise ContinuousRejected(
            f"empty continuous response for {fut.tradingsymbol} "
            f"({start}..{end})"
        )

    rows: list[dict[str, Any]] = []
    for c in candles:
        ts = c.get("date")
        if isinstance(ts, datetime):
            date_s = ts.isoformat(sep=" ")
        else:
            date_s = str(ts)
        oi_raw = c.get("oi")
        if oi_raw is None:
            oi_raw = c.get("OI")
        rows.append(
            {
                "date": date_s,
                "open": float(c["open"]),
                "high": float(c["high"]),
                "low": float(c["low"]),
                "close": float(c["close"]),
                "volume": int(c.get("volume") or 0),
                "oi": "" if oi_raw is None else float(oi_raw),
            }
        )

    path = dest_path if dest_path is not None else continuous_path(profile.id, name)
    _write_continuous_parquet(rows, path)
    first = _as_date(candles[0].get("date")) or start
    last = _as_date(candles[-1].get("date")) or end
    return ContinuousResult(
        underlying=name,
        market_id=profile.id,
        token=fut.token,
        tradingsymbol=fut.tradingsymbol,
        from_date=first,
        to_date=last,
        rows=len(rows),
        path=path,
    )
