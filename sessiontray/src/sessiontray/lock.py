"""Single-instance lock via PID file under data/."""

from __future__ import annotations

import os
from pathlib import Path

from kitetools.config import DATA_DIR

LOCK_PATH = DATA_DIR / "sessiontray.lock"


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes

        # PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
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


def try_acquire(path: Path = LOCK_PATH) -> bool:
    """Return True if this process owns the lock; False if another instance runs."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        try:
            old = int(path.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            old = 0
        if old and old != os.getpid() and _pid_alive(old):
            return False
    path.write_text(str(os.getpid()), encoding="utf-8")
    return True


def release(path: Path = LOCK_PATH) -> None:
    try:
        if path.exists():
            text = path.read_text(encoding="utf-8").strip()
            if text == str(os.getpid()):
                path.unlink(missing_ok=True)
    except OSError:
        pass
