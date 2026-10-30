"""Local-only Kite session status (no API calls)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone

from kitetools.auth import NotLoggedIn, token_expiry
from kitetools.config import TOKEN_FILE


@dataclass(frozen=True)
class SessionStatus:
    valid: bool
    expires_at: datetime | None
    detail: str


def check_session(*, now: datetime | None = None) -> SessionStatus:
    """Inspect ``.env/access_token.json`` expiry without calling Kite."""
    now = now if now is not None else datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    if not TOKEN_FILE.exists():
        return SessionStatus(False, None, "No saved session")
    try:
        record = json.loads(TOKEN_FILE.read_text(encoding="utf-8"))
        login_time = datetime.fromisoformat(record["login_time"])
        expires = token_expiry(login_time)
        if now >= expires:
            return SessionStatus(
                False,
                expires,
                f"Expired at {expires:%Y-%m-%d %H:%M} IST",
            )
        return SessionStatus(
            True,
            expires,
            f"Expires {expires:%H:%M} IST",
        )
    except (OSError, KeyError, ValueError, TypeError, json.JSONDecodeError) as exc:
        return SessionStatus(False, None, f"Invalid token file: {exc}")


def require_valid() -> None:
    """Raise NotLoggedIn if session missing/expired (mirrors auth.load_token)."""
    status = check_session()
    if not status.valid:
        raise NotLoggedIn(status.detail + ". Run: uv run kitetools login")
