"""Pre-open quote() sweep of NSE/BSE equity."""

from __future__ import annotations

import csv
import os
import tempfile
import time
from dataclasses import dataclass
from datetime import date, datetime, time as dt_time
from pathlib import Path
from typing import Any, Callable, Sequence

import duckdb

from kitetools import auth

from . import catalog as catalog_mod
from .client import KiteClient
from .paths import CATALOG_PATH, PREOPEN_DIR, STAGING_DIR, ensure_dir, snapshot_path, ymd
from .universe import EquityRow, list_equity

QUOTE_BATCH_SIZE = 500
QUOTE_PACE_S = 1.0
DEFAULT_CAPTURE_AT = dt_time(9, 8, 30)


class CaptureRejected(Exception):
    """Capture refused (empty universe / no auction volume)."""


@dataclass(frozen=True)
class CaptureDryRun:
    equities: int
    batches: int
    estimated_seconds: float
    wait_until: datetime | None
    wait_seconds: float


@dataclass(frozen=True)
class CaptureResult:
    trade_date: date
    path: Path
    equities: int
    traded_rows: int
    missing: int
    total_volume: int
    total_traded_value: float
    taken_at: datetime


def _today_ist() -> date:
    return datetime.now(auth.IST).date()


def _now_ist() -> datetime:
    return datetime.now(auth.IST)


def parse_at(text: str) -> dt_time:
    """Parse HH:MM or HH:MM:SS into a time."""
    parts = text.strip().split(":")
    if len(parts) not in (2, 3):
        raise ValueError(f"invalid --at time {text!r}; expected HH:MM[:SS]")
    try:
        hour = int(parts[0])
        minute = int(parts[1])
        second = int(parts[2]) if len(parts) == 3 else 0
    except ValueError as exc:
        raise ValueError(f"invalid --at time {text!r}; expected HH:MM[:SS]") from exc
    if not (0 <= hour <= 23 and 0 <= minute <= 59 and 0 <= second <= 59):
        raise ValueError(f"invalid --at time {text!r}")
    return dt_time(hour, minute, second)


def target_datetime(
    at: dt_time,
    *,
    now: datetime | None = None,
) -> datetime:
    now = now if now is not None else _now_ist()
    if now.tzinfo is None:
        now = now.replace(tzinfo=auth.IST)
    else:
        now = now.astimezone(auth.IST)
    return datetime.combine(now.date(), at, tzinfo=auth.IST)


def wait_until(
    at: dt_time,
    *,
    now: datetime | None = None,
    sleep: Callable[[float], None] = time.sleep,
    on_wait: Callable[[float, datetime], None] | None = None,
) -> datetime:
    """Block until today's ``at`` IST (or return immediately if already past)."""
    now = now if now is not None else _now_ist()
    if now.tzinfo is None:
        now = now.replace(tzinfo=auth.IST)
    else:
        now = now.astimezone(auth.IST)
    target = target_datetime(at, now=now)
    remaining = (target - now).total_seconds()
    if remaining <= 0:
        return now
    if on_wait is not None:
        on_wait(remaining, target)
    # Sleep in chunks so Ctrl+C stays responsive.
    end = time.monotonic() + remaining
    while True:
        left = end - time.monotonic()
        if left <= 0:
            break
        sleep(min(left, 1.0))
    return _now_ist()


def estimate_dry_run(
    equities: Sequence[EquityRow],
    *,
    at: dt_time = DEFAULT_CAPTURE_AT,
    now: datetime | None = None,
    skip_wait: bool = False,
) -> CaptureDryRun:
    n = len(equities)
    batches = (n + QUOTE_BATCH_SIZE - 1) // QUOTE_BATCH_SIZE if n else 0
    now = now if now is not None else _now_ist()
    if now.tzinfo is None:
        now = now.replace(tzinfo=auth.IST)
    else:
        now = now.astimezone(auth.IST)
    target = None if skip_wait else target_datetime(at, now=now)
    wait_seconds = 0.0
    if target is not None:
        wait_seconds = max(0.0, (target - now).total_seconds())
    return CaptureDryRun(
        equities=n,
        batches=batches,
        estimated_seconds=batches * QUOTE_PACE_S,
        wait_until=target if wait_seconds > 0 else None,
        wait_seconds=wait_seconds,
    )


def _parse_trade_ts(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=auth.IST)
        return value.astimezone(auth.IST)
    text = str(value).strip()
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=auth.IST)
    return dt.astimezone(auth.IST)


