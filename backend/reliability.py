"""Shared reliability primitives: bounded retry, rate limiting, circuit breaker.

Deliberately small and dependency-free. Clocks and sleep functions are
injectable so every behavior is deterministically testable with fake clocks.
"""

from __future__ import annotations

import asyncio
import enum
import random
import time
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

import structlog

log = structlog.get_logger(__name__)

T = TypeVar("T")


async def retry_async(
    func: Callable[[], Awaitable[T]],
    *,
    attempts: int,
    base_delay_seconds: float,
    jitter_seconds: float = 0.5,
    retry_on: tuple[type[BaseException], ...],
    sleep_func: Callable[[float], Awaitable[None]] = asyncio.sleep,
    on_retry: Callable[[int, float, BaseException], None] | None = None,
) -> T:
    """Run `func` with bounded exponential backoff + jitter.

    Retries ONLY exceptions matching `retry_on`; the final attempt's exception
    propagates. `attempts` is the TOTAL number of tries (1 = no retry).
    """
    if attempts < 1:
        raise ValueError("attempts must be >= 1")
    for attempt in range(1, attempts + 1):
        try:
            return await func()
        except retry_on as exc:
            if attempt == attempts:
                raise
            delay = base_delay_seconds * (2 ** (attempt - 1)) + random.uniform(0, jitter_seconds)
            if on_retry is not None:
                on_retry(attempt, delay, exc)
            await sleep_func(delay)
    raise AssertionError("unreachable")  # pragma: no cover


class TokenBucket:
    """Classic token bucket; capacity equals the per-minute allowance.

    `acquire()` waits (via the injectable sleep) until a token is available —
    it never raises, so a wait is a rate-limit DECISION, not a failure.
    """

    def __init__(
        self,
        requests_per_minute: int,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep_func: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ):
        if requests_per_minute < 1:
            raise ValueError("requests_per_minute must be >= 1")
        self.capacity = float(requests_per_minute)
        self.rate_per_second = self.capacity / 60.0
        self._tokens = self.capacity
        self._updated = clock()
        self._clock = clock
        self._sleep = sleep_func

    def _refill(self) -> None:
        now = self._clock()
        self._tokens = min(self.capacity, self._tokens + (now - self._updated) * self.rate_per_second)
        self._updated = now

    async def acquire(self, tokens: float = 1.0) -> float:
        """Consume tokens; returns the seconds waited (0 when immediate)."""
        waited = 0.0
        while True:
            self._refill()
            if self._tokens >= tokens:
                self._tokens -= tokens
                return waited
            deficit = tokens - self._tokens
            wait = max(deficit / self.rate_per_second, 0.05)
            await self._sleep(wait)
            waited += wait


class CircuitState(enum.Enum):
    closed = "closed"
    open = "open"
    half_open = "half_open"


class CircuitOpenError(Exception):
    """The circuit for this dependency is open; the call was not attempted."""


class CircuitBreaker:
    """Explicit closed -> open -> (cooldown) -> half_open -> closed/open.

    closed: calls allowed; consecutive failures >= threshold -> open.
    open: calls rejected (CircuitOpenError) until cooldown elapses.
    half_open: exactly one probe call allowed; success -> closed, failure -> open.
    """

    def __init__(
        self,
        name: str,
        *,
        failure_threshold: int = 3,
        cooldown_seconds: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
    ):
        if failure_threshold < 1:
            raise ValueError("failure_threshold must be >= 1")
        self.name = name
        self.failure_threshold = failure_threshold
        self.cooldown_seconds = cooldown_seconds
        self._clock = clock
        self.state = CircuitState.closed
        self.failures = 0
        self.opened_at: float | None = None
        self._probe_in_flight = False

    def before_call(self) -> CircuitState:
        """Gate a call; raises CircuitOpenError when the circuit is open."""
        if self.state == CircuitState.open:
            if self._clock() - (self.opened_at or 0.0) >= self.cooldown_seconds:
                self.state = CircuitState.half_open
                self._probe_in_flight = False
            else:
                raise CircuitOpenError(
                    f"circuit {self.name!r} is open; cooldown "
                    f"{self.cooldown_seconds - (self._clock() - (self.opened_at or 0.0)):.1f}s remaining"
                )
        if self.state == CircuitState.half_open:
            if self._probe_in_flight:
                raise CircuitOpenError(f"circuit {self.name!r} is half-open; probe already in flight")
            self._probe_in_flight = True
            return CircuitState.half_open
        return CircuitState.closed

    def record_success(self) -> CircuitState:
        previous, self.state = self.state, CircuitState.closed
        self.failures = 0
        self.opened_at = None
        self._probe_in_flight = False
        if previous != CircuitState.closed:
            log.info("circuit_closed", circuit=self.name, previous=previous.value)
        return self.state

    def record_failure(self) -> CircuitState:
        self.failures += 1
        if self.state == CircuitState.half_open or self.failures >= self.failure_threshold:
            previous, self.state = self.state, CircuitState.open
            self.opened_at = self._clock()
            self._probe_in_flight = False
            log.warning(
                "circuit_opened",
                circuit=self.name,
                failures=self.failures,
                threshold=self.failure_threshold,
                cooldown_seconds=self.cooldown_seconds,
                previous=previous.value,
            )
        return self.state
