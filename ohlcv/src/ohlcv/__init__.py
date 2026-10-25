"""NFO OHLCV download, storage, and query.

Phase 0: read-only Kite historical/quote probe.
Phase 1: instruments archive, catalog, rate-limited client, series queue.
Phase 2: end-of-day quote() snapshot (daily/yyyymmdd.parquet) + quote ranking.
"""

import kitetools  # noqa: F401 — inject system SSL trust before any HTTPS

__all__: list[str] = []
