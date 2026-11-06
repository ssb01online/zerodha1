"""Per-market fetch watermarks for NSE/BSE cash equity.

Separate from the F&O catalog: cash instruments have no expiry.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import duckdb

from .catalog import connect

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
            f"cash catalog schema_version={row[0]}, code expects {SCHEMA_VERSION}"
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
    return connect(path, read_only=read_only)


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
