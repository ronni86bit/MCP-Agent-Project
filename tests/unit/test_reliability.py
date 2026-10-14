"""Phase 6 reliability primitives: bounded retry, token bucket, circuit breaker.

All deterministic — fake clocks and fake sleeps, no real waiting.
"""

import asyncio

import pytest

from backend.reliability import (
    CircuitBreaker,
    CircuitOpenError,
    CircuitState,
    TokenBucket,
    retry_async,
)


class FakeSleeper:
    def __init__(self):
        self.sleeps: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.sleeps.append(seconds)


class FakeClock:
    def __init__(self, start: float = 100.0):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


async def test_retry_transient_then_succeeds():
    calls = {"n": 0}

    async def flaky():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("transient")
        return "ok"

    result = await retry_async(
        flaky, attempts=2, base_delay_seconds=1.0, retry_on=(RuntimeError,),
        sleep_func=FakeSleeper(),
    )
    assert result == "ok"
    assert calls["n"] == 2


async def test_retry_exhaustion_raises_last_error():
    sleeper = FakeSleeper()

    async def always_fails():
        raise ConnectionError("down")

    with pytest.raises(ConnectionError):
        await retry_async(
            always_fails, attempts=3, base_delay_seconds=1.0, jitter_seconds=0.0,
            retry_on=(ConnectionError,), sleep_func=sleeper,
        )
    assert sleeper.sleeps == [1.0, 2.0]  # exponential backoff, no sleep after last attempt


async def test_non_retryable_failure_is_not_retried():
    calls = {"n": 0}

    async def bad_args():
        calls["n"] += 1
        raise ValueError("invalid")

    with pytest.raises(ValueError):
        await retry_async(
            bad_args, attempts=3, base_delay_seconds=1.0, retry_on=(ConnectionError,),
            sleep_func=FakeSleeper(),
        )
    assert calls["n"] == 1


async def test_retry_logs_attempts():
    events = []

    async def fails_once():
        if not events:
            events.append("x")
            raise TimeoutError("t")
        return 1

    await retry_async(
        fails_once, attempts=2, base_delay_seconds=1.0, retry_on=(TimeoutError,),
        sleep_func=FakeSleeper(),
        on_retry=lambda attempt, delay, exc: events.append((attempt, delay)),
    )
    assert events[1][0] == 1 and events[1][1] >= 1.0


async def test_token_bucket_bursts_then_throttles():
    clock = FakeClock()
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        clock.advance(seconds)

    bucket = TokenBucket(2, clock=clock, sleep_func=fake_sleep)  # capacity 2, refill 1/30s
    waited = [await bucket.acquire() for _ in range(4)]
    assert waited[0] == 0.0 and waited[1] == 0.0  # capacity burst of two
    assert any(w > 0 for w in waited[2:]), "beyond capacity the bucket must throttle"


async def test_token_bucket_refills_over_time():
    clock = FakeClock()
    bucket = TokenBucket(60, clock=clock, sleep_func=lambda s: asyncio.sleep(0))
    await bucket.acquire()
    clock.advance(60)  # a full minute: capacity refilled
    waited = await bucket.acquire()
    assert waited == 0.0


def test_circuit_breaker_opens_after_threshold():
    clock = FakeClock()
    breaker = CircuitBreaker("github", failure_threshold=3, cooldown_seconds=60, clock=clock)
    for _ in range(2):
        breaker.before_call()
        breaker.record_failure()
    assert breaker.state == CircuitState.closed
    breaker.before_call()
    breaker.record_failure()
    assert breaker.state == CircuitState.open
    with pytest.raises(CircuitOpenError):
        breaker.before_call()


def test_circuit_breaker_half_open_then_closes_on_success():
    clock = FakeClock()
    breaker = CircuitBreaker("monitoring", failure_threshold=1, cooldown_seconds=10, clock=clock)
    breaker.before_call()
    breaker.record_failure()
    assert breaker.state == CircuitState.open

    clock.advance(11)  # cooldown elapsed
    assert breaker.before_call() == CircuitState.half_open
    breaker.record_success()
    assert breaker.state == CircuitState.closed
    assert breaker.before_call() == CircuitState.closed


def test_circuit_breaker_reopens_when_half_open_probe_fails():
    clock = FakeClock()
    breaker = CircuitBreaker("postgres", failure_threshold=1, cooldown_seconds=10, clock=clock)
    breaker.before_call()
    breaker.record_failure()
    clock.advance(11)
    breaker.state = CircuitState.half_open
    breaker.record_failure()
    assert breaker.state == CircuitState.open
    with pytest.raises(CircuitOpenError):
        breaker.before_call()


def test_circuit_breaker_blocks_concurrent_half_open_probes():
    clock = FakeClock()
    breaker = CircuitBreaker("x", failure_threshold=1, cooldown_seconds=5, clock=clock)
    breaker.before_call()
    breaker.record_failure()
    clock.advance(6)
    assert breaker.before_call() == CircuitState.half_open
    with pytest.raises(CircuitOpenError):
        breaker.before_call()  # second probe while one is in flight
    breaker.record_success()
    assert breaker.state == CircuitState.closed


def test_circuit_breaker_success_resets_failure_count():
    breaker = CircuitBreaker("y", failure_threshold=3, cooldown_seconds=1)
    breaker.before_call()
    breaker.record_failure()
    breaker.before_call()
    breaker.record_success()
    breaker.before_call()
    breaker.record_failure()
    breaker.before_call()
    breaker.record_failure()
    assert breaker.state == CircuitState.closed  # 2 consecutive failures < 3
