"""OHLCV download, storage, and query (NFO + MCX/CDS markets).

CLI: probe, sync, rank, queue, daily, continuous, fetch, status, show.
Use --market nfo|mcx|cds (default nfo) on market-scoped commands.
"""

import kitetools  # noqa: F401 — inject system SSL trust before any HTTPS

__all__: list[str] = []
