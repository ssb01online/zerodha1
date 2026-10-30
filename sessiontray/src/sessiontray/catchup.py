"""Spawn ``ohlcv eod --catch-up`` without importing ohlcv (thin tray)."""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from kitetools.auth import IST
from kitetools.config import DATA_DIR, ROOT

EOD_DIR = DATA_DIR / "ohlcv" / "_eod"
CATCHUP_LOCK_PATH = EOD_DIR / "catchup.lock"


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes

        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
        if handle:
            ctypes.windll.kernel32.CloseHandle(handle)
            return True
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def catchup_lock_held(*, lock_path: Path = CATCHUP_LOCK_PATH) -> bool:
    """True if another live process owns the catch-up PID lock."""
    if not lock_path.exists():
        return False
    try:
        old = int(lock_path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return False
    return _pid_alive(old)


def spawn_catchup(*, cwd: Path | None = None) -> tuple[bool, str]:
    """Start catch-up in the background; log under ``data/ohlcv/_eod/``.

    Returns ``(started, message)``. Does not wait for completion.
    """
    if catchup_lock_held():
        return False, "EOD catch-up already running"

    eod_dir = EOD_DIR
    eod_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(IST).strftime("%Y%m%d-%H%M%S")
    log_path = eod_dir / f"catchup-{stamp}.log"
    work = cwd if cwd is not None else ROOT

    try:
        if sys.platform == "win32":
            # Match eod-*.cmd: shell redirect so the log fd outlives this call.
            creationflags = subprocess.CREATE_NO_WINDOW  # type: ignore[attr-defined]
            subprocess.Popen(
                [
                    "cmd.exe",
                    "/c",
                    f'uv run ohlcv eod --catch-up >> "{log_path}" 2>&1',
                ],
                cwd=str(work),
                creationflags=creationflags,
            )
        else:
            log_fh = open(log_path, "a", encoding="utf-8")
            subprocess.Popen(
                ["uv", "run", "ohlcv", "eod", "--catch-up"],
                cwd=str(work),
                stdout=log_fh,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            # Child inherits the fd; closing here can break some platforms — leave open.
    except OSError as exc:
        return False, f"Failed to start catch-up: {exc}"

    return True, f"EOD catch-up started → {log_path.name}"
