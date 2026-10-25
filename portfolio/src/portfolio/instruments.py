"""Daily IST cache of Kite instrument meta for position enrichment.

Slim map: instrument_token → {expiry, instrument_type}. Full dump is not retained.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

from kitetools import auth
from kitetools.config import DATA_DIR

CACHE_DIR = DATA_DIR / "instruments"
CACHE_PATH = CACHE_DIR / "cache.json"
# After a failed refresh while serving stale data, wait before retrying Kite.
_REFRESH_RETRY_SECONDS = 300.0


@dataclass(frozen=True)
class InstrumentMeta:
    expiry: date | None
    instrument_type: str | None


_lock = threading.Lock()
_memory: dict[int, InstrumentMeta] | None = None
_memory_as_of: date | None = None
_next_refresh_attempt: float = 0.0
# When set (including {}), lookups use only this map — no file/Kite. None = normal cache.
_override: dict[int, InstrumentMeta] | None = None


def set_lookup_override(mapping: dict[int, InstrumentMeta] | None) -> None:
    """Test hook: force lookups to use ``mapping`` (empty dict → all misses)."""
    global _override
    _override = mapping


def clear_lookup_override() -> None:
    set_lookup_override(None)


def _today_ist() -> date:
    return datetime.now(auth.IST).date()


def _parse_expiry(value: Any) -> date | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    if not text:
        return None
    # Kite may emit YYYY-MM-DD or datetime ISO strings.
    if "T" in text:
        return datetime.fromisoformat(text).date()
    return date.fromisoformat(text[:10])


def _meta_from_row(row: dict[str, Any]) -> InstrumentMeta:
    itype = row.get("instrument_type")
    itype_s = str(itype).strip() if itype is not None and str(itype).strip() else None
    return InstrumentMeta(expiry=_parse_expiry(row.get("expiry")), instrument_type=itype_s)


def _rows_to_map(rows: list[dict[str, Any]]) -> dict[int, InstrumentMeta]:
    out: dict[int, InstrumentMeta] = {}
    for row in rows:
        token = row.get("instrument_token")
        if token is None:
            continue
        out[int(token)] = _meta_from_row(row)
    return out


def _load_file(path: Path) -> tuple[date, dict[int, InstrumentMeta]] | None:
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    as_of_raw = data.get("as_of_date")
    by_token = data.get("by_token")
    if not as_of_raw or not isinstance(by_token, dict):
        return None
    try:
        as_of = date.fromisoformat(str(as_of_raw)[:10])
    except ValueError:
        return None
    out: dict[int, InstrumentMeta] = {}
    for key, val in by_token.items():
        if not isinstance(val, dict):
            continue
        try:
            token = int(key)
        except (TypeError, ValueError):
            continue
        out[token] = InstrumentMeta(
            expiry=_parse_expiry(val.get("expiry")),
            instrument_type=(
                str(val["instrument_type"]).strip()
                if val.get("instrument_type") not in (None, "")
                else None
            ),
        )
    return as_of, out


def _save_file(path: Path, as_of: date, mapping: dict[int, InstrumentMeta]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "as_of_date": as_of.isoformat(),
        "by_token": {
            str(token): {
                "expiry": meta.expiry.isoformat() if meta.expiry else None,
                "instrument_type": meta.instrument_type,
            }
            for token, meta in mapping.items()
        },
    }
    path.write_text(json.dumps(payload), encoding="utf-8")


def _fetch_from_kite() -> dict[int, InstrumentMeta]:
    kite = auth.get_kite()
    rows = kite.instruments()
    return _rows_to_map(list(rows or []))


def _ensure_loaded() -> dict[int, InstrumentMeta]:
    """Return in-memory map for today, refreshing from Kite when needed."""
    global _memory, _memory_as_of, _next_refresh_attempt
    today = _today_ist()
    with _lock:
        if _memory is not None and _memory_as_of == today:
            return _memory

        loaded = _load_file(CACHE_PATH)
        if loaded is not None and loaded[0] == today:
            _memory_as_of, _memory = loaded
            return _memory

        stale: dict[int, InstrumentMeta] | None = None
        if loaded is not None:
            stale = loaded[1]
        elif _memory is not None:
            stale = _memory

        now = time.monotonic()
        if stale is not None and now < _next_refresh_attempt:
            _memory = stale
            if loaded is not None:
                _memory_as_of = loaded[0]
            return _memory

        try:
            fresh = _fetch_from_kite()
            _save_file(CACHE_PATH, today, fresh)
            _memory = fresh
            _memory_as_of = today
            _next_refresh_attempt = 0.0
            return _memory
        except Exception:
            _next_refresh_attempt = now + _REFRESH_RETRY_SECONDS
            if stale is not None:
                _memory = stale
                if loaded is not None:
                    _memory_as_of = loaded[0]
                return _memory
            # No usable cache (e.g. offline selftest): empty map for today, no retry storm.
            _memory = {}
            _memory_as_of = today
            return _memory


def get_meta(token: int) -> InstrumentMeta | None:
    """Lookup instrument meta by token. Honours test override when set."""
    if _override is not None:
        return _override.get(token)
    if token == 0:
        return None
    mapping = _ensure_loaded()
    return mapping.get(token)


__all__ = [
    "CACHE_PATH",
    "InstrumentMeta",
    "clear_lookup_override",
    "get_meta",
    "set_lookup_override",
]
