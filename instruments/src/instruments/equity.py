"""NSE/BSE cash-equity instrument list (DuckDB + CSV).

Projected from the daily instruments master. No second Kite call.
Indices (segment INDICES, instrument_type EQ) are excluded.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any
from pathlib import Path

import duckdb

from .cache import CACHE_DIR, ensure_cache, get_as_of, iter_instruments
from .models import Instrument

EQUITY_DB_PATH = CACHE_DIR / "equity.duckdb"
EQUITY_CSV_PATH = CACHE_DIR / "equity.csv"

EQ_EXCHANGES = frozenset({"NSE", "BSE"})

_COLUMNS = (
    "instrument_token",
    "exchange_token",
    "exchange",
    "tradingsymbol",
    "prev_tradingsymbol",
    "name",
    "segment",
    "tick_size",
    "lot_size",
    "first_seen",
    "last_seen",
)


@dataclass(frozen=True)
class EquityRow:
    instrument_token: int
    exchange_token: int
    exchange: str
    tradingsymbol: str
    prev_tradingsymbol: str | None
    name: str
    segment: str
    tick_size: float
    lot_size: int
    first_seen: date
    last_seen: date


@dataclass(frozen=True)
class EquityPublish:
    as_of: date
    live: int
    nse: int
    bse: int
    db_path: Path
    csv_path: Path


def is_cash_equity(inst: Instrument) -> bool:
    """True for NSE/BSE regular equity. Drops indices and other series."""
    exchange = (inst.exchange or "").strip().upper()
    itype = (inst.instrument_type or "").strip().upper()
    segment = (inst.segment or "").strip().upper()
    return exchange in EQ_EXCHANGES and itype == "EQ" and segment == exchange


def _connect(db_path: Path) -> duckdb.DuckDBPyConnection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    return duckdb.connect(str(db_path))


def ensure_schema(con: duckdb.DuckDBPyConnection) -> None:
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS equity (
            instrument_token BIGINT PRIMARY KEY,
            exchange_token BIGINT NOT NULL,
            exchange VARCHAR NOT NULL,
            tradingsymbol VARCHAR NOT NULL,
            prev_tradingsymbol VARCHAR,
            name VARCHAR NOT NULL,
            segment VARCHAR NOT NULL,
            tick_size DOUBLE NOT NULL,
            lot_size INTEGER NOT NULL,
            first_seen DATE NOT NULL,
            last_seen DATE NOT NULL
        )
        """
    )


def _as_date(value: Any) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def _row_from_db(rec: tuple) -> EquityRow:
    prev = rec[4]
    return EquityRow(
        instrument_token=int(rec[0]),
        exchange_token=int(rec[1]),
        exchange=str(rec[2]),
        tradingsymbol=str(rec[3]),
        prev_tradingsymbol=str(prev) if prev else None,
        name=str(rec[5]),
        segment=str(rec[6]),
        tick_size=float(rec[7]),
        lot_size=int(rec[8]),
        first_seen=_as_date(rec[9]),
        last_seen=_as_date(rec[10]),
    )


def _load_existing(con: duckdb.DuckDBPyConnection) -> dict[int, EquityRow]:
    ensure_schema(con)
    found = con.execute(
        f"SELECT {', '.join(_COLUMNS)} FROM equity"
    ).fetchall()
    return {int(rec[0]): _row_from_db(rec) for rec in found}


def _merge(existing: EquityRow | None, inst: Instrument, as_of: date) -> EquityRow:
    symbol = inst.tradingsymbol.strip()
    prev: str | None = None
    first = as_of
    if existing is not None:
        first = existing.first_seen
        prev = existing.prev_tradingsymbol
        if existing.tradingsymbol != symbol:
            prev = existing.tradingsymbol
    return EquityRow(
        instrument_token=int(inst.instrument_token),
        exchange_token=int(inst.exchange_token),
        exchange=inst.exchange.strip().upper(),
        tradingsymbol=symbol,
        prev_tradingsymbol=prev,
        name=(inst.name or "").strip(),
        segment=inst.segment.strip().upper(),
        tick_size=float(inst.tick_size),
        lot_size=int(inst.lot_size or 0),
        first_seen=first,
        last_seen=as_of,
    )


