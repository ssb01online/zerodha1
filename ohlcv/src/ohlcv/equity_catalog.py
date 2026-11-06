"""NSE/BSE cash equity store: day bars, symbol names, and fetch watermarks.

Separate from the F&O catalog. One file per exchange (``nse_eq.duckdb``,
``bse_eq.duckdb``). Bars are keyed by instrument token; the trading symbol
lives on ``symbols``, not on every bar.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import duckdb

from .catalog import CatalogLocked, connect

SCHEMA_VERSION = 1


@dataclass(frozen=True)
class EquityFetchState:
    token: int
    interval: str
    fetched_to: datetime | None
    status: str
    rows_total: int
    attempts: int
    last_error: str | None
    updated_at: datetime | None


@dataclass(frozen=True)
class EquitySymbol:
    token: int
    exchange_token: int
    tradingsymbol: str
    prev_tradingsymbol: str | None
    name: str
    updated_at: datetime | None


def ensure_schema(con: duckdb.DuckDBPyConnection) -> None:
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
        """
    )
    row = con.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
    if row is None:
        con.execute(
            "INSERT INTO meta (key, value) VALUES ('schema_version', ?)",
            [str(SCHEMA_VERSION)],
        )
    elif int(row[0]) != SCHEMA_VERSION:
        raise RuntimeError(
            f"equity db schema_version={row[0]}, code expects {SCHEMA_VERSION}"
        )
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS symbols (
            token BIGINT PRIMARY KEY,
            exchange_token BIGINT NOT NULL,
            tradingsymbol VARCHAR NOT NULL,
            prev_tradingsymbol VARCHAR,
            name VARCHAR NOT NULL,
            updated_at TIMESTAMP
        )
        """
    )
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS bars (
            token BIGINT NOT NULL,
            ts TIMESTAMP NOT NULL,
            open DOUBLE NOT NULL,
            high DOUBLE NOT NULL,
            low DOUBLE NOT NULL,
            close DOUBLE NOT NULL,
            volume BIGINT NOT NULL,
            PRIMARY KEY (token, ts)
        )
        """
    )
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS fetch_state (
            token BIGINT NOT NULL,
            interval TEXT NOT NULL,
            fetched_to TIMESTAMP,
            status TEXT NOT NULL,
            rows_total BIGINT NOT NULL DEFAULT 0,
            attempts INTEGER NOT NULL DEFAULT 0,
            last_error TEXT,
            updated_at TIMESTAMP,
            PRIMARY KEY (token, interval)
        )
        """
    )


def open_catalog(path: Path, *, read_only: bool = False) -> duckdb.DuckDBPyConnection:
    try:
        return connect(path, read_only=read_only)
    except CatalogLocked as exc:
        raise CatalogLocked(
            f"{path.name} is locked — another ohlcv process or notebook has it open. "
            "Close the other connection and retry."
        ) from exc


def get_state(
    con: duckdb.DuckDBPyConnection,
    token: int,
    interval: str = "day",
) -> EquityFetchState | None:
    rec = con.execute(
        """
        SELECT token, interval, fetched_to, status, rows_total, attempts, last_error, updated_at
        FROM fetch_state
        WHERE token = ? AND interval = ?
        """,
        [token, interval],
    ).fetchone()
    if rec is None:
        return None
    return EquityFetchState(
        token=int(rec[0]),
        interval=str(rec[1]),
        fetched_to=rec[2],
        status=str(rec[3]),
        rows_total=int(rec[4] or 0),
        attempts=int(rec[5] or 0),
        last_error=str(rec[6]) if rec[6] else None,
        updated_at=rec[7],
    )


def list_states(
    con: duckdb.DuckDBPyConnection,
    interval: str = "day",
) -> list[EquityFetchState]:
    found = con.execute(
        """
        SELECT token, interval, fetched_to, status, rows_total, attempts, last_error, updated_at
        FROM fetch_state
        WHERE interval = ?
        ORDER BY token
        """,
        [interval],
    ).fetchall()
    return [
        EquityFetchState(
            token=int(rec[0]),
            interval=str(rec[1]),
            fetched_to=rec[2],
            status=str(rec[3]),
            rows_total=int(rec[4] or 0),
            attempts=int(rec[5] or 0),
            last_error=str(rec[6]) if rec[6] else None,
            updated_at=rec[7],
        )
        for rec in found
    ]


def upsert_state(con: duckdb.DuckDBPyConnection, state: EquityFetchState) -> None:
    con.execute(
        "DELETE FROM fetch_state WHERE token = ? AND interval = ?",
        [state.token, state.interval],
    )
    con.execute(
        """
        INSERT INTO fetch_state (
            token, interval, fetched_to, status, rows_total, attempts, last_error, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            state.token,
            state.interval,
            state.fetched_to,
            state.status,
            state.rows_total,
            state.attempts,
            state.last_error,
            state.updated_at,
        ],
    )


