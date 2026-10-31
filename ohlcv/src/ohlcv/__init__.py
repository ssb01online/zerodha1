"""OHLCV download, storage, and query (NFO + MCX/CDS markets).

CLI: probe, sync, rank, queue, daily, continuous, fetch, eod, status, show, quality.
Use --market nfo|mcx|cds (default nfo) on market-scoped commands.
Quality API router is mounted by ``portfolio serve``.
"""

import kitetools  # noqa: F401 — inject system SSL trust before any HTTPS

from .api import router as api_router

__all__ = ["api_router"]
