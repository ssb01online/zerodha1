"""OHLCV download, storage, and query (NFO, MCX, CDS, and NSE/BSE cash equity).

CLI: probe, sync, rank, queue, daily, continuous, fetch, eod, status, show, quality.
Use --market nfo|mcx|cds|nse|bse (default nfo) on market-scoped commands.
NSE and BSE are day candles only.
Quality API router is mounted by ``portfolio serve``.
"""

import kitetools  # noqa: F401 — inject system SSL trust before any HTTPS

from .api import router as api_router

__all__ = ["api_router"]
