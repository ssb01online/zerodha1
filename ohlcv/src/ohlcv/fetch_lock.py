"""Per-market fetch lock: single-flight for fetch / EOD writers.

Lock file: ``data/ohlcv/<market>/fetch.lock`` with pid, started (IST), cmd.
Stale locks (dead PID) are reclaimed. Same-process re-acquire is allowed.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

from kitetools import auth

from .paths import ensure_dir, paths_for


@dataclass
class FetchInProgress(Exception):
    """Another live process holds this market's fetch lock."""

    market_id: str
    pid: int
    started: datetime | None
    cmd: str
    lock_path: Path
    alive: bool = True

    def __str__(self) -> str:
        return format_in_progress(self)


def lock_path_for(market_id: str) -> Path:
    return paths_for(market_id).root / "fetch.lock"


def pid_alive(pid: int) -> bool:
    """True if ``pid`` appears to be a live OS process."""
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


def _parse_lock(text: str) -> tuple[int, datetime | None, str]:
    pid = 0
    started: datetime | None = None
    cmd = ""
    for raw in text.splitlines():
        line = raw.strip()
        if not line or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip().lower()
        value = value.strip()
        if key == "pid":
            try:
                pid = int(value)
            except ValueError:
                pid = 0
        elif key == "started":
            try:
                started = datetime.fromisoformat(value)
            except ValueError:
                started = None
        elif key == "cmd":
            cmd = value
    return pid, started, cmd


def _format_lock(*, pid: int, started: datetime, cmd: str) -> str:
    return f"pid={pid}\nstarted={started.isoformat()}\ncmd={cmd}\n"


def _read_lock(path: Path) -> tuple[int, datetime | None, str] | None:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    return _parse_lock(text)


def format_in_progress(exc: FetchInProgress, *, now: datetime | None = None) -> str:
    """Human-readable refusal for CLI SystemExit."""
    now = now if now is not None else datetime.now(auth.IST)
    if now.tzinfo is None:
        now = now.replace(tzinfo=auth.IST)
    alive_label = "alive" if exc.alive else "dead"
    lines = [
        f"{exc.market_id} fetch already in progress",
        f"  pid: {exc.pid} ({alive_label})",
    ]
    if exc.started is not None:
        started = exc.started
        if started.tzinfo is None:
            started = started.replace(tzinfo=auth.IST)
        age = now - started.astimezone(auth.IST)
        total_s = max(0, int(age.total_seconds()))
        if total_s < 60:
            age_s = f"{total_s}s ago"
        elif total_s < 3600:
            age_s = f"{total_s // 60}m ago"
        else:
            hours = total_s // 3600
            mins = (total_s % 3600) // 60
            age_s = f"{hours}h {mins}m ago" if mins else f"{hours}h ago"
        stamp = started.astimezone(auth.IST).strftime("%Y-%m-%d %H:%M:%S %Z")
        lines.append(f"  started: {stamp} ({age_s})")
    else:
        lines.append("  started: (unknown)")
    if exc.cmd:
        lines.append(f"  cmd: {exc.cmd}")
    lines.append(f"  lock: {exc.lock_path}")
    return "\n".join(lines)


def try_acquire(
    market_id: str,
    *,
    cmd: str,
    lock_path: Path | None = None,
    now: datetime | None = None,
    alive_fn: Callable[[int], bool] | None = None,
) -> Path:
    """Acquire the market fetch lock or raise ``FetchInProgress``.

    Returns the lock path owned by this process. Same-PID re-acquire refreshes
    the file. Dead/corrupt locks are removed and replaced.
    """
    path = lock_path if lock_path is not None else lock_path_for(market_id)
    ensure_dir(path.parent)
    check_alive = alive_fn if alive_fn is not None else pid_alive
    stamp = now if now is not None else datetime.now(auth.IST)
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=auth.IST)
    my_pid = os.getpid()
    payload = _format_lock(pid=my_pid, started=stamp, cmd=cmd)

    for _ in range(3):
        try:
            with open(path, "x", encoding="utf-8") as fh:
                fh.write(payload)
            return path
        except FileExistsError:
            parsed = _read_lock(path)
            if parsed is None:
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass
                continue
            other_pid, started, other_cmd = parsed
            if other_pid == my_pid:
                path.write_text(payload, encoding="utf-8")
                return path
            if other_pid and check_alive(other_pid):
                raise FetchInProgress(
                    market_id=market_id,
                    pid=other_pid,
                    started=started,
                    cmd=other_cmd,
                    lock_path=path,
                    alive=True,
                )
            # Stale or corrupt — reclaim.
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
            continue

    # Last resort after races: refuse if still held by a live peer.
    parsed = _read_lock(path)
    if parsed is not None:
        other_pid, started, other_cmd = parsed
        if other_pid and other_pid != my_pid and check_alive(other_pid):
            raise FetchInProgress(
                market_id=market_id,
                pid=other_pid,
                started=started,
                cmd=other_cmd,
                lock_path=path,
                alive=True,
            )
    path.write_text(payload, encoding="utf-8")
    return path


def release(
    market_id: str,
    *,
    lock_path: Path | None = None,
) -> None:
    """Unlink the lock only if this process still owns it."""
    path = lock_path if lock_path is not None else lock_path_for(market_id)
    try:
        if not path.exists():
            return
        parsed = _read_lock(path)
        if parsed is None:
            return
        other_pid, _started, _cmd = parsed
        if other_pid == os.getpid():
            path.unlink(missing_ok=True)
    except OSError:
        pass
