"""Daily IST cache of full Kite instruments (NSE/BSE/NFO/MCX/CDS) plus slim token lookup."""

from __future__ import annotations

import json
import threading
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterator

from kitetools import auth
from kitetools.config import DATA_DIR

from .models import Instrument, InstrumentMeta
from .scope import in_scope

CACHE_DIR = DATA_DIR / "instruments"
MASTER_PATH = CACHE_DIR / "master.json"
# Legacy slim cache from portfolio; ignored by this module, removed after a successful refresh.
_LEGACY_SLIM_PATH = CACHE_DIR / "cache.json"
# After a failed refresh while serving stale data, wait before retrying Kite.
_REFRESH_RETRY_SECONDS = 300.0

_lock = threading.Lock()
_rows: list[Instrument] | None = None
_by_token: dict[int, Instrument] | None = None
_meta_by_token: dict[int, InstrumentMeta] | None = None
_memory_as_of: date | None = None
_next_refresh_attempt: float = 0.0
# When set (including {}), get_meta uses only this map — no file/Kite. None = normal cache.
_override: dict[int, InstrumentMeta] | None = None
# When set (including []), full-row APIs / query use only these rows — no file/Kite.
_rows_override: list[Instrument] | None = None
_rows_override_by_token: dict[int, Instrument] | None = None
# Bumped on every memory load or rows override change so query indexes can invalidate.
_load_generation: int = 0


def set_lookup_override(mapping: dict[int, InstrumentMeta] | None) -> None:
    """Test hook: force get_meta to use ``mapping`` (empty dict → all misses)."""
    global _override
    _override = mapping


def clear_lookup_override() -> None:
    set_lookup_override(None)


def set_rows_override(rows: list[Instrument] | None) -> None:
    """Test hook: force full-row reads and query to use ``rows`` (empty → no instruments)."""
    global _rows_override, _rows_override_by_token, _load_generation
    _rows_override = rows
    if rows is None:
        _rows_override_by_token = None
    else:
        _rows_override_by_token = {r.instrument_token: r for r in rows}
    _load_generation += 1


def clear_rows_override() -> None:
    set_rows_override(None)


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
    if "T" in text:
        return datetime.fromisoformat(text).date()
    return date.fromisoformat(text[:10])


def _float(value: Any, default: float = 0.0) -> float:
    if value is None or value == "":
        return default
    return float(value)


def _int(value: Any, default: int = 0) -> int:
    if value is None or value == "":
        return default
    return int(value)


def _str(value: Any, default: str = "") -> str:
    if value is None:
        return default
    return str(value)


def instrument_from_row(row: dict[str, Any]) -> Instrument | None:
    """Build an Instrument from a Kite CSV/dict row; None if out of scope or missing token."""
    exchange = _str(row.get("exchange")).strip().upper()
    if not in_scope(exchange):
        return None
    token = row.get("instrument_token")
    if token is None or token == "":
        return None
    itype = _str(row.get("instrument_type")).strip()
    return Instrument(
        instrument_token=int(token),
        exchange_token=_int(row.get("exchange_token")),
        tradingsymbol=_str(row.get("tradingsymbol")),
        name=_str(row.get("name")),
        last_price=_float(row.get("last_price")),
        expiry=_parse_expiry(row.get("expiry")),
        strike=_float(row.get("strike")),
        tick_size=_float(row.get("tick_size")),
        lot_size=_int(row.get("lot_size"), 1),
        instrument_type=itype,
        segment=_str(row.get("segment")),
        exchange=exchange,
    )


def _rows_from_kite(raw_rows: list[dict[str, Any]]) -> list[Instrument]:
    out: list[Instrument] = []
    for row in raw_rows:
        inst = instrument_from_row(row)
        if inst is not None:
            out.append(inst)
    return out


def _index_rows(rows: list[Instrument]) -> tuple[dict[int, Instrument], dict[int, InstrumentMeta]]:
    by_token: dict[int, Instrument] = {}
    meta: dict[int, InstrumentMeta] = {}
    for inst in rows:
        by_token[inst.instrument_token] = inst
        meta[inst.instrument_token] = inst.meta()
    return by_token, meta


def instrument_to_json(inst: Instrument) -> dict[str, Any]:
    """Serialize a full instrument row for cache/API payloads."""
    return {
        "instrument_token": inst.instrument_token,
        "exchange_token": inst.exchange_token,
        "tradingsymbol": inst.tradingsymbol,
        "name": inst.name,
        "last_price": inst.last_price,
        "expiry": inst.expiry.isoformat() if inst.expiry else None,
        "strike": inst.strike,
        "tick_size": inst.tick_size,
        "lot_size": inst.lot_size,
        "instrument_type": inst.instrument_type,
        "segment": inst.segment,
        "exchange": inst.exchange,
    }


