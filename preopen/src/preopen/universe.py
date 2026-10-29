"""NSE/BSE equity universe for pre-open capture."""

from __future__ import annotations

from dataclasses import dataclass

from instruments import Instrument, ensure_cache, iter_instruments

EQ_EXCHANGES = frozenset({"NSE", "BSE"})


@dataclass(frozen=True)
class EquityRow:
    token: int
    exchange: str
    tradingsymbol: str
    name: str

    @property
    def quote_key(self) -> str:
        return f"{self.exchange}:{self.tradingsymbol}"


def _is_equity(inst: Instrument) -> bool:
    exchange = (inst.exchange or "").strip().upper()
    itype = (inst.instrument_type or "").strip().upper()
    return exchange in EQ_EXCHANGES and itype == "EQ"


def list_equity(rows: list[Instrument] | None = None) -> list[EquityRow]:
    """Return NSE/BSE EQ instruments sorted by exchange, tradingsymbol."""
    if rows is None:
        ensure_cache()
        source = list(iter_instruments())
    else:
        source = rows
    out = [
        EquityRow(
            token=int(inst.instrument_token),
            exchange=inst.exchange.strip().upper(),
            tradingsymbol=inst.tradingsymbol.strip(),
            name=(inst.name or "").strip(),
        )
        for inst in source
        if _is_equity(inst)
    ]
    out.sort(key=lambda r: (r.exchange, r.tradingsymbol))
    return out
