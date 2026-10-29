"""DuckDB catalog of pre-open snapshot days."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

import duckdb

from .paths import CATALOG_PATH, ensure_dir, ymd

SCHEMA_VERSION = 1


@dataclass(frozen=True)
class SnapshotMeta:
    trade_date: date
    path: str
    taken_at: datetime
    rows: int
    traded_rows: int
    total_traded_value: float


def connect(catalog_path: Path | None = None, *, read_only: bool = False) -> duckdb.DuckDBPyConnection:
    path = catalog_path if catalog_path is not None else CATALOG_PATH
    if not read_only:
        ensure_dir(path.parent)
    return duckdb.connect(str(path), read_only=read_only)


def init_schema(con: duckdb.DuckDBPyConnection) -> None:
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS meta (
            key VARCHAR PRIMARY KEY,
            value VARCHAR NOT NULL
        )
        """
    )
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS snapshots (
            trade_date DATE PRIMARY KEY,
            path VARCHAR NOT NULL,
            taken_at TIMESTAMP NOT NULL,
            rows INTEGER NOT NULL,
            traded_rows INTEGER NOT NULL,
            total_traded_value DOUBLE NOT NULL
        )
        """
    )
    row = con.execute(
        "SELECT value FROM meta WHERE key = 'schema_version'"
    ).fetchone()
    if row is None:
        con.execute(
            "INSERT INTO meta (key, value) VALUES ('schema_version', ?)",
            [str(SCHEMA_VERSION)],
        )


def record_snapshot(
    con: duckdb.DuckDBPyConnection,
    *,
    trade_date: date,
    path: Path,
    taken_at: datetime,
    rows: int,
    traded_rows: int,
    total_traded_value: float,
) -> None:
    init_schema(con)
    taken = taken_at.replace(tzinfo=None) if taken_at.tzinfo else taken_at
    con.execute(
        """
        INSERT OR REPLACE INTO snapshots
            (trade_date, path, taken_at, rows, traded_rows, total_traded_value)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        [
            trade_date,
            str(path),
            taken,
            int(rows),
            int(traded_rows),
            float(total_traded_value),
        ],
    )


def _row_to_meta(row: tuple[Any, ...]) -> SnapshotMeta:
    td = row[0]
    if isinstance(td, datetime):
        td = td.date()
    elif not isinstance(td, date):
        td = date.fromisoformat(str(td)[:10])
    taken = row[2]
    if isinstance(taken, str):
        taken = datetime.fromisoformat(taken)
    return SnapshotMeta(
        trade_date=td,
        path=str(row[1]),
        taken_at=taken,
        rows=int(row[3]),
        traded_rows=int(row[4]),
        total_traded_value=float(row[5]),
    )


def list_days(
    con: duckdb.DuckDBPyConnection | None = None,
    *,
    catalog_path: Path | None = None,
) -> list[SnapshotMeta]:
    own = con is None
    if own:
        path = catalog_path if catalog_path is not None else CATALOG_PATH
        if not path.is_file():
            return []
        con = connect(path, read_only=True)
    try:
        try:
            rows = con.execute(
                """
                SELECT trade_date, path, taken_at, rows, traded_rows, total_traded_value
                FROM snapshots
                ORDER BY trade_date DESC
                """
            ).fetchall()
        except duckdb.Error:
            return []
        return [_row_to_meta(r) for r in rows]
    finally:
        if own and con is not None:
            con.close()


def get_day(
    trade_date: date,
    *,
    catalog_path: Path | None = None,
) -> SnapshotMeta | None:
    path = catalog_path if catalog_path is not None else CATALOG_PATH
    if not path.is_file():
        return None
    con = connect(path, read_only=True)
    try:
        try:
            row = con.execute(
                """
                SELECT trade_date, path, taken_at, rows, traded_rows, total_traded_value
                FROM snapshots
                WHERE trade_date = ?
                """,
                [trade_date],
            ).fetchone()
        except duckdb.Error:
            return None
        if row is None:
            return None
        return _row_to_meta(row)
    finally:
        con.close()


def meta_to_json(meta: SnapshotMeta) -> dict[str, Any]:
    return {
        "trade_date": meta.trade_date.isoformat(),
        "ymd": ymd(meta.trade_date),
        "path": meta.path,
        "taken_at": meta.taken_at.isoformat(sep=" "),
        "rows": meta.rows,
        "traded_rows": meta.traded_rows,
        "total_traded_value": meta.total_traded_value,
    }
