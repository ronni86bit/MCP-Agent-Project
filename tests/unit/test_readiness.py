"""Phase 8 Gate C: readiness cache semantics and readiness derivation.

Deterministic — the cache clock is fake, probes are counters; no network,
no Postgres, no subprocesses.
"""

import asyncio

import pytest

from backend.agent.service import ReadinessCache, readiness_from_health


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _probe_counter(calls: list[int], result: dict | None = None):
    async def _probe() -> dict:
        calls.append(1)
        return result if result is not None else {"ollama": "up", "mcp_servers": {}}
    return _probe


# -- ReadinessCache: TTL + single flight ---------------------------------------


async def test_first_call_probes_and_second_within_ttl_is_cached():
    clock = FakeClock()
    cache = ReadinessCache(ttl_seconds=30.0, clock=clock)
    calls: list[int] = []
    probe = _probe_counter(calls)

    snapshot, cached = await cache.get(probe)
    assert snapshot == {"ollama": "up", "mcp_servers": {}}
    assert cached is False
    snapshot, cached = await cache.get(probe)
    assert cached is True
    assert len(calls) == 1  # no second probe inside the TTL

    clock.advance(29.9)  # just inside the TTL: still fresh
    _, cached = await cache.get(probe)
    assert cached is True
    clock.advance(0.2)  # past expiry: probe again
    _, cached = await cache.get(probe)
    assert cached is False
    assert len(calls) == 2


async def test_concurrent_callers_share_one_probe():
    cache = ReadinessCache(ttl_seconds=30.0, clock=FakeClock())
    calls: list[int] = []

    started = asyncio.Event()
    release = asyncio.Event()

    async def slow_probe() -> dict:
        calls.append(1)
        started.set()
        await release.wait()
        return {"ollama": "up", "mcp_servers": {}}

    async def _bang():
        task = asyncio.ensure_future(cache.get(slow_probe))
        await started.wait()
        others = [asyncio.ensure_future(cache.get(slow_probe)) for _ in range(9)]
        await asyncio.sleep(0.05)  # let all nine park on the in-flight probe
        assert cache.probe_in_flight
        release.set()
        results = [await t for t in [task, *others]]
        return results

    results = await _bang()
    assert all(snapshot == {"ollama": "up", "mcp_servers": {}} for snapshot, _ in results)
    assert results[0][1] is False  # the first caller performed the probe
    assert len(calls) == 1  # every other caller shared it — no probe storm
    assert cache.probe_in_flight is False


async def test_invalidate_forces_a_fresh_probe():
    clock = FakeClock()
    cache = ReadinessCache(ttl_seconds=30.0, clock=clock)
    calls: list[int] = []
    probe = _probe_counter(calls)

    await cache.get(probe)
    cache.invalidate()
    await cache.get(probe)
    assert len(calls) == 2


async def test_failed_probe_is_not_cached():
    clock = FakeClock()
    cache = ReadinessCache(ttl_seconds=30.0, clock=clock)

    async def failing_probe() -> dict:
        raise RuntimeError("probe exploded")

    with pytest.raises(RuntimeError):
        await cache.get(failing_probe)
    # nothing cached: the next caller probes again
    calls: list[int] = []
    _, cached = await cache.get(_probe_counter(calls))
    assert cached is False and len(calls) == 1


def test_nonpositive_ttl_is_rejected():
    with pytest.raises(ValueError):
        ReadinessCache(ttl_seconds=0)


# -- readiness derivation -------------------------------------------------------

def _snapshot(ollama: str, mcp: dict) -> dict:
    return {"ollama": ollama, "mcp_servers": mcp}


def test_ready_when_llm_and_all_mcp_up():
    ready, checks = readiness_from_health(
        _snapshot("up", {"postgres-mcp": "up (5 tools)", "github-mcp": "up (4 tools)"})
    )
    assert ready is True
    assert checks["ollama"]["up"] is True
    assert all(c["up"] for c in checks["mcp_servers"].values())


def test_not_ready_when_llm_down_even_if_mcp_up():
    ready, checks = readiness_from_health(_snapshot("down: connection refused", {"mcp": "up (9 tools)"}))
    assert ready is False
    assert checks["ollama"]["up"] is False


def test_not_ready_when_any_mcp_down_even_if_llm_up():
    ready, _ = readiness_from_health(
        _snapshot("up", {"postgres-mcp": "up (5 tools)", "monitoring-mcp": "down: TransportError"})
    )
    assert ready is False


def test_not_ready_on_missing_probe_results():
    ready, _ = readiness_from_health({})
    assert ready is False


def test_mcp_up_detection_requires_up_prefix_not_substring():
    ready, checks = readiness_from_health(_snapshot("up", {"mcp": "down: up was here"}))
    assert ready is False
    assert checks["mcp_servers"]["mcp"]["up"] is False


# -- cancellation vs investigation metric (Gate C documented semantics) ---------


async def test_hard_cancellation_propagates_and_is_not_counted():
    """Regression: cancelling run() must still propagate CancelledError (the
    orchestrator boundary never converts it), and the cancelled run must NOT
    be counted as a terminal investigation outcome — the metric measures
    normal terminal returns only; the startup sweep later persists such rows
    as `interrupted`."""
    import uuid

    from backend.agent.loop import InvestigationLoop
    from backend.agent.state import InvestigationRecord
    from backend.config import Settings
    from backend.observability.metrics import REGISTRY

    class HangingFactory:
        async def __aenter__(self):
            await asyncio.Event().wait()  # never ready

        async def __aexit__(self, *exc):
            return False

    record = InvestigationRecord(
        investigation_id=f"inv-{uuid.uuid4().hex[:8]}",
        request_id=f"req-{uuid.uuid4().hex[:8]}",
        question="Why did today's sales drop?",
    )
    loop = InvestigationLoop(
        llm=type("L", (), {"model_name": "t"})(),
        manager_factory=HangingFactory,
        record=record,
        settings=Settings(max_investigation_seconds=60),
    )

    def _terminal_total() -> float:
        return (REGISTRY.get_sample_value("ops_agent_investigations_total", {"status": "complete"}) or 0.0) + (
            REGISTRY.get_sample_value("ops_agent_investigations_total", {"status": "failed"}) or 0.0
        )

    before = _terminal_total()
    task = asyncio.ensure_future(loop.run())
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert record.status == "running"  # never finalized: not a terminal return
    assert _terminal_total() == before  # cancellation is invisible to the metric
