"""Read helpers for daily quote snapshots (in-memory DuckDB)."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any, Iterable

import duckdb

from .paths import DAILY_DIR, ymd


def _connect() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    con.execute("SET memory_limit='4GB'")
    return con


def daily_path(trade_date: date, *, dest_dir: Path | None = None) -> Path:
    root = dest_dir if dest_dir is not None else DAILY_DIR
    return root / f"{ymd(trade_date)}.parquet"


def daily(
    trade_date: date,
    *,
    columns: Iterable[str] | None = None,
    dest_dir: Path | None = None,
) -> list[dict[str, Any]]:
    """Load one daily/yyyymmdd.parquet snapshot."""
    path = daily_path(trade_date, dest_dir=dest_dir)
    if not path.is_file():
        raise FileNotFoundError(f"daily snapshot not found: {path}")
    cols = ", ".join(columns) if columns else "*"
    con = _connect()
    try:
        rel = con.execute(f"SELECT {cols} FROM read_parquet(?)", [str(path)])
        names = [d[0] for d in rel.description]
        return [dict(zip(names, row)) for row in rel.fetchall()]
    finally:
        con.close()


def series_daily(
    underlying: str,
    expiry: date,
    *,
    columns: Iterable[str] | None = None,
    dest_dir: Path | None = None,
) -> list[dict[str, Any]]:
    """Day bars for one (underlying, expiry) across all daily/*.parquet files."""
    root = dest_dir if dest_dir is not None else DAILY_DIR
    pattern = str(root / "*.parquet")
    files = sorted(root.glob("*.parquet")) if root.is_dir() else []
    if not files:
        return []
    cols = ", ".join(columns) if columns else "*"
    con = _connect()
    try:
        rel = con.execute(
            f"""
            SELECT {cols}
            FROM read_parquet(?, hive_partitioning = false)
            WHERE underlying = ? AND expiry = ?
            ORDER BY trade_date, token
            """,
            [pattern, underlying, expiry],
        )
        names = [d[0] for d in rel.description]
        return [dict(zip(names, row)) for row in rel.fetchall()]
    finally:
        con.close()