def _upsert(con: duckdb.DuckDBPyConnection, row: EquityRow) -> None:
    con.execute("DELETE FROM equity WHERE instrument_token = ?", [row.instrument_token])
    con.execute(
        f"""
        INSERT INTO equity ({", ".join(_COLUMNS)})
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            row.instrument_token,
            row.exchange_token,
            row.exchange,
            row.tradingsymbol,
            row.prev_tradingsymbol,
            row.name,
            row.segment,
            row.tick_size,
            row.lot_size,
            row.first_seen,
            row.last_seen,
        ],
    )


def _write_csv(path: Path, rows: list[EquityRow]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(_COLUMNS)
        for row in rows:
            writer.writerow(
                [
                    row.instrument_token,
                    row.exchange_token,
                    row.exchange,
                    row.tradingsymbol,
                    row.prev_tradingsymbol or "",
                    row.name,
                    row.segment,
                    row.tick_size,
                    row.lot_size,
                    row.first_seen.isoformat(),
                    row.last_seen.isoformat(),
                ]
            )
    tmp.replace(path)


def list_equity(
    db_path: Path | None = None,
    *,
    exchange: str | None = None,
    as_of: date | None = None,
) -> list[EquityRow]:
    """Rows in the equity table. ``as_of`` keeps rows whose ``last_seen`` matches."""
    path = Path(db_path) if db_path is not None else EQUITY_DB_PATH
    if not path.is_file():
        return []
    con = duckdb.connect(str(path), read_only=True)
    try:
        found = con.execute(
            f"SELECT {', '.join(_COLUMNS)} FROM equity ORDER BY exchange, tradingsymbol"
        ).fetchall()
    finally:
        con.close()
    rows = [_row_from_db(rec) for rec in found]
    if exchange is not None:
        key = exchange.strip().upper()
        rows = [r for r in rows if r.exchange == key]
    if as_of is not None:
        rows = [r for r in rows if r.last_seen == as_of]
    return rows


def get_equity(
    db_path: Path | None = None,
    *,
    token: int | None = None,
    tradingsymbol: str | None = None,
    exchange: str | None = None,
) -> EquityRow | None:
    """Resolve one row by token, or by tradingsymbol within an exchange."""
    rows = list_equity(db_path, exchange=exchange)
    if token is not None:
        for row in rows:
            if row.instrument_token == int(token):
                return row
        return None
    symbol = (tradingsymbol or "").strip().upper()
    if not symbol:
        return None
    matches = [r for r in rows if r.tradingsymbol.upper() == symbol]
    if len(matches) == 1:
        return matches[0]
    return None


def publish_equity(
    rows: list[Instrument] | None = None,
    *,
    as_of: date | None = None,
    db_path: Path | None = None,
    csv_path: Path | None = None,
) -> EquityPublish:
    """Upsert cash-equity rows and overwrite the live-universe CSV.

    Tokens that leave the dump stay in DuckDB with their previous ``last_seen``.
    The CSV contains only rows whose ``last_seen`` equals ``as_of``.
    """
    if rows is None:
        ensure_cache()
        rows = list(iter_instruments())
        if as_of is None:
            as_of = get_as_of()
    if as_of is None:
        raise ValueError("as_of is required when rows are passed without a master cache")

    db = Path(db_path) if db_path is not None else EQUITY_DB_PATH
    csv_dest = Path(csv_path) if csv_path is not None else EQUITY_CSV_PATH
    incoming = [inst for inst in rows if is_cash_equity(inst)]

    con = _connect(db)
    try:
        existing = _load_existing(con)
        con.execute("BEGIN")
        try:
            for inst in incoming:
                merged = _merge(existing.get(int(inst.instrument_token)), inst, as_of)
                _upsert(con, merged)
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            raise
        stored = list(_load_existing(con).values())
    finally:
        con.close()

    live = [row for row in stored if row.last_seen == as_of]
    live.sort(key=lambda r: (r.exchange, r.tradingsymbol, r.instrument_token))
    _write_csv(csv_dest, live)
    return EquityPublish(
        as_of=as_of,
        live=len(live),
        nse=sum(1 for row in live if row.exchange == "NSE"),
        bse=sum(1 for row in live if row.exchange == "BSE"),
        db_path=db,
        csv_path=csv_dest,
    )
