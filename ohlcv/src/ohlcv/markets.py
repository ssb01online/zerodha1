"""Per-exchange OHLCV market profiles (session hours, sync types, continuous day).

Session end times are cushions after the last observed minute bar (Phase 0 probes).
``sync_instrument_types``: None = all types with expiry (NFO); frozenset for FUT-only markets.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import time


@dataclass(frozen=True)
class MarketProfile:
    id: str  # "nfo" | "mcx" | "cds"
    exchange: str  # "NFO" | "MCX" | "CDS"
    session_start: time
    session_end: time  # inclusive cushion after last observed minute
    continuous_day: bool
    # None → upsert every in-exchange row that has an expiry (NFO FUT+OPT).
    sync_instrument_types: frozenset[str] | None = None


NFO = MarketProfile(
    id="nfo",
    exchange="NFO",
    session_start=time(9, 15),
    session_end=time(15, 40),
    continuous_day=True,
    sync_instrument_types=None,
)
MCX = MarketProfile(
    id="mcx",
    exchange="MCX",
    session_start=time(9, 0),
    session_end=time(23, 30),
    continuous_day=True,
    sync_instrument_types=frozenset({"FUT"}),
)
CDS = MarketProfile(
    id="cds",
    exchange="CDS",
    session_start=time(9, 0),
    session_end=time(17, 0),
    continuous_day=True,
    sync_instrument_types=frozenset({"FUT"}),
)

MARKETS: dict[str, MarketProfile] = {
    NFO.id: NFO,
    MCX.id: MCX,
    CDS.id: CDS,
}

_BY_EXCHANGE: dict[str, MarketProfile] = {
    NFO.exchange: NFO,
    MCX.exchange: MCX,
    CDS.exchange: CDS,
}


def get_market(market_id: str) -> MarketProfile:
    key = (market_id or "").strip().lower()
    try:
        return MARKETS[key]
    except KeyError as exc:
        known = ", ".join(sorted(MARKETS))
        raise ValueError(f"unknown market_id={market_id!r}; expected one of: {known}") from exc


def get_market_by_exchange(exchange: str) -> MarketProfile:
    key = (exchange or "").strip().upper()
    try:
        return _BY_EXCHANGE[key]
    except KeyError as exc:
        known = ", ".join(sorted(_BY_EXCHANGE))
        raise ValueError(
            f"unknown exchange={exchange!r}; expected one of: {known}"
        ) from exc
