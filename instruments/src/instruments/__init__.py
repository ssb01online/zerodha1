"""Daily Kite instruments cache (NSE/BSE/NFO) and slim token lookup for portfolio.

TODO (Phase 2+): browser UI/API, option chains, live quotes, MF/CDS/MCX/BFO,
NSE/BSE scrip pairing.
"""

import kitetools  # noqa: F401 — inject system SSL trust before any HTTPS

from .cache import (
    MASTER_PATH,
    clear_lookup_override,
    ensure_cache,
    get_by_token,
    get_meta,
    iter_instruments,
    set_lookup_override,
)
from .models import Instrument, InstrumentMeta
from .scope import IN_SCOPE_EXCHANGES

__all__ = [
    "IN_SCOPE_EXCHANGES",
    "MASTER_PATH",
    "Instrument",
    "InstrumentMeta",
    "clear_lookup_override",
    "ensure_cache",
    "get_by_token",
    "get_meta",
    "iter_instruments",
    "set_lookup_override",
]
