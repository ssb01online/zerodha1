"""Instrument row and slim meta types."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class InstrumentMeta:
    """Slim fields used for portfolio position enrichment."""

    expiry: date | None
    instrument_type: str | None


@dataclass(frozen=True)
class Instrument:
    """Full row from the daily Kite instruments dump (scoped exchanges)."""

    instrument_token: int
    exchange_token: int
    tradingsymbol: str
    name: str
    last_price: float
    expiry: date | None
    strike: float
    tick_size: float
    lot_size: int
    instrument_type: str
    segment: str
    exchange: str

    def meta(self) -> InstrumentMeta:
        itype = self.instrument_type.strip() if self.instrument_type else None
        return InstrumentMeta(expiry=self.expiry, instrument_type=itype or None)
