"""Run logging, live progress heartbeats, and soft/hard Ctrl-C handling."""

from __future__ import annotations

import logging
import signal
import sys
import time
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable, TextIO

from kitetools import auth

from .paths import LOGS_DIR, ensure_dir, ymd

LOGGER_NAME = "ohlcv"
_FORCE_EXIT_CODE = 130


@dataclass
class InterruptController:
    """First Ctrl-C soft-stops; second forces exit (code 130)."""

    soft_stop: bool = False
    reason: str | None = None
    _prev_handler: Any = None
    _on_soft_stop: Callable[[], None] | None = field(default=None, repr=False)
    _announced: bool = False

    def install(self, *, on_soft_stop: Callable[[], None] | None = None) -> None:
        self._on_soft_stop = on_soft_stop
        self._prev_handler = signal.signal(signal.SIGINT, self._handle)

    def restore(self) -> None:
        if self._prev_handler is not None:
            signal.signal(signal.SIGINT, self._prev_handler)
            self._prev_handler = None

    def _handle(self, signum: int, frame: Any) -> None:
        if self.soft_stop:
            # Second Ctrl-C: restore default and hard-exit.
            signal.signal(signal.SIGINT, signal.SIG_DFL)
            raise SystemExit(_FORCE_EXIT_CODE)
        self.soft_stop = True
        self.reason = "keyboard interrupt"
        if not self._announced:
            self._announced = True
            if self._on_soft_stop is not None:
                self._on_soft_stop()

    def should_stop(self) -> str | None:
        if self.soft_stop:
            return self.reason or "keyboard interrupt"
        return None


@dataclass(frozen=True)
class ContractProgress:
    series_index: int
    series_total: int
    underlying: str
    expiry: date
    contract_index: int
    contract_total: int
    fetched: int
    skipped: int
    errors: int
    gave_up: int
    rows: int
    requests: int
    retries: int
    elapsed_s: float
    tradingsymbol: str = ""
    last_status: str = ""


@dataclass
class ProgressReporter:
    """Console heartbeats (\\r) + structured logging for long fetch/daily runs."""

    logger: logging.Logger
    stream: TextIO = field(default_factory=lambda: sys.stderr)
    heartbeat_interval_s: float = 0.4
    _last_heartbeat: float = field(default=0.0, init=False, repr=False)
    _heartbeat_active: bool = field(default=False, init=False, repr=False)
    _run_started: float = field(default_factory=time.monotonic, init=False, repr=False)
    _contracts_done: int = field(default=0, init=False, repr=False)
    _contracts_total: int = field(default=0, init=False, repr=False)

    def set_totals(self, *, contracts_total: int) -> None:
        self._contracts_total = max(0, contracts_total)
        self._contracts_done = 0
        self._run_started = time.monotonic()

    def clear_heartbeat(self) -> None:
        if self._heartbeat_active and self.stream.isatty():
            self.stream.write("\r" + " " * 120 + "\r")
            self.stream.flush()
        self._heartbeat_active = False

    def note(self, message: str, *, level: int = logging.INFO) -> None:
        self.clear_heartbeat()
        self.logger.log(level, message)

    def soft_stop_announced(self) -> None:
        self.clear_heartbeat()
        msg = (
            "Interrupt received; finishing current contract then stopping. "
            "Press Ctrl-C again to force quit."
        )
        self.logger.warning(msg)
        print(msg, file=self.stream)

    def series_start(
        self,
        *,
        series_index: int,
        series_total: int,
        underlying: str,
        expiry: date,
        contracts: int,
    ) -> None:
        self.clear_heartbeat()
        self.logger.info(
            "series_start %s/%s %s %s contracts=%s",
            series_index,
            series_total,
            ymd(expiry),
            underlying,
            contracts,
        )

    def contract(self, p: ContractProgress) -> None:
        self._contracts_done += 1
        now = time.monotonic()
        line = (
            f"[{p.series_index}/{p.series_total}] {p.underlying} {ymd(p.expiry)}  "
            f"{p.contract_index}/{p.contract_total} contracts  "
            f"fetched={p.fetched} skip={p.skipped} err={p.errors}  "
            f"req={p.requests}  elapsed={_fmt_dur(p.elapsed_s)}"
            f"{_eta_suffix(self._run_started, self._contracts_done, self._contracts_total)}"
        )
        self.logger.debug(
            "contract %s/%s %s %s %s status=%s fetched=%s skip=%s err=%s rows=%s req=%s",
            p.series_index,
            p.series_total,
            ymd(p.expiry),
            p.underlying,
            p.tradingsymbol or p.contract_index,
            p.last_status,
            p.fetched,
            p.skipped,
            p.errors,
            p.rows,
            p.requests,
        )
        if self.stream.isatty():
            if now - self._last_heartbeat >= self.heartbeat_interval_s or p.contract_index >= p.contract_total:
                self.stream.write("\r" + line[:118].ljust(118))
                self.stream.flush()
                self._last_heartbeat = now
                self._heartbeat_active = True
        elif now - self._last_heartbeat >= 5.0:
            print(line, file=self.stream)
            self._last_heartbeat = now

    def series_done(
        self,
        *,
        series_index: int,
        series_total: int,
        underlying: str,
        expiry: date,
        contracts: int,
        fetched: int,
        skipped: int,
        errors: int,
        rows: int,
        elapsed_s: float,
        locked: bool = False,
        stop_reason: str | None = None,
    ) -> None:
        self.clear_heartbeat()
        flag = " LOCKED" if locked else ""
        stop = f" stopped={stop_reason}" if stop_reason else ""
        msg = (
            f"[{series_index}/{series_total}] {ymd(expiry)} {underlying} "
            f"{contracts} contracts fetched={fetched} skipped={skipped} "
            f"rows={rows:,} {elapsed_s:.1f}s{flag}{stop}"
        )
        self.logger.info(msg)
        print(msg, file=self.stream)

    def batch(
        self,
        *,
        batch_index: int,
        batch_total: int,
        contracts_done: int,
        contracts_total: int,
        requests: int,
        elapsed_s: float,
    ) -> None:
        line = (
            f"quote batch {batch_index}/{batch_total}  "
            f"contracts={contracts_done}/{contracts_total}  "
            f"req={requests}  elapsed={_fmt_dur(elapsed_s)}"
        )
        self.logger.info(line)
        if self.stream.isatty():
            self.stream.write("\r" + line[:118].ljust(118))
            self.stream.flush()
            self._heartbeat_active = True
            self._last_heartbeat = time.monotonic()
        else:
            print(line, file=self.stream)


