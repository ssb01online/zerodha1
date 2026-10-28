"""Daily Kite instruments cache (NSE/BSE/NFO/MCX/CDS), token lookup, and browser query API.

TODO (future): live quotes, MF/BFO, NSE/BSE scrip pairing.
"""

import kitetools  # noqa: F401 — inject system SSL trust before any HTTPS

from .api import router as api_router
from .cache import (
    MASTER_PATH,
    clear_lookup_override,
    clear_rows_override,
    ensure_cache,
    get_as_of,
    get_by_token,
    get_meta,
    instrument_to_json,
    iter_instruments,
    peek_as_of,
    set_lookup_override,
    set_rows_override,
)
from .models import Instrument, InstrumentMeta
from .scope import IN_SCOPE_EXCHANGES

__all__ = [
    "IN_SCOPE_EXCHANGES",
    "MASTER_PATH",
    "Instrument",
    "InstrumentMeta",
    "api_router",
    "clear_lookup_override",
    "clear_rows_override",
    "ensure_cache",
    "get_as_of",
    "get_by_token",
    "get_meta",
    "instrument_to_json",
    "iter_instruments",
    "peek_as_of",
    "set_lookup_override",
    "set_rows_override",
]
