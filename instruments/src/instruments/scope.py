"""Exchange scope for the instruments cache.

In scope: NSE and BSE equity, NFO / MCX / CDS futures and options.

TODO (future):
- Mutual funds via kite.mf_instruments()
- BFO (BSE F&O)
- NSE/BSE same-scrip pairing for the browser UI
- Live quotes / LTP enrichment for the browser
"""

from __future__ import annotations

IN_SCOPE_EXCHANGES: frozenset[str] = frozenset({"NSE", "BSE", "NFO", "MCX", "CDS"})


def in_scope(exchange: str | None) -> bool:
    if not exchange:
        return False
    return str(exchange).strip().upper() in IN_SCOPE_EXCHANGES
