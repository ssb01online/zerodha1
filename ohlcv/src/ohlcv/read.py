"""Read helpers for daily quote snapshots and minute/day series (in-memory DuckDB)."""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable, Sequence

import duckdb

from .paths import (
    DAILY_DIR,
    DAY_DIR,
    MINUTE_DIR,
    STAGING_DIR,
    ensure_dir,
    sanitize_underlying,
    series_path,
    ymd,
)
from .planner import IntervalName

DUCKDB_MEMORY = "4GB"
DUCKDB_THREADS = 4


def connect(*, temp_directory: Path | None = None) -> duckdb.DuckDBPyConnection:
    """Open an in-memory DuckDB with memory/thread limits suitable for laptop use."""
    con = duckdb.connect()
    con.execute(f"SET memory_limit='{DUCKDB_MEMORY}'")
    con.execute(f"SET threads={DUCKDB_THREADS}")
    tmp = temp_directory if temp_directory is not None else ensure_dir(STAGING_DIR / "duckdb_tmp")
    con.execute(f"SET temp_directory='{Path(tmp).as_posix()}'")
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
    con = connect()
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
    con = connect()
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


def minute_path(
    underlying: str,
    expiry: date,
    *,
    minute_dir: Path | None = None,
) -> Path:
    root = minute_dir if minute_dir is not None else MINUTE_DIR
    return series_path(root, underlying, expiry)


def day_path(
    underlying: str,
    expiry: date,
    *,
    day_dir: Path | None = None,
) -> Path:
    root = day_dir if day_dir is not None else DAY_DIR
    return series_path(root, underlying, expiry)


def series_file_path(
    underlying: str,
    expiry: date,
    *,
    interval: IntervalName = "minute",
    series_root: Path | None = None,
) -> Path:
    if series_root is not None:
        return series_path(series_root, underlying, expiry)
    if interval == "day":
        return day_path(underlying, expiry)
    return minute_path(underlying, expiry)


def _quote_idents(columns: Iterable[str] | None) -> str:
    if not columns:
        return "*"
    parts: list[str] = []
    for col in columns:
        name = str(col).strip()
        if not name or not all(c.isalnum() or c == "_" for c in name):
            raise ValueError(f"invalid column name: {col!r}")
        parts.append(name)
    return ", ".join(parts)