def setup_run_logging(
    command: str,
    *,
    log_file: Path | None = None,
    level: int = logging.INFO,
) -> Path:
    """Configure the ``ohlcv`` logger: file (DEBUG) + stderr (INFO, no heartbeat spam)."""
    ensure_dir(LOGS_DIR)
    if log_file is None:
        stamp = datetime.now(auth.IST).strftime("%Y%m%d_%H%M%S")
        log_file = LOGS_DIR / f"{command}_{stamp}.log"
    else:
        ensure_dir(log_file.parent)

    logger = logging.getLogger(LOGGER_NAME)
    logger.handlers.clear()
    logger.setLevel(logging.DEBUG)
    logger.propagate = False

    fmt = logging.Formatter(
        fmt="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    fh = logging.FileHandler(log_file, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    # Console: INFO+, but ProgressReporter also prints directly for heartbeats.
    sh = logging.StreamHandler(sys.stderr)
    sh.setLevel(level)
    sh.setFormatter(logging.Formatter("%(message)s"))
    # Avoid duplicating series_done lines that ProgressReporter already prints.
    sh.addFilter(_SkipConsoleNoise())
    logger.addHandler(sh)

    logger.info("log_file=%s command=%s", log_file, command)
    return log_file


def get_logger() -> logging.Logger:
    return logging.getLogger(LOGGER_NAME)


class _SkipConsoleNoise(logging.Filter):
    """File gets everything; console logger skips lines ProgressReporter prints itself."""

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        if msg.startswith("[") and "] " in msg and "contracts fetched=" in msg:
            return False
        if msg.startswith("series_start "):
            return False
        if msg.startswith("quote batch "):
            return False
        return True


def _fmt_dur(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.0f}s"
    mins = seconds / 60.0
    if mins < 60:
        return f"{mins:.1f}m"
    return f"{mins / 60.0:.1f}h"


def _eta_suffix(run_started: float, done: int, total: int) -> str:
    if done <= 0 or total <= 0 or done >= total:
        return ""
    elapsed = time.monotonic() - run_started
    if elapsed <= 0:
        return ""
    rate = done / elapsed
    if rate <= 0:
        return ""
    remaining = (total - done) / rate
    return f"  ETA~{_fmt_dur(remaining)}"
