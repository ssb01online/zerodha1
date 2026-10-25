"""End-of-day quote() snapshot of all live NFO contracts."""

from __future__ import annotations

import csv
import os
import tempfile
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable, Sequence

import duckdb

from kitetools import auth

from .catalog import ContractRow, list_contracts
from .client import KiteClient, RetryExhausted
from .paths import DAILY_DIR, STAGING_DIR, ensure_dir, ymd
from .universe import SESSION_END

QUOTE_BATCH_SIZE = 500
QUOTE_PACE_S = 1.0


class SnapshotRejected(Exception):
    """Snapshot refused (weekend / no session / empty volume)."""


@dataclass(frozen=True)
class SnapshotDryRun:
    contracts: int
    batches: int
    estimated_seconds: float


@dataclass(frozen=True)
class SnapshotResult:
    trade_date: date
    status: str
    path: Path
    contracts: int
    traded_contracts: int
    missing: int
    total_volume: int
    taken_at: datetime


def _today_ist() -> date:
    return datetime.now(auth.IST).date()


def _now_ist() -> datetime:
    return datetime.now(auth.IST)


def live_contracts(
    con: duckdb.DuckDBPyConnection,
    *,
    seen_on: date | None = None,
) -> list[ContractRow]:
    """NFO contracts whose last_seen matches today's master sync date."""
    seen_on = seen_on if seen_on is not None else _today_ist()
    return [c for c in list_contracts(con) if c.last_seen == seen_on]


def estimate_dry_run(contracts: Sequence[ContractRow]) -> SnapshotDryRun:
    n = len(contracts)
    batches = (n + QUOTE_BATCH_SIZE - 1) // QUOTE_BATCH_SIZE if n else 0
    return SnapshotDryRun(
        contracts=n,
        batches=batches,
        estimated_seconds=batches * QUOTE_PACE_S,
    )


def snapshot_status(now: datetime | None = None) -> str:
    now = now if now is not None else _now_ist()
    if now.tzinfo is None:
        now = now.replace(tzinfo=auth.IST)
    else:
        now = now.astimezone(auth.IST)
    session_end = datetime.combine(now.date(), SESSION_END, tzinfo=auth.IST)
    return "final" if now >= session_end else "intraday"


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


def _infer_trade_date(quotes: dict[str, Any]) -> date | None:
    best: date | None = None
    for q in quotes.values():
        ts = _parse_trade_ts(q.get("last_trade_time"))
        if ts is None:
            continue
        day = ts.date()
        if best is None or day > best:
            best = day
    return best


def _row_from_quote(
    contract: ContractRow,
    quote: dict[str, Any],
    trade_date: date,
) -> dict[str, Any]:
    ohlc = quote.get("ohlc") or {}
    volume = int(quote.get("volume") or 0)
    avg = float(quote.get("average_price") or 0.0)
    last_price = quote.get("last_price")
    close = float(last_price) if last_price is not None else None
    prev_close_raw = ohlc.get("close")
    prev_close = float(prev_close_raw) if prev_close_raw is not None else None
    if volume > 0:
        open_ = float(ohlc["open"]) if ohlc.get("open") is not None else None
        high = float(ohlc["high"]) if ohlc.get("high") is not None else None
        low = float(ohlc["low"]) if ohlc.get("low") is not None else None
    else:
        open_ = high = low = None
    ltt = _parse_trade_ts(quote.get("last_trade_time"))
    return {
        "trade_date": trade_date.isoformat(),
        "token": contract.token,
        "underlying": contract.underlying,
        "expiry": contract.expiry.isoformat(),
        "tradingsymbol": contract.tradingsymbol,
        "instrument_type": contract.instrument_type,
        "strike": contract.strike,
        "lot_size": contract.lot_size,
        "open": open_,
        "high": high,
        "low": low,
        "close": close,
        "prev_close": prev_close,
        "volume": volume,
        "average_price": avg,
        "traded_value": float(volume) * avg,
        "traded": volume > 0,
        "oi": float(quote.get("oi") or quote.get("open_interest") or 0.0),
        "oi_day_high": float(quote.get("oi_day_high") or 0.0),
        "oi_day_low": float(quote.get("oi_day_low") or 0.0),
        "buy_quantity": int(quote.get("buy_quantity") or 0),
        "sell_quantity": int(quote.get("sell_quantity") or 0),
        "last_trade_time": ltt.isoformat() if ltt is not None else None,
    }