def _series_filters(
    *,
    tokens: Sequence[int] | None,
    symbols: Sequence[str] | None,
    start: datetime | date | None,
    end: datetime | date | None,
) -> tuple[str, list[Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if tokens is not None:
        token_list = [int(t) for t in tokens]
        if not token_list:
            clauses.append("FALSE")
        else:
            placeholders = ",".join("?" * len(token_list))
            clauses.append(f"token IN ({placeholders})")
            params.extend(token_list)
    if symbols is not None:
        symbol_list = [str(s) for s in symbols]
        if not symbol_list:
            clauses.append("FALSE")
        else:
            placeholders = ",".join("?" * len(symbol_list))
            clauses.append(f"tradingsymbol IN ({placeholders})")
            params.extend(symbol_list)
    if start is not None:
        clauses.append("ts >= ?")
        params.append(start)
    if end is not None:
        clauses.append("ts <= ?")
        params.append(end)
    if not clauses:
        return "", params
    return " WHERE " + " AND ".join(clauses), params


def series_rel(
    con: duckdb.DuckDBPyConnection,
    underlying: str,
    expiry: date,
    *,
    columns: Iterable[str] | None = None,
    tokens: Sequence[int] | None = None,
    symbols: Sequence[str] | None = None,
    start: datetime | date | None = None,
    end: datetime | date | None = None,
    interval: IntervalName = "minute",
    series_root: Path | None = None,
    minute_dir: Path | None = None,
) -> duckdb.DuckDBPyRelation:
    """Return a DuckDB relation for one minute/day series. Caller owns ``con``."""
    root = series_root
    if root is None and minute_dir is not None and interval == "minute":
        root = minute_dir
    path = series_file_path(
        underlying, expiry, interval=interval, series_root=root
    )
    if not path.is_file():
        raise FileNotFoundError(f"{interval} series not found: {path}")
    cols = _quote_idents(columns)
    where, params = _series_filters(tokens=tokens, symbols=symbols, start=start, end=end)
    sql = f"""
        SELECT {cols}
        FROM read_parquet(?)
        {where}
        ORDER BY token, ts
    """
    return con.execute(sql, [str(path), *params])


def series(
    underlying: str,
    expiry: date,
    *,
    columns: Iterable[str] | None = None,
    tokens: Sequence[int] | None = None,
    symbols: Sequence[str] | None = None,
    start: datetime | date | None = None,
    end: datetime | date | None = None,
    interval: IntervalName = "minute",
    series_root: Path | None = None,
    minute_dir: Path | None = None,
) -> list[dict[str, Any]]:
    """Load a small series slice into memory (opens/closes its own connection)."""
    con = connect()
    try:
        rel = series_rel(
            con,
            underlying,
            expiry,
            columns=columns,
            tokens=tokens,
            symbols=symbols,
            start=start,
            end=end,
            interval=interval,
            series_root=series_root,
            minute_dir=minute_dir,
        )
        names = [d[0] for d in rel.description]
        return [dict(zip(names, row)) for row in rel.fetchall()]
    finally:
        con.close()


def day_series(
    underlying: str,
    expiry: date,
    *,
    columns: Iterable[str] | None = None,
    tokens: Sequence[int] | None = None,
    symbols: Sequence[str] | None = None,
    start: datetime | date | None = None,
    end: datetime | date | None = None,
    day_dir: Path | None = None,
) -> list[dict[str, Any]]:
    """Load historical day bars for one (underlying, expiry) series file."""
    return series(
        underlying,
        expiry,
        columns=columns,
        tokens=tokens,
        symbols=symbols,
        start=start,
        end=end,
        interval="day",
        series_root=day_dir,
    )


def underlying_minute(
    underlying: str,
    *,
    columns: Iterable[str] | None = None,
    instrument_type: str | None = None,
    start: datetime | date | None = None,
    end: datetime | date | None = None,
    minute_dir: Path | None = None,
) -> list[dict[str, Any]]:
    """Load minute bars for one underlying across all expiry files under minute/<UL>/."""
    root = minute_dir if minute_dir is not None else MINUTE_DIR
    folder = root / sanitize_underlying(underlying)
    files = sorted(folder.glob("*.parquet")) if folder.is_dir() else []
    if not files:
        return []
    cols = _quote_idents(columns)
    clauses: list[str] = []
    params: list[Any] = [str(folder / "*.parquet")]
    if instrument_type is not None:
        clauses.append("instrument_type = ?")
        params.append(instrument_type)
    if start is not None:
        clauses.append("ts >= ?")
        params.append(start)
    if end is not None:
        clauses.append("ts <= ?")
        params.append(end)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    con = connect()
    try:
        rel = con.execute(
            f"""
            SELECT {cols}
            FROM read_parquet(?, hive_partitioning = false)
            {where}
            ORDER BY expiry, token, ts
            """,
            params,
        )
        names = [d[0] for d in rel.description]
        return [dict(zip(names, row)) for row in rel.fetchall()]
    finally:
        con.close()


def file_info(path: Path) -> tuple[int, int, datetime | None, datetime | None]:
    """Return (bytes, rows, min_ts, max_ts) using Parquet metadata / column stats.

    Missing path → FileNotFoundError. Empty / schema-only files return rows=0 and
    null timestamps.
    """
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(f"parquet not found: {path}")
    size = path.stat().st_size
    con = connect()
    try:
        # Footer metadata for row count (no data scan).
        meta = con.execute(
            "SELECT num_rows FROM parquet_file_metadata(?)",
            [str(path)],
        ).fetchone()
        rows = int(meta[0] or 0) if meta and meta[0] is not None else 0
        if rows == 0:
            return size, 0, None, None

        def _as_ts(value: Any) -> datetime | None:
            if value is None:
                return None
            if isinstance(value, datetime):
                return value
            text = str(value)
            try:
                return datetime.fromisoformat(text)
            except ValueError:
                return None

        # Column stats for ts min/max.
        stats = con.execute(
            """
            SELECT MIN(stats_min_value), MAX(stats_max_value)
            FROM parquet_metadata(?)
            WHERE path_in_schema = 'ts'
            """,
            [str(path)],
        ).fetchone()
        min_ts = _as_ts(stats[0]) if stats else None
        max_ts = _as_ts(stats[1]) if stats else None
        if min_ts is None or max_ts is None:
            span = con.execute(
                "SELECT MIN(ts), MAX(ts) FROM read_parquet(?)",
                [str(path)],
            ).fetchone()
            if span:
                min_ts = _as_ts(span[0]) or min_ts
                max_ts = _as_ts(span[1]) or max_ts
        return size, rows, min_ts, max_ts
    finally:
        con.close()
