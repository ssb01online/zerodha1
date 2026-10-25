"""Fetch live Kite portfolio payloads or load them from Phase 0 samples."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from kiteconnect.exceptions import TokenException

from kitetools import auth
from kitetools.auth import IST

TOKEN_HINT = "Token is invalid or expired. Run: uv run kitetools login"


class TokenInvalid(Exception):
    """Raised when the Kite session is missing or expired."""

    def __init__(self, message: str | None = None) -> None:
        super().__init__(message or TOKEN_HINT)


@dataclass
class RawPayload:
    holdings: list[dict[str, Any]]
    positions: dict[str, Any]
    mf_holdings: list[dict[str, Any]]
    profile: dict[str, Any] = field(default_factory=dict)
    fetched_at: datetime = field(default_factory=lambda: datetime.now(IST))


def _now_ist() -> datetime:
    return datetime.now(IST)


def fetch_live() -> RawPayload:
    """Hit Kite for holdings, positions, and mf_holdings.

    Raises TokenInvalid (not SystemExit) so Phase 2 can map it to HTTP.
    """
    try:
        kite = auth.get_kite()
        profile = kite.profile()
        holdings = kite.holdings()
        positions = kite.positions()
        mf_holdings = kite.mf_holdings()
    except (auth.NotLoggedIn, TokenException) as exc:
        raise TokenInvalid(f"{TOKEN_HINT}\n({exc})") from exc
    return RawPayload(
        holdings=list(holdings or []),
        positions=positions or {"net": [], "day": []},
        mf_holdings=list(mf_holdings or []),
        profile={
            "user_id": profile.get("user_id"),
            "user_name": profile.get("user_name"),
        },
        fetched_at=_now_ist(),
    )


def load_from_sample(folder: Path) -> RawPayload:
    """Load a Phase 0 sample folder into a RawPayload."""
    folder = Path(folder)
    required = ("holdings.json", "positions.json", "mf_holdings.json", "meta.json")
    missing = [name for name in required if not (folder / name).exists()]
    if missing:
        raise FileNotFoundError(f"{folder} is missing: {', '.join(missing)}")

    holdings = json.loads((folder / "holdings.json").read_text(encoding="utf-8"))
    positions = json.loads((folder / "positions.json").read_text(encoding="utf-8"))
    mf_holdings = json.loads((folder / "mf_holdings.json").read_text(encoding="utf-8"))
    meta = json.loads((folder / "meta.json").read_text(encoding="utf-8"))

    fetched_at_raw = meta.get("fetched_at")
    if fetched_at_raw:
        fetched_at = datetime.fromisoformat(fetched_at_raw)
    else:
        fetched_at = _now_ist()

    return RawPayload(
        holdings=list(holdings or []),
        positions=positions or {"net": [], "day": []},
        mf_holdings=list(mf_holdings or []),
        profile={
            "user_id": meta.get("user_id"),
            "user_name": meta.get("user_name"),
        },
        fetched_at=fetched_at,
    )