def _row_from_quote(
    equity: EquityRow,
    quote: dict[str, Any],
    *,
    trade_date: date,
    taken_at: datetime,
) -> dict[str, Any]:
    ohlc = quote.get("ohlc") or {}
    volume = int(quote.get("volume") or 0)
    avg = float(quote.get("average_price") or 0.0)
    last_price = quote.get("last_price")
    ltt = _parse_trade_ts(quote.get("last_trade_time"))
    qts = _parse_trade_ts(quote.get("timestamp"))
    taken = taken_at.astimezone(auth.IST) if taken_at.tzinfo else taken_at.replace(
        tzinfo=auth.IST
    )
    return {
        "trade_date": trade_date.isoformat(),
        "exchange": equity.exchange,
        "tradingsymbol": equity.tradingsymbol,
        "token": equity.token,
        "name": equity.name,
        "last_price": float(last_price) if last_price is not None else None,
        "average_price": avg,
        "volume": volume,
        "traded_value": float(volume) * avg,
        "ohlc_open": float(ohlc["open"]) if ohlc.get("open") is not None else None,
        "ohlc_high": float(ohlc["high"]) if ohlc.get("high") is not None else None,
        "ohlc_low": float(ohlc["low"]) if ohlc.get("low") is not None else None,
        "ohlc_close": float(ohlc["close"]) if ohlc.get("close") is not None else None,
        "buy_quantity": int(quote.get("buy_quantity") or 0),
        "sell_quantity": int(quote.get("sell_quantity") or 0),
        "last_trade_time": ltt.isoformat() if ltt is not None else None,
        "quote_timestamp": qts.isoformat() if qts is not None else None,
        "taken_at": taken.replace(tzinfo=None).isoformat(sep=" "),
    }


_CSV_COLUMNS = [
    "trade_date",
    "exchange",
    "tradingsymbol",
    "token",
    "name",
    "last_price",
    "average_price",
    "volume",
    "traded_value",
    "ohlc_open",
    "ohlc_high",
    "ohlc_low",
    "ohlc_close",
    "buy_quantity",
    "sell_quantity",
    "last_trade_time",
    "quote_timestamp",
    "taken_at",
]


