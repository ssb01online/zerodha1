"""OHLCV download, storage, and query (NFO + MCX/CDS markets).

NFO: full pipeline (probe, sync, rank, queue, daily, fetch, status, show).
MCX/CDS: sync/queue/fetch/status/show for FUT+options (--market, --types).
Rank/daily remain NFO-only.
"""

import kitetools  # noqa: F401 — inject system SSL trust before any HTTPS

__all__: list[str] = []