def _load_file(path: Path) -> tuple[date, list[Instrument]] | None:
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    as_of_raw = data.get("as_of_date")
    rows_raw = data.get("rows")
    if not as_of_raw or not isinstance(rows_raw, list):
        return None
    try:
        as_of = date.fromisoformat(str(as_of_raw)[:10])
    except ValueError:
        return None
    rows: list[Instrument] = []
    for row in rows_raw:
        if not isinstance(row, dict):
            continue
        inst = instrument_from_row(row)
        if inst is not None:
            rows.append(inst)
    return as_of, rows


def _save_file(path: Path, as_of: date, rows: list[Instrument]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "as_of_date": as_of.isoformat(),
        "rows": [instrument_to_json(inst) for inst in rows],
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    # Drop legacy slim cache after a successful full write.
    try:
        if _LEGACY_SLIM_PATH.is_file():
            _LEGACY_SLIM_PATH.unlink()
    except OSError:
        pass


def _fetch_from_kite() -> list[Instrument]:
    kite = auth.get_kite()
    raw = list(kite.instruments() or [])
    return _rows_from_kite(raw)


def _set_memory(as_of: date, rows: list[Instrument]) -> None:
    global _rows, _by_token, _meta_by_token, _memory_as_of, _load_generation
    by_token, meta = _index_rows(rows)
    _rows = rows
    _by_token = by_token
    _meta_by_token = meta
    _memory_as_of = as_of
    _load_generation += 1


def _ensure_loaded() -> dict[int, InstrumentMeta]:
    """Return slim meta map for today, refreshing from Kite when needed."""
    global _next_refresh_attempt
    today = _today_ist()
    with _lock:
        if _meta_by_token is not None and _memory_as_of == today:
            return _meta_by_token

        loaded = _load_file(MASTER_PATH)
        if loaded is not None and loaded[0] == today:
            _set_memory(loaded[0], loaded[1])
            assert _meta_by_token is not None
            return _meta_by_token

        stale_rows: list[Instrument] | None = None
        stale_as_of: date | None = None
        if loaded is not None:
            stale_as_of, stale_rows = loaded
        elif _rows is not None and _memory_as_of is not None:
            stale_rows = _rows
            stale_as_of = _memory_as_of

        now = time.monotonic()
        if stale_rows is not None and now < _next_refresh_attempt:
            _set_memory(stale_as_of or today, stale_rows)
            assert _meta_by_token is not None
            return _meta_by_token

        try:
            fresh = _fetch_from_kite()
            _save_file(MASTER_PATH, today, fresh)
            _set_memory(today, fresh)
            _next_refresh_attempt = 0.0
            assert _meta_by_token is not None
            return _meta_by_token
        except Exception:
            _next_refresh_attempt = now + _REFRESH_RETRY_SECONDS
            if stale_rows is not None:
                _set_memory(stale_as_of or today, stale_rows)
                assert _meta_by_token is not None
                return _meta_by_token
            _set_memory(today, [])
            assert _meta_by_token is not None
            return _meta_by_token


def ensure_cache() -> None:
    """Load or refresh today's cache (no-op if already current in memory)."""
    if _rows_override is not None:
        return
    _ensure_loaded()


def peek_as_of() -> date | None:
    """In-memory as_of without loading/refreshing the cache (None if not loaded yet)."""
    if _rows_override is not None:
        return None
    return _memory_as_of


def get_as_of() -> date | None:
    """IST calendar date the in-memory master was loaded for, or None if empty/override."""
    if _rows_override is not None:
        return None
    ensure_cache()
    return _memory_as_of


def get_load_generation() -> int:
    """Monotonic counter; query indexes rebuild when this changes."""
    if _rows_override is not None:
        return _load_generation
    ensure_cache()
    return _load_generation


def get_meta(token: int) -> InstrumentMeta | None:
    """Lookup instrument meta by token. Honours test override when set."""
    if _override is not None:
        return _override.get(token)
    if token == 0:
        return None
    if _rows_override is not None:
        assert _rows_override_by_token is not None
        inst = _rows_override_by_token.get(token)
        return inst.meta() if inst is not None else None
    mapping = _ensure_loaded()
    return mapping.get(token)


def get_by_token(token: int) -> Instrument | None:
    """Return the full instrument row for ``token``, or None."""
    if token == 0:
        return None
    if _rows_override is not None:
        assert _rows_override_by_token is not None
        return _rows_override_by_token.get(token)
    _ensure_loaded()
    assert _by_token is not None
    return _by_token.get(token)


def iter_instruments() -> Iterator[Instrument]:
    """Iterate all cached in-scope instruments (loads/refreshes as needed)."""
    if _rows_override is not None:
        yield from _rows_override
        return
    _ensure_loaded()
    assert _rows is not None
    yield from _rows


__all__ = [
    "MASTER_PATH",
    "clear_lookup_override",
    "clear_rows_override",
    "ensure_cache",
    "get_as_of",
    "get_by_token",
    "get_load_generation",
    "get_meta",
    "instrument_to_json",
    "iter_instruments",
    "peek_as_of",
    "set_lookup_override",
    "set_rows_override",
]