def _write_parquet(
    rows: list[dict[str, Any]],
    dest: Path,
    *,
    staging_dir: Path | None = None,
) -> None:
    ensure_dir(dest.parent)
    stage_root = staging_dir if staging_dir is not None else STAGING_DIR
    ensure_dir(stage_root)
    staging = stage_root / f"preopen_{dest.stem}.parquet"
    fd, tmp_name = tempfile.mkstemp(prefix="preopen_", suffix=".csv")
    tmp_path = Path(tmp_name)
    try:
        with open(fd, "w", encoding="utf-8", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=_CSV_COLUMNS)
            writer.writeheader()
            for row in rows:
                out = {}
                for col in _CSV_COLUMNS:
                    val = row.get(col)
                    out[col] = "" if val is None else val
                writer.writerow(out)

        con = duckdb.connect()
        try:
            con.execute("SET memory_limit='4GB'")
            con.execute(
                """
                COPY (
                    SELECT
                        CAST(trade_date AS DATE) AS trade_date,
                        exchange,
                        tradingsymbol,
                        CAST(token AS BIGINT) AS token,
                        name,
                        TRY_CAST(last_price AS DOUBLE) AS last_price,
                        CAST(average_price AS DOUBLE) AS average_price,
                        CAST(volume AS BIGINT) AS volume,
                        CAST(traded_value AS DOUBLE) AS traded_value,
                        TRY_CAST(ohlc_open AS DOUBLE) AS ohlc_open,
                        TRY_CAST(ohlc_high AS DOUBLE) AS ohlc_high,
                        TRY_CAST(ohlc_low AS DOUBLE) AS ohlc_low,
                        TRY_CAST(ohlc_close AS DOUBLE) AS ohlc_close,
                        CAST(buy_quantity AS BIGINT) AS buy_quantity,
                        CAST(sell_quantity AS BIGINT) AS sell_quantity,
                        TRY_CAST(last_trade_time AS TIMESTAMP) AS last_trade_time,
                        TRY_CAST(quote_timestamp AS TIMESTAMP) AS quote_timestamp,
                        TRY_CAST(taken_at AS TIMESTAMP) AS taken_at
                    FROM read_csv(?, header = true, auto_detect = true, nullstr = '')
                    ORDER BY traded_value DESC, exchange, tradingsymbol
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


def fetch_quotes(
    client: KiteClient,
    equities: Sequence[EquityRow],
    *,
    should_stop: Callable[[], str | None] | None = None,
    on_batch: Callable[[int, int, int, int], None] | None = None,
) -> tuple[dict[int, dict[str, Any]], int]:
    """Return (token -> quote, missing_count)."""
    by_token: dict[int, dict[str, Any]] = {}
    total = len(equities)
    batch_total = (total + QUOTE_BATCH_SIZE - 1) // QUOTE_BATCH_SIZE if total else 0
    for batch_i, i in enumerate(range(0, total, QUOTE_BATCH_SIZE), start=1):
        if should_stop is not None:
            reason = should_stop()
            if reason is not None:
                raise CaptureRejected(f"stopped: {reason}")
        batch = equities[i : i + QUOTE_BATCH_SIZE]
        keys = [e.quote_key for e in batch]
        data = client.quote(keys)
        for q in data.values():
            token = q.get("instrument_token")
            if token is None:
                continue
            by_token[int(token)] = q
        if on_batch is not None:
            on_batch(batch_i, batch_total, min(i + len(batch), total), total)
    missing = sum(1 for e in equities if e.token not in by_token)
    return by_token, missing


def take_snapshot(
    client: KiteClient,
    equities: Sequence[EquityRow],
    *,
    now: datetime | None = None,
    dest_dir: Path | None = None,
    staging_dir: Path | None = None,
    catalog_path: Path | None = None,
    should_stop: Callable[[], str | None] | None = None,
    on_batch: Callable[[int, int, int, int], None] | None = None,
) -> CaptureResult:
    if not equities:
        raise CaptureRejected("no NSE/BSE EQ instruments to snapshot")

    now = now if now is not None else _now_ist()
    if now.tzinfo is None:
        now = now.replace(tzinfo=auth.IST)
    else:
        now = now.astimezone(auth.IST)
    trade_date = now.date()
    dest_dir = dest_dir if dest_dir is not None else PREOPEN_DIR
    staging_dir = staging_dir if staging_dir is not None else STAGING_DIR
    catalog_path = catalog_path if catalog_path is not None else CATALOG_PATH

    by_token, missing = fetch_quotes(
        client,
        equities,
        should_stop=should_stop,
        on_batch=on_batch,
    )

    rows: list[dict[str, Any]] = []
    total_volume = 0
    total_traded_value = 0.0
    traded_rows = 0
    for e in equities:
        q = by_token.get(e.token)
        if q is None:
            continue
        row = _row_from_quote(e, q, trade_date=trade_date, taken_at=now)
        rows.append(row)
        vol = int(row["volume"])
        total_volume += vol
        total_traded_value += float(row["traded_value"])
        if vol > 0:
            traded_rows += 1

    if total_volume <= 0:
        raise CaptureRejected(
            f"no pre-open volume on {ymd(trade_date)}; "
            "auction may not have matched yet, or this is a weekend/holiday. "
            "Re-run after ~09:08 IST on a session day (use --force to skip wait)."
        )

    dest = snapshot_path(trade_date, root=dest_dir)
    _write_parquet(rows, dest, staging_dir=staging_dir)

    con = catalog_mod.connect(catalog_path)
    try:
        catalog_mod.record_snapshot(
            con,
            trade_date=trade_date,
            path=dest,
            taken_at=now,
            rows=len(rows),
            traded_rows=traded_rows,
            total_traded_value=total_traded_value,
        )
    finally:
        con.close()

    return CaptureResult(
        trade_date=trade_date,
        path=dest,
        equities=len(rows),
        traded_rows=traded_rows,
        missing=missing,
        total_volume=total_volume,
        total_traded_value=total_traded_value,
        taken_at=now.replace(tzinfo=None),
    )


def run_capture(
    client: KiteClient,
    *,
    at: dt_time = DEFAULT_CAPTURE_AT,
    skip_wait: bool = False,
    dry_run: bool = False,
    now: datetime | None = None,
    dest_dir: Path | None = None,
    staging_dir: Path | None = None,
    catalog_path: Path | None = None,
    equities: Sequence[EquityRow] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    on_wait: Callable[[float, datetime], None] | None = None,
    on_batch: Callable[[int, int, int, int], None] | None = None,
) -> CaptureResult | CaptureDryRun:
    rows = list(equities) if equities is not None else list_equity()
    estimate = estimate_dry_run(rows, at=at, now=now, skip_wait=skip_wait)
    if dry_run:
        return estimate

    if not skip_wait:
        wait_until(at, now=now, sleep=sleep, on_wait=on_wait)
        capture_now = _now_ist()
    else:
        capture_now = now if now is not None else _now_ist()

    return take_snapshot(
        client,
        rows,
        now=capture_now,
        dest_dir=dest_dir,
        staging_dir=staging_dir,
        catalog_path=catalog_path,
        on_batch=on_batch,
    )
