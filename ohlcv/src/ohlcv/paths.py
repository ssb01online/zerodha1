"""Paths under data/ohlcv/nfo/ and yyyymmdd helpers."""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

from kitetools.config import DATA_DIR

OHLCV_DIR = DATA_DIR / "ohlcv" / "nfo"
PROBE_DIR = OHLCV_DIR / "_probe"
DAILY_DIR = OHLCV_DIR / "daily"
DAY_DIR = OHLCV_DIR / "day"
MINUTE_DIR = OHLCV_DIR / "minute"
STAGING_DIR = OHLCV_DIR / "_staging"
CATALOG_PATH = OHLCV_DIR / "catalog.duckdb"
ARCHIVE_DIR = DATA_DIR / "instruments" / "archive"


def ymd(value: date | datetime) -> str:
    """Format as yyyymmdd."""
    if isinstance(value, datetime):
        value = value.date()
    return value.strftime("%Y%m%d")


def sanitize_underlying(name: str) -> str:
    """Folder-safe underlying name (M&M -> M_M)."""
    text = (name or "").strip().upper()
    out: list[str] = []
    for ch in text:
        if ch.isalnum() or ch in ("-", "_"):
            out.append(ch)
        else:
            out.append("_")
    return "".join(out) or "UNKNOWN"


def series_path(root: Path, underlying: str, expiry: date) -> Path:
    return root / sanitize_underlying(underlying) / f"{ymd(expiry)}.parquet"


def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path
