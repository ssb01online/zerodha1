"""Gzip archive of the daily instruments master (keeps expired tokens resolvable)."""

from __future__ import annotations

import gzip
import json
import shutil
from datetime import date
from pathlib import Path

from instruments.cache import MASTER_PATH

from .paths import ARCHIVE_DIR, ensure_dir, ymd


def _as_of_from_master(master_path: Path) -> date:
    data = json.loads(master_path.read_text(encoding="utf-8"))
    raw = data.get("as_of_date")
    if not raw:
        raise ValueError(f"master missing as_of_date: {master_path}")
    return date.fromisoformat(str(raw)[:10])


def archive_path_for(as_of: date, archive_dir: Path | None = None) -> Path:
    root = archive_dir if archive_dir is not None else ARCHIVE_DIR
    return root / f"{ymd(as_of)}.json.gz"


def ensure_archive(
    master_path: Path | None = None,
    archive_dir: Path | None = None,
) -> Path:
    """Copy master.json to archive/<yyyymmdd>.json.gz if not already present.

    Returns the archive path (existing or newly written).
    """
    src = Path(master_path) if master_path is not None else MASTER_PATH
    root = archive_dir if archive_dir is not None else ARCHIVE_DIR
    if not src.is_file():
        raise FileNotFoundError(f"instruments master not found: {src}")

    as_of = _as_of_from_master(src)
    dest = archive_path_for(as_of, root)
    if dest.is_file():
        return dest

    ensure_dir(dest.parent)
    # Write via temp then rename so a crash never leaves a half-written .gz.
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    with src.open("rb") as fin, gzip.open(tmp, "wb") as fout:
        shutil.copyfileobj(fin, fout)
    tmp.replace(dest)
    return dest
