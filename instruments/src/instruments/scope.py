"""Exchange scope for the instruments cache.

In scope (Phase 1): NSE and BSE equity, NFO futures/options.

TODO (future):
- Mutual funds via kite.mf_instruments()
- CDS (currency)
- MCX (commodities)
- BFO (BSE F&O)
- NSE/BSE same-scrip pairing for the browser UI
"""

from __future__ import annotations

IN_SCOPE_EXCHANGES: frozenset[str] = frozenset({"NSE", "BSE", "NFO"})


def in_scope(exchange: str | None) -> bool:
    if not exchange:
        return False
    return str(exchange).strip().upper() in IN_SCOPE_EXCHANGES