def _upsert_symbol(con: duckdb.DuckDBPyConnection, symbol: EquitySymbol) -> None:
    con.execute("DELETE FROM symbols WHERE token = ?", [symbol.token])
    con.execute(
        """
        INSERT INTO symbols (
            token, exchange_token, tradingsymbol, prev_tradingsymbol, name, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        [
            symbol.token,
            symbol.exchange_token,
            symbol.tradingsymbol,
            symbol.prev_tradingsymbol,
            symbol.name,
            symbol.updated_at,
        ],
    )


def commit_chunk(
    con: duckdb.DuckDBPyConnection,
    *,
    symbol: EquitySymbol,
    state: EquityFetchState,
    csv_path: Path | None,
    range_start: datetime | None,
    range_end: datetime | None,
) -> int:
    """Replace one chunk of bars from CSV, then store the symbol and watermark.

    ``csv_path`` is omitted for an empty chunk (watermark only). ``range_start``
    is inclusive and ``range_end`` is exclusive, both naive timestamps covering
    the chunk's calendar days. The call is one transaction.
    """
    con.execute("BEGIN TRANSACTION")
    try:
        if csv_path is not None:
            if range_start is None or range_end is None:
                raise ValueError("range_start and range_end are required when loading a CSV")
            con.execute(
                "DELETE FROM bars WHERE token = ? AND ts >= ? AND ts < ?",
                [symbol.token, range_start, range_end],
            )
            con.execute(
                """
                INSERT INTO bars (token, ts, open, high, low, close, volume)
                SELECT token, ts, open, high, low, close, volume
                FROM (
                    SELECT
                        CAST(token AS BIGINT) AS token,
                        CAST(ts AS TIMESTAMP) AS ts,
                        CAST(open AS DOUBLE) AS open,
                        CAST(high AS DOUBLE) AS high,
                        CAST(low AS DOUBLE) AS low,
                        CAST(close AS DOUBLE) AS close,
                        CAST(volume AS BIGINT) AS volume,
                        row_number() OVER (
                            PARTITION BY CAST(token AS BIGINT), CAST(ts AS TIMESTAMP)
                            ORDER BY CAST(ts AS TIMESTAMP)
                        ) AS rn
                    FROM read_csv(?, header = true, auto_detect = true)
                )
                WHERE rn = 1
                """,
                [str(csv_path)],
            )
        _upsert_symbol(con, symbol)
        count_row = con.execute(
            "SELECT COUNT(*) FROM bars WHERE token = ?",
            [symbol.token],
        ).fetchone()
        rows = int(count_row[0]) if count_row else 0
        upsert_state(
            con,
            EquityFetchState(
                token=state.token,
                interval=state.interval,
                fetched_to=state.fetched_to,
                status=state.status,
                rows_total=rows,
                attempts=state.attempts,
                last_error=state.last_error,
                updated_at=state.updated_at,
            ),
        )
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
    return rows


def read_day_bars(
    con: duckdb.DuckDBPyConnection,
    token: int,
    *,
    tail: int | None = None,
) -> list[dict[str, Any]]:
    """Bars for one token, oldest first. ``tail`` keeps the latest N."""
    limit_sql = ""
    params: list[Any] = [int(token)]
    order = "ASC"
    if tail is not None:
        order = "DESC"
        limit_sql = " LIMIT ?"
        params.append(int(tail))
    found = con.execute(
        f"""
        SELECT b.token, COALESCE(s.tradingsymbol, ''), b.ts,
               b.open, b.high, b.low, b.close, b.volume
        FROM bars b
        LEFT JOIN symbols s ON s.token = b.token
        WHERE b.token = ?
        ORDER BY b.ts {order}
        {limit_sql}
        """,
        params,
    ).fetchall()
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
