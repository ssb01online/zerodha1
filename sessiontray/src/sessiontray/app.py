"""System tray: green = valid Kite token, red = expired/missing."""

from __future__ import annotations

import atexit
import subprocess
import sys
import threading

from kitetools.auth import IST
from kitetools.config import ROOT

from . import lock as lock_mod
from .status import SessionStatus, check_session

POLL_S = 60.0


def _circle_icon(rgb: tuple[int, int, int], size: int = 64):
    from PIL import Image, ImageDraw

    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    margin = 4
    draw.ellipse((margin, margin, size - margin - 1, size - margin - 1), fill=rgb + (255,))
    return img


def _tooltip(status: SessionStatus) -> str:
    if status.valid and status.expires_at is not None:
        exp = status.expires_at.astimezone(IST)
        return f"Kite OK · expires {exp:%H:%M} IST"
    return "Kite token expired · run login"


def _show_toast(title: str, message: str) -> None:
    try:
        from winotify import Notification, audio

        toast = Notification(
            app_id="Kite Session Tray",
            title=title,
            msg=message,
            duration="long",
        )
        toast.set_audio(audio.Default, loop=False)
        toast.show()
    except Exception:
        # Toast is best-effort; tray icon still updates.
        pass


def _spawn_login() -> None:
    """Open a new console running ``uv run kitetools login``."""
    creationflags = 0
    if sys.platform == "win32":
        creationflags = subprocess.CREATE_NEW_CONSOLE  # type: ignore[attr-defined]
    subprocess.Popen(
        ["uv", "run", "kitetools", "login"],
        cwd=str(ROOT),
        creationflags=creationflags,
    )


class TrayApp:
    def __init__(self, *, poll_s: float = POLL_S) -> None:
        import pystray

        self.poll_s = poll_s
        self._status = check_session()
        self._stop = threading.Event()
        self._icon = pystray.Icon(
            "kite_session",
            _circle_icon((34, 139, 34) if self._status.valid else (178, 34, 34)),
            _tooltip(self._status),
            menu=pystray.Menu(
                pystray.MenuItem("Login", self._on_login, default=True),
                pystray.MenuItem("Refresh", self._on_refresh),
                pystray.MenuItem("Quit", self._on_quit),
            ),
        )
        self._toasted_expired = False

    def _apply(self, status: SessionStatus, *, toast_on_expire: bool) -> None:
        prev_valid = self._status.valid
        self._status = status
        self._icon.icon = _circle_icon((34, 139, 34) if status.valid else (178, 34, 34))
        self._icon.title = _tooltip(status)
        if toast_on_expire and prev_valid and not status.valid:
            _show_toast("Kite session expired", "Run Login from the tray, or: uv run kitetools login")
            self._toasted_expired = True
        if status.valid:
            self._toasted_expired = False

    def _on_login(self, icon=None, item=None) -> None:
        _spawn_login()

    def _on_refresh(self, icon=None, item=None) -> None:
        self._apply(check_session(), toast_on_expire=False)

    def _on_quit(self, icon=None, item=None) -> None:
        self._stop.set()
        self._icon.stop()

    def _poll_loop(self) -> None:
        while not self._stop.wait(self.poll_s):
            self._apply(check_session(), toast_on_expire=True)

    def run(self) -> None:
        # Toast once at startup if already expired.
        if not self._status.valid and not self._toasted_expired:
            _show_toast(
                "Kite token expired",
                "No valid session. Use tray Login or: uv run kitetools login",
            )
            self._toasted_expired = True
        thread = threading.Thread(target=self._poll_loop, name="sessiontray-poll", daemon=True)
        thread.start()
        self._icon.run()


def main(argv: list[str] | None = None) -> None:
    argv = argv if argv is not None else sys.argv[1:]
    if argv in (["-h"], ["--help"]):
        print("usage: uv run sessiontray\nMinimal tray: green=valid Kite token, red=expired.")
        return

    if not lock_mod.try_acquire():
        print(
            f"sessiontray already running (lock {lock_mod.LOCK_PATH}).",
            file=sys.stderr,
        )
        raise SystemExit(1)
    atexit.register(lock_mod.release)

    app = TrayApp()
    try:
        app.run()
    finally:
        lock_mod.release()


if __name__ == "__main__":
    main()
