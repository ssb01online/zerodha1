"""Offline readers for pre-open parquet snapshots."""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from typing import Any

import duckdb

from . import catalog as catalog_mod
from .paths import PREOPEN_DIR, snapshot_path, ymd


def _parse_ymd(text: str) -> date:
    text = text.strip()
    if len(text) == 8 and text.isdigit():
        return date(int(text[:4]), int(text[4:6]), int(text[6:8]))
    return date.fromisoformat(text)


def resolve_parquet(
    trade_date: date,
    *,
    dest_dir: Path | None = None,
    catalog_path: Path | None = None,
) -> Path:
    meta = catalog_mod.get_day(trade_date, catalog_path=catalog_path)
    if meta is not None:
        path = Path(meta.path)
        if path.is_file():
            return path
    root = dest_dir if dest_dir is not None else PREOPEN_DIR
    path = snapshot_path(trade_date, root=root)
    if path.is_file():
        return path
    raise FileNotFoundError(f"pre-open snapshot not found for {ymd(trade_date)}")


def load_day(
    trade_date: date | str,
    *,
    limit: int = 50,
    min_value: float = 0.0,
    exchange: str | None = None,
    dest_dir: Path | None = None,
    catalog_path: Path | None = None,
) -> dict[str, Any]:
    """Return ranked rows plus totals for one trade date."""
    if isinstance(trade_date, str):
        trade_date = _parse_ymd(trade_date)
    path = resolve_parquet(
        trade_date,
        dest_dir=dest_dir,
        catalog_path=catalog_path,
    )
    exch = exchange.strip().upper() if exchange else None
    if exch is not None and exch not in ("NSE", "BSE"):
        raise ValueError(f"exchange must be NSE or BSE, got {exchange!r}")

    con = duckdb.connect()
    try:
        con.execute("SET memory_limit='4GB'")
        day_total = con.execute(
            "SELECT COALESCE(SUM(traded_value), 0) FROM read_parquet(?)",
            [str(path)],
        ).fetchone()[0]
        day_rows = con.execute(
            "SELECT COUNT(*) FROM read_parquet(?)",
            [str(path)],
        ).fetchone()[0]
        day_traded = con.execute(
            "SELECT COUNT(*) FROM read_parquet(?) WHERE volume > 0",
            [str(path)],
        ).fetchone()[0]

        where = ["traded_value >= ?"]
        params: list[Any] = [str(path), float(min_value)]
        if exch:
            where.append("exchange = ?")
            params.append(exch)
        where_sql = " AND ".join(where)
        filtered_total = con.execute(
            f"SELECT COALESCE(SUM(traded_value), 0) FROM read_parquet(?) WHERE {where_sql}",
            params,
        ).fetchone()[0]
        filtered_count = con.execute(
            f"SELECT COUNT(*) FROM read_parquet(?) WHERE {where_sql}",
            params,
        ).fetchone()[0]

        lim = max(0, int(limit))
        params_lim = list(params) + [lim]
        rel = con.execute(
            f"""
            SELECT
                exchange,
                tradingsymbol,
                token,
                name,
                last_price,
                average_price,
                volume,
                traded_value,
                ohlc_open,
                ohlc_high,
                ohlc_low,
                ohlc_close,
                buy_quantity,
                sell_quantity,
                last_trade_time,
                quote_timestamp,
                taken_at
            FROM read_parquet(?)
            WHERE {where_sql}
            ORDER BY traded_value DESC, exchange, tradingsymbol
            LIMIT ?
            """,
            params_lim,
        )
        names = [d[0] for d in rel.description]
        items: list[dict[str, Any]] = []
        for row in rel.fetchall():
            item = dict(zip(names, row))
            for key in ("last_trade_time", "quote_timestamp", "taken_at", "trade_date"):
                val = item.get(key)
                if isinstance(val, datetime):
                    item[key] = val.isoformat(sep=" ")
                elif isinstance(val, date):
                    item[key] = val.isoformat()
            items.append(item)
    finally:
        con.close()

    meta = catalog_mod.get_day(trade_date, catalog_path=catalog_path)
    return {
        "trade_date": trade_date.isoformat(),
        "ymd": ymd(trade_date),
        "path": str(path),
        "taken_at": meta.taken_at.isoformat(sep=" ") if meta else None,
        "day_rows": int(day_rows),
        "day_traded_rows": int(day_traded),
        "day_total_traded_value": float(day_total),
        "filtered_rows": int(filtered_count),
        "filtered_total_traded_value": float(filtered_total),
        "limit": lim,
        "min_value": float(min_value),
        "exchange": exch,
        "items": items,
    }


def list_days_payload(*, catalog_path: Path | None = None) -> dict[str, Any]:
    days = catalog_mod.list_days(catalog_path=catalog_path)
    return {
        "count": len(days),
        "days": [catalog_mod.meta_to_json(d) for d in days],
    }


def show_summary(
    trade_date: date | str | None = None,
    *,
    limit: int = 10,
    dest_dir: Path | None = None,
    catalog_path: Path | None = None,
) -> dict[str, Any]:
    if trade_date is None:
        days = catalog_mod.list_days(catalog_path=catalog_path)
        if not days:
            # Fall back to scanning parquet files.
            root = dest_dir if dest_dir is not None else PREOPEN_DIR
            files = sorted(root.glob("*.parquet"), reverse=True)
            if not files:
                raise FileNotFoundError("no pre-open snapshots yet")
            trade_date = _parse_ymd(files[0].stem)
        else:
            trade_date = days[0].trade_date
    return load_day(
        trade_date,
        limit=limit,
        dest_dir=dest_dir,
        catalog_path=catalog_path,
    )
