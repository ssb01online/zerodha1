"""Paths under data/ohlcv/<market>/ and yyyymmdd helpers.

Module-level OHLCV_DIR / CATALOG_PATH / … remain NFO aliases for existing callers.
Use paths_for(market_id) for MCX/CDS (Phase 2+).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from kitetools.config import DATA_DIR

from .markets import get_market


@dataclass(frozen=True)
class MarketPaths:
    root: Path
    probe_dir: Path
    daily_dir: Path
    day_dir: Path
    minute_dir: Path
    continuous_dir: Path
    staging_dir: Path
    logs_dir: Path
    catalog_path: Path


def paths_for(market_id: str) -> MarketPaths:
    """Resolve on-disk layout for a market id (nfo / mcx / cds)."""
    profile = get_market(market_id)
    root = DATA_DIR / "ohlcv" / profile.id
    return MarketPaths(
        root=root,
        probe_dir=root / "_probe",
        daily_dir=root / "daily",
        day_dir=root / "day",
        minute_dir=root / "minute",
        continuous_dir=root / "continuous",
        staging_dir=root / "_staging",
        logs_dir=root / "logs",
        catalog_path=root / "catalog.duckdb",
    )


# NFO defaults — existing sync/fetch/cli imports keep working unchanged.
_NFO = paths_for("nfo")
OHLCV_DIR = _NFO.root
PROBE_DIR = _NFO.probe_dir
DAILY_DIR = _NFO.daily_dir
DAY_DIR = _NFO.day_dir
MINUTE_DIR = _NFO.minute_dir
STAGING_DIR = _NFO.staging_dir
LOGS_DIR = _NFO.logs_dir
CATALOG_PATH = _NFO.catalog_path
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
