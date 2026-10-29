"""NSE/BSE equity pre-open quote snapshots.

CLI: ``uv run preopen capture|show``.
API router mounted by ``portfolio serve``.
"""

import kitetools  # noqa: F401 — inject system SSL trust before any HTTPS

from .api import router as api_router

__all__ = ["api_router"]
