"""Normalized portfolio models (stdlib dataclasses)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .fetch import RawPayload


@dataclass(frozen=True)
class Holding:
    isin: str
    exchange: str
    tradingsymbol: str
    instrument_token: int
    product: str
    quantity: int
    t1_quantity: int
    collateral_quantity: int
    used_quantity: int
    opening_quantity: int
    owned_quantity: int
    average_price: float
    last_price: float
    close_price: float
    pnl: float
    day_change: float
    day_change_percentage: float


@dataclass(frozen=True)
class MfHolding:
    isin: str
    folio: str
    fund: str
    tradingsymbol: str
    quantity: float
    average_price: float
    last_price: float
    pnl: float


@dataclass(frozen=True)
class Position:
    exchange: str
    tradingsymbol: str
    instrument_token: int
    product: str
    segment: str
    quantity: int
    overnight_quantity: int
    multiplier: int
    average_price: float
    last_price: float
    close_price: float
    value: float
    pnl: float
    m2m: float
    realised: float
    unrealised: float
    buy_quantity: int = 0
    buy_price: float = 0.0
    buy_value: float = 0.0
    sell_quantity: int = 0
    sell_price: float = 0.0
    sell_value: float = 0.0
    day_buy_quantity: int = 0
    day_buy_price: float = 0.0
    day_buy_value: float = 0.0
    day_sell_quantity: int = 0
    day_sell_price: float = 0.0
    day_sell_value: float = 0.0


@dataclass(frozen=True)
class PortfolioView:
    fetched_at: datetime
    holdings: tuple[Holding, ...]
    mf_holdings: tuple[MfHolding, ...]
    positions_net: tuple[Position, ...]
    positions_day: tuple[Position, ...]
    raw: RawPayload

    @property
    def counts(self) -> dict[str, int]:
        return {
            "holdings": len(self.holdings),
            "positions_net": len(self.positions_net),
            "positions_day": len(self.positions_day),
            "mf_holdings": len(self.mf_holdings),
        }
