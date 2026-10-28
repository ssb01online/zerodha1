"""OHLCV download, storage, and query (NFO + MCX/CDS markets).

NFO: full pipeline (probe, sync, rank, queue, daily, fetch, status, show).
MCX/CDS Phase 2: sync + queue for FUT into data/ohlcv/{mcx,cds}/ (--market).
Fetch for MCX/CDS lands in Phase 3.
"""

import kitetools  # noqa: F401 — inject system SSL trust before any HTTPS

__all__: list[str] = []