_CSV_COLUMNS = [
    "trade_date",
    "token",
    "underlying",
    "expiry",
    "tradingsymbol",
    "instrument_type",
    "strike",
    "lot_size",
    "open",
    "high",
    "low",
    "close",
    "prev_close",
    "volume",
    "average_price",
    "traded_value",
    "traded",
    "oi",
    "oi_day_high",
    "oi_day_low",
    "buy_quantity",
    "sell_quantity",
    "last_trade_time",
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
    staging = stage_root / f"daily_{dest.stem}.parquet"
    fd, tmp_name = tempfile.mkstemp(prefix="ohlcv_daily_", suffix=".csv")
    tmp_path = Path(tmp_name)
    try:
        with open(fd, "w", encoding="utf-8", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=_CSV_COLUMNS)
            writer.writeheader()
            for row in rows:
                out = {}
                for col in _CSV_COLUMNS:
                    val = row.get(col)
                    if val is None:
                        out[col] = ""
                    elif isinstance(val, bool):
                        out[col] = "true" if val else "false"
                    else:
                        out[col] = val
                writer.writerow(out)

        con = duckdb.connect()
        try:
            con.execute("SET memory_limit='4GB'")
            con.execute(
                """
                COPY (
                    SELECT
                        CAST(trade_date AS DATE) AS trade_date,
                        CAST(token AS BIGINT) AS token,
                        underlying,
                        CAST(expiry AS DATE) AS expiry,
                        tradingsymbol,
                        instrument_type,
                        CAST(strike AS DOUBLE) AS strike,
                        CAST(lot_size AS INTEGER) AS lot_size,
                        TRY_CAST(open AS DOUBLE) AS open,
                        TRY_CAST(high AS DOUBLE) AS high,
                        TRY_CAST(low AS DOUBLE) AS low,
                        TRY_CAST(close AS DOUBLE) AS close,
                        TRY_CAST(prev_close AS DOUBLE) AS prev_close,
                        CAST(volume AS BIGINT) AS volume,
                        CAST(average_price AS DOUBLE) AS average_price,
                        CAST(traded_value AS DOUBLE) AS traded_value,
                        CAST(traded AS BOOLEAN) AS traded,
                        CAST(oi AS DOUBLE) AS oi,
                        CAST(oi_day_high AS DOUBLE) AS oi_day_high,
                        CAST(oi_day_low AS DOUBLE) AS oi_day_low,
                        CAST(buy_quantity AS BIGINT) AS buy_quantity,
                        CAST(sell_quantity AS BIGINT) AS sell_quantity,
                        TRY_CAST(last_trade_time AS TIMESTAMP) AS last_trade_time
                    FROM read_csv(?, header = true, auto_detect = true, nullstr = '')
                    ORDER BY underlying, expiry, token
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
    contracts: Sequence[ContractRow],
) -> tuple[dict[int, dict[str, Any]], int]:
    """Return (token -> quote, missing_count). Aborts on RetryExhausted."""
    by_token: dict[int, dict[str, Any]] = {}
    for i in range(0, len(contracts), QUOTE_BATCH_SIZE):
        batch = contracts[i : i + QUOTE_BATCH_SIZE]
        keys = [f"NFO:{c.tradingsymbol}" for c in batch]
        data = client.quote(keys)
        for q in data.values():
            token = q.get("instrument_token")
            if token is None:
                continue
            by_token[int(token)] = q
    missing = sum(1 for c in contracts if c.token not in by_token)
    return by_token, missing


def take_snapshot(
    client: KiteClient,
    contracts: Sequence[ContractRow],
    *,
    now: datetime | None = None,
    run_date: date | None = None,
    dest_dir: Path | None = None,
    staging_dir: Path | None = None,
) -> SnapshotResult:
    """Fetch quotes, validate session, write daily/yyyymmdd.parquet."""
    if not contracts:
        raise SnapshotRejected("no live NFO contracts to snapshot")

    now = now if now is not None else _now_ist()
    if now.tzinfo is None:
        now = now.replace(tzinfo=auth.IST)
    else:
        now = now.astimezone(auth.IST)
    run_date = run_date if run_date is not None else now.date()
    dest_dir = dest_dir if dest_dir is not None else DAILY_DIR

    by_token, missing = fetch_quotes(client, contracts)
    trade_date = _infer_trade_date(by_token)
    if trade_date is None:
        raise SnapshotRejected(
            "no last_trade_time in quote response; cannot infer trade date"
        )
    if trade_date != run_date:
        raise SnapshotRejected(
            f"no session today; last session was {ymd(trade_date)}; "
            "take the snapshot on the session day after close."
        )

    rows: list[dict[str, Any]] = []
    total_volume = 0
    traded_contracts = 0
    for c in contracts:
        q = by_token.get(c.token)
        if q is None:
            continue
        row = _row_from_quote(c, q, trade_date)
        rows.append(row)
        total_volume += int(row["volume"])
        if row["traded"]:
            traded_contracts += 1

    if total_volume <= 0:
        raise SnapshotRejected(
            f"no session today; last session was {ymd(trade_date)}; "
            "take the snapshot on the session day after close."
        )

    status = snapshot_status(now)
    dest = dest_dir / f"{ymd(trade_date)}.parquet"
    _write_parquet(rows, dest, staging_dir=staging_dir)
    taken_at = now.replace(tzinfo=None)
    return SnapshotResult(
        trade_date=trade_date,
        status=status,
        path=dest,
        contracts=len(rows),
        traded_contracts=traded_contracts,
        missing=missing,
        total_volume=total_volume,
        taken_at=taken_at,
    )


def daily_path(trade_date: date, *, dest_dir: Path | None = None) -> Path:
    root = dest_dir if dest_dir is not None else DAILY_DIR
    return root / f"{ymd(trade_date)}.parquet"


def untraded_tokens(
    trade_date: date,
    *,
    dest_dir: Path | None = None,
) -> set[int]:
    """Tokens with volume == 0 in that day's snapshot file."""
    path = daily_path(trade_date, dest_dir=dest_dir)
    if not path.is_file():
        raise FileNotFoundError(f"daily snapshot not found: {path}")
    con = duckdb.connect()
    try:
        con.execute("SET memory_limit='4GB'")
        rows = con.execute(
            "SELECT token FROM read_parquet(?) WHERE volume = 0",
            [str(path)],
        ).fetchall()
        return {int(r[0]) for r in rows}
    finally:
        con.close()


def load_snapshot_rows(
    trade_date: date,
    *,
    dest_dir: Path | None = None,
    columns: Iterable[str] | None = None,
) -> list[dict[str, Any]]:
    path = daily_path(trade_date, dest_dir=dest_dir)
    if not path.is_file():
        raise FileNotFoundError(f"daily snapshot not found: {path}")
    cols = ", ".join(columns) if columns else "*"
    con = duckdb.connect()
    try:
        con.execute("SET memory_limit='4GB'")
        rel = con.execute(f"SELECT {cols} FROM read_parquet(?)", [str(path)])
        names = [d[0] for d in rel.description]
        return [dict(zip(names, row)) for row in rel.fetchall()]
    finally:
        con.close()
