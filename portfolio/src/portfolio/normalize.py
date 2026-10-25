"""Normalize raw Kite portfolio payloads into PortfolioView."""

from __future__ import annotations

from typing import Any, Callable

import instruments
from instruments import InstrumentMeta

from .fetch import RawPayload
from .models import Holding, MfHolding, PortfolioView, Position

InstrumentLookup = Callable[[int], InstrumentMeta | None]


def _int(value: Any) -> int:
    if value is None:
        return 0
    return int(value)


def _float(value: Any) -> float:
    if value is None:
        return 0.0
    return float(value)


def _str(value: Any, default: str = "") -> str:
    if value is None:
        return default
    return str(value)


def _holding(row: dict[str, Any]) -> Holding:
    quantity = _int(row.get("quantity"))
    t1_quantity = _int(row.get("t1_quantity"))
    collateral_quantity = _int(row.get("collateral_quantity"))
    return Holding(
        isin=_str(row.get("isin")),
        exchange=_str(row.get("exchange")),
        tradingsymbol=_str(row.get("tradingsymbol")),
        instrument_token=_int(row.get("instrument_token")),
        product=_str(row.get("product")),
        quantity=quantity,
        t1_quantity=t1_quantity,
        collateral_quantity=collateral_quantity,
        used_quantity=_int(row.get("used_quantity")),
        opening_quantity=_int(row.get("opening_quantity")),
        owned_quantity=quantity + t1_quantity + collateral_quantity,
        average_price=_float(row.get("average_price")),
        last_price=_float(row.get("last_price")),
        close_price=_float(row.get("close_price")),
        pnl=_float(row.get("pnl")),
        day_change=_float(row.get("day_change")),
        day_change_percentage=_float(row.get("day_change_percentage")),
    )


def _mf_holding(row: dict[str, Any]) -> MfHolding:
    return MfHolding(
        isin=_str(row.get("isin")),
        folio=_str(row.get("folio")),
        fund=_str(row.get("fund")),
        tradingsymbol=_str(row.get("tradingsymbol")),
        quantity=_float(row.get("quantity")),
        average_price=_float(row.get("average_price")),
        last_price=_float(row.get("last_price")),
        pnl=_float(row.get("pnl")),
    )


def _position(row: dict[str, Any], lookup: InstrumentLookup) -> Position:
    token = _int(row.get("instrument_token"))
    meta = lookup(token) if token else None
    return Position(
        exchange=_str(row.get("exchange")),
        tradingsymbol=_str(row.get("tradingsymbol")),
        instrument_token=token,
        product=_str(row.get("product")),
        segment=_str(row.get("segment")),
        quantity=_int(row.get("quantity")),
        overnight_quantity=_int(row.get("overnight_quantity")),
        multiplier=_int(row.get("multiplier") if row.get("multiplier") is not None else 1),
        average_price=_float(row.get("average_price")),
        last_price=_float(row.get("last_price")),
        close_price=_float(row.get("close_price")),
        value=_float(row.get("value")),
        pnl=_float(row.get("pnl")),
        m2m=_float(row.get("m2m")),
        realised=_float(row.get("realised")),
        unrealised=_float(row.get("unrealised")),
        buy_quantity=_int(row.get("buy_quantity")),
        buy_price=_float(row.get("buy_price")),
        buy_value=_float(row.get("buy_value")),
        sell_quantity=_int(row.get("sell_quantity")),
        sell_price=_float(row.get("sell_price")),
        sell_value=_float(row.get("sell_value")),
        day_buy_quantity=_int(row.get("day_buy_quantity")),
        day_buy_price=_float(row.get("day_buy_price")),
        day_buy_value=_float(row.get("day_buy_value")),
        day_sell_quantity=_int(row.get("day_sell_quantity")),
        day_sell_price=_float(row.get("day_sell_price")),
        day_sell_value=_float(row.get("day_sell_value")),
        expiry=meta.expiry if meta else None,
        instrument_type=meta.instrument_type if meta else None,
    )


def to_view(
    raw: RawPayload,
    *,
    lookup: InstrumentLookup | None = None,
) -> PortfolioView:
    resolve = lookup if lookup is not None else instruments.get_meta
    positions = raw.positions or {}
    net = positions.get("net") or []
    day = positions.get("day") or []
    return PortfolioView(
        fetched_at=raw.fetched_at,
        holdings=tuple(_holding(r) for r in (raw.holdings or [])),
        mf_holdings=tuple(_mf_holding(r) for r in (raw.mf_holdings or [])),
        positions_net=tuple(_position(r, resolve) for r in net),
        positions_day=tuple(_position(r, resolve) for r in day),
        raw=raw,
    )
