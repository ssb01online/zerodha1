"""In-memory query indexes over the daily instruments master (browser API)."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from typing import Any

from . import cache
from .models import Instrument
from .scope import IN_SCOPE_EXCHANGES


@dataclass(frozen=True)
class UnderlyingInfo:
    name: str
    fut_count: int
    ce_count: int
    pe_count: int

    @property
    def total(self) -> int:
        return self.fut_count + self.ce_count + self.pe_count


@dataclass
class _Indexes:
    generation: int
    as_of: date | None
    rows: list[Instrument]
    by_exchange_type: dict[str, int]
    underlyings: dict[str, UnderlyingInfo]
    # underlying -> sorted expiry dates
    expiries: dict[str, list[date]]
    # (underlying, expiry_iso) -> fut + strikes
    chains: dict[tuple[str, str], tuple[Instrument | None, dict[float, dict[str, Instrument]]]]


_indexes: _Indexes | None = None


def _match_q(haystack: str, q: str) -> bool:
    if not q:
        return True
    return q in haystack.casefold()


def _rebuild() -> _Indexes:
    cache.ensure_cache()
    rows = list(cache.iter_instruments())
    by_exchange_type: dict[str, int] = defaultdict(int)
    u_fut: dict[str, int] = defaultdict(int)
    u_ce: dict[str, int] = defaultdict(int)
    u_pe: dict[str, int] = defaultdict(int)
    expiry_sets: dict[str, set[date]] = defaultdict(set)
    # (underlying, expiry_iso) -> (fut, strike -> {CE/PE: inst})
    raw_chains: dict[
        tuple[str, str], tuple[Instrument | None, dict[float, dict[str, Instrument]]]
    ] = {}

    def chain_slot(underlying: str, exp: date) -> tuple[
        Instrument | None, dict[float, dict[str, Instrument]]
    ]:
        key = (underlying, exp.isoformat())
        if key not in raw_chains:
            raw_chains[key] = (None, {})
        return raw_chains[key]

    for inst in rows:
        by_exchange_type[f"{inst.exchange}:{inst.instrument_type or '?'}"] += 1
        if inst.exchange != "NFO":
            continue
        name = (inst.name or "").strip()
        if not name or inst.expiry is None:
            continue
        itype = (inst.instrument_type or "").upper()
        if itype == "FUT":
            u_fut[name] += 1
            expiry_sets[name].add(inst.expiry)
            fut, strikes = chain_slot(name, inst.expiry)
            raw_chains[(name, inst.expiry.isoformat())] = (inst, strikes)
        elif itype in ("CE", "PE"):
            if itype == "CE":
                u_ce[name] += 1
            else:
                u_pe[name] += 1
            expiry_sets[name].add(inst.expiry)
            fut, strikes = chain_slot(name, inst.expiry)
            by_strike = strikes.setdefault(inst.strike, {})
            by_strike[itype] = inst

    underlyings = {
        name: UnderlyingInfo(
            name=name,
            fut_count=u_fut.get(name, 0),
            ce_count=u_ce.get(name, 0),
            pe_count=u_pe.get(name, 0),
        )
        for name in sorted(set(u_fut) | set(u_ce) | set(u_pe))
    }
    expiries = {name: sorted(dates) for name, dates in expiry_sets.items()}

    return _Indexes(
        generation=cache.get_load_generation(),
        as_of=cache.get_as_of(),
        rows=rows,
        by_exchange_type=dict(by_exchange_type),
        underlyings=underlyings,
        expiries=expiries,
        chains=raw_chains,
    )


def _ensure_indexes() -> _Indexes:
    global _indexes
    gen = cache.get_load_generation()
    if _indexes is not None and _indexes.generation == gen:
        return _indexes
    _indexes = _rebuild()
    return _indexes


def invalidate_indexes() -> None:
    """Drop cached indexes (tests). Next query rebuilds."""
    global _indexes
    _indexes = None


def meta() -> dict[str, Any]:
    idx = _ensure_indexes()
    by_exchange: dict[str, int] = defaultdict(int)
    by_type: dict[str, int] = defaultdict(int)
    for key, count in idx.by_exchange_type.items():
        exchange, itype = key.split(":", 1)
        by_exchange[exchange] += count
        by_type[itype] += count
    return {
        "as_of_date": idx.as_of.isoformat() if idx.as_of else None,
        "total": len(idx.rows),
        "by_exchange": dict(sorted(by_exchange.items())),
        "by_instrument_type": dict(sorted(by_type.items())),
        "scope_exchanges": sorted(IN_SCOPE_EXCHANGES),
        "data_note": "Daily instruments dump; last_price is not live.",
    }


def get(token: int) -> Instrument | None:
    """Full row by token (uses cache / rows override)."""
    return cache.get_by_token(token)


def search(
    q: str = "",
    exchange: str | None = None,
    instrument_type: str | None = None,
    *,
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    """Paginated instrument search. Empty q matches all (after filters)."""
    idx = _ensure_indexes()
    q_fold = (q or "").strip().casefold()
    ex = (exchange or "").strip().upper() or None
    itype = (instrument_type or "").strip().upper() or None
    limit = max(1, min(int(limit), 500))
    offset = max(0, int(offset))

    matched: list[Instrument] = []
    for inst in idx.rows:
        if ex is not None and inst.exchange != ex:
            continue
        if itype is not None and (inst.instrument_type or "").upper() != itype:
            continue
        if q_fold:
            hay = f"{inst.tradingsymbol} {inst.name}".casefold()
            if q_fold not in hay:
                continue
        matched.append(inst)

    matched.sort(key=lambda r: (r.exchange, r.tradingsymbol))
    total = len(matched)
    page = matched[offset : offset + limit]
    return {
        "as_of_date": idx.as_of.isoformat() if idx.as_of else None,
        "total": total,
        "limit": limit,
        "offset": offset,
        "items": [cache.instrument_to_json(r) for r in page],
    }


def underlyings(q: str = "", *, limit: int = 100) -> dict[str, Any]:
    idx = _ensure_indexes()
    q_fold = (q or "").strip().casefold()
    limit = max(1, min(int(limit), 500))
    items = [
        {
            "name": u.name,
            "fut_count": u.fut_count,
            "ce_count": u.ce_count,
            "pe_count": u.pe_count,
            "total": u.total,
        }
        for u in idx.underlyings.values()
        if _match_q(u.name, q_fold)
    ]
    items.sort(key=lambda x: (-x["total"], x["name"]))
    return {
        "as_of_date": idx.as_of.isoformat() if idx.as_of else None,
        "total": len(items),
        "items": items[:limit],
    }


def expiries(underlying: str) -> dict[str, Any]:
    idx = _ensure_indexes()
    name = (underlying or "").strip()
    dates = idx.expiries.get(name, [])
    return {
        "underlying": name,
        "as_of_date": idx.as_of.isoformat() if idx.as_of else None,
        "expiries": [d.isoformat() for d in dates],
    }


def chain(underlying: str, expiry: str) -> dict[str, Any]:
    idx = _ensure_indexes()
    name = (underlying or "").strip()
    exp = (expiry or "").strip()[:10]
    fut, strikes_map = idx.chains.get((name, exp), (None, {}))
    strikes_out: list[dict[str, Any]] = []
    for strike in sorted(strikes_map.keys()):
        legs = strikes_map[strike]
        row: dict[str, Any] = {"strike": strike}
        if "CE" in legs:
            row["ce"] = cache.instrument_to_json(legs["CE"])
        if "PE" in legs:
            row["pe"] = cache.instrument_to_json(legs["PE"])
        strikes_out.append(row)
    return {
        "underlying": name,
        "expiry": exp,
        "as_of_date": idx.as_of.isoformat() if idx.as_of else None,
        "fut": cache.instrument_to_json(fut) if fut is not None else None,
        "strikes": strikes_out,
    }


__all__ = [
    "UnderlyingInfo",
    "chain",
    "expiries",
    "get",
    "invalidate_indexes",
    "meta",
    "search",
    "underlyings",
]
