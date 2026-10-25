"""High-level capture helpers: live or from sample → normalize → store."""

from __future__ import annotations

from pathlib import Path

from . import store
from .fetch import TokenInvalid, fetch_live, load_from_sample
from .normalize import to_view
from .store import CaptureResult


def capture_live(
    db_path: Path | None = None,
    *,
    source: str = "live",
) -> CaptureResult:
    raw = fetch_live()
    view = to_view(raw)
    con = store.connect(db_path)
    try:
        store.ensure_schema(con)
        return store.capture(con, view, source=source)
    finally:
        con.close()


def capture_from_sample(
    folder: Path,
    db_path: Path | None = None,
    *,
    source: str = "sample",
) -> CaptureResult:
    raw = load_from_sample(folder)
    view = to_view(raw)
    con = store.connect(db_path)
    try:
        store.ensure_schema(con)
        return store.capture(con, view, source=source)
    finally:
        con.close()


__all__ = ["CaptureResult", "TokenInvalid", "capture_from_sample", "capture_live"]
