"""Windows tray: green/red Kite access_token status.

CLI: ``uv run sessiontray``

On login (or startup when already valid), may spawn ``ohlcv eod --catch-up``
for markets whose ready time has passed without a ``_eod/*.done`` marker.
"""

import kitetools  # noqa: F401 — inject system SSL trust before any HTTPS

__all__: list[str] = []
