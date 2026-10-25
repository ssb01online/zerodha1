"""Rate-limited Kite historical/quote client with retry and backoff."""

from __future__ import annotations

import random
import time
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Callable, Protocol

from kiteconnect import KiteConnect
from kiteconnect.exceptions import (
    DataException,
    GeneralException,
    InputException,
    NetworkException,
    PermissionException,
    TokenException,
)

HIST_MIN_INTERVAL = 0.34  # ~3 req/s
QUOTE_MIN_INTERVAL = 1.0  # 1 req/s
MAX_ATTEMPTS = 6
BACKOFF_BASES = (1.0, 2.0, 4.0, 8.0, 16.0, 32.0)
BACKOFF_CAP = 60.0
RATE_LIMIT_MIN_WAIT = 5.0
JITTER_FRAC = 0.20


class SessionExpired(Exception):
    """Kite session invalid/expired — caller should exit with login hint."""


class RequestRejected(Exception):
    """Non-retryable request error (bad input / permission)."""

    def __init__(self, message: str, *, cause: BaseException | None = None) -> None:
        super().__init__(message)
        self.cause = cause


class RetryExhausted(Exception):
    """Retries exhausted for a retryable error."""

    def __init__(
        self,
        message: str,
        *,
        token: int | None = None,
        last_error: BaseException | None = None,
    ) -> None:
        super().__init__(message)
        self.token = token
        self.last_error = last_error


class Clock(Protocol):
    def monotonic(self) -> float: ...

    def sleep(self, seconds: float) -> None: ...


@dataclass
class RealClock:
    def monotonic(self) -> float:
        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        if seconds > 0:
            time.sleep(seconds)


@dataclass
class ClientCounters:
    requests: int = 0
    retries: int = 0
    errors: int = 0


@dataclass
class KiteClient:
    """Thin wrapper around KiteConnect with pacing and retries."""

    kite: KiteConnect
    hist_interval: float = HIST_MIN_INTERVAL
    quote_interval: float = QUOTE_MIN_INTERVAL
    clock: Clock = field(default_factory=RealClock)
    rng: random.Random = field(default_factory=random.Random)
    counters: ClientCounters = field(default_factory=ClientCounters)
    _last_hist: float = field(default=0.0, init=False, repr=False)
    _last_quote: float = field(default=0.0, init=False, repr=False)

    def _wait(self, last: float, minimum: float) -> None:
        gap = minimum - (self.clock.monotonic() - last)
        if gap > 0:
            self.clock.sleep(gap)

    def _backoff_seconds(self, attempt_index: int, *, rate_limited: bool) -> float:
        base = BACKOFF_BASES[min(attempt_index, len(BACKOFF_BASES) - 1)]
        wait = min(base, BACKOFF_CAP)
        if rate_limited:
            wait = max(wait, RATE_LIMIT_MIN_WAIT)
        jitter = wait * JITTER_FRAC * (2.0 * self.rng.random() - 1.0)
        return max(0.0, wait + jitter)

    def _is_rate_limited(self, exc: BaseException) -> bool:
        text = str(exc).lower()
        return "429" in text or "rate" in text or "too many" in text

    def _is_retryable(self, exc: BaseException) -> bool:
        if isinstance(exc, (NetworkException, DataException, GeneralException)):
            return True
        # requests / urllib3 connection errors often surface as generic Exception subclasses
        name = type(exc).__name__.lower()
        if "timeout" in name or "connection" in name:
            return True
        text = str(exc).lower()
        if "timeout" in text or "connection" in text:
            return True
        return False

    def _call_with_retry(
        self,
        fn: Callable[[], Any],
        *,
        token: int | None = None,
        pace: Callable[[], None],
        mark_paced: Callable[[], None],
    ) -> Any:
        last_error: BaseException | None = None
        for attempt in range(MAX_ATTEMPTS):
            pace()
            self.counters.requests += 1
            try:
                result = fn()
                mark_paced()
                return result
            except TokenException as exc:
                mark_paced()
                raise SessionExpired(str(exc)) from exc
            except (InputException, PermissionException) as exc:
                mark_paced()
                self.counters.errors += 1
                raise RequestRejected(str(exc), cause=exc) from exc
            except Exception as exc:
                mark_paced()
                last_error = exc
                if not self._is_retryable(exc):
                    self.counters.errors += 1
                    raise RequestRejected(str(exc), cause=exc) from exc
                if attempt >= MAX_ATTEMPTS - 1:
                    break
                self.counters.retries += 1
                wait = self._backoff_seconds(
                    attempt,
                    rate_limited=self._is_rate_limited(exc),
                )
                self.clock.sleep(wait)
        self.counters.errors += 1
        raise RetryExhausted(
            f"retries exhausted after {MAX_ATTEMPTS} attempts: {last_error}",
            token=token,
            last_error=last_error,
        ) from last_error

    def historical(
        self,
        instrument_token: int,
        from_date: datetime | date | str,
        to_date: datetime | date | str,
        interval: str,
        *,
        oi: bool = False,
        continuous: bool = False,
    ) -> list[dict[str, Any]]:
        def pace() -> None:
            self._wait(self._last_hist, self.hist_interval)

        def mark() -> None:
            self._last_hist = self.clock.monotonic()

        def call() -> list[dict[str, Any]]:
            return self.kite.historical_data(
                instrument_token,
                from_date,
                to_date,
                interval,
                continuous=continuous,
                oi=oi,
            )

        return self._call_with_retry(call, token=instrument_token, pace=pace, mark_paced=mark)

    def quote(self, instruments: list[str]) -> dict[str, Any]:
        def pace() -> None:
            self._wait(self._last_quote, self.quote_interval)

        def mark() -> None:
            self._last_quote = self.clock.monotonic()

        def call() -> dict[str, Any]:
            return self.kite.quote(instruments)

        return self._call_with_retry(call, pace=pace, mark_paced=mark)
