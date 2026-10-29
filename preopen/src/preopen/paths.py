"""Paths under data/preopen/."""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

from kitetools.config import DATA_DIR

PREOPEN_DIR = DATA_DIR / "preopen"
STAGING_DIR = PREOPEN_DIR / "_staging"
CATALOG_PATH = PREOPEN_DIR / "catalog.duckdb"


def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def ymd(value: date | datetime) -> str:
    if isinstance(value, datetime):
        value = value.date()
    return value.strftime("%Y%m%d")


def snapshot_path(trade_date: date, *, root: Path | None = None) -> Path:
    base = root if root is not None else PREOPEN_DIR
    return base / f"{ymd(trade_date)}.parquet"
