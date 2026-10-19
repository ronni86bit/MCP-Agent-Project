"""Phase 8 Gate C: /health (liveness), /ready (readiness + cache), and the
preserved /api/health contract, over the real ASGI app with stub servers and
a duck-typed LLM — no Ollama, no subprocesses beyond the stubs.

PostgreSQL is NOT required here: the state-store fallback is simulated by
setting service.store = None exactly as startup() does when the database is
unreachable, which must never gate readiness.
"""

import asyncio
import uuid

import httpx
import pytest

from backend.agent.llm.adapter import LLMError
from backend.agent.service import AgentService
from backend.config import Settings
from backend.main import create_app
from tests.integration.test_agent_loop import StubMulti, StubServerManager


class ProbeLLM:
    """Duck-typed LLM: check_provider outcome is scriptable, calls counted."""

    model_name = "stub-model"

    def __init__(self, *, up: bool = True):
        self.up = up
        self.checks = 0

    async def check_provider(self) -> None:
        self.checks += 1
        if not self.up:
            raise LLMError("provider unreachable")


def _service(llm: ProbeLLM, *, store_fallback: bool = False) -> AgentService:
    settings = Settings(max_investigation_seconds=90)
    factory = lambda: StubMulti({"postgres-mcp": StubServerManager(["get_daily_sales"])})  # noqa: E731
    service = AgentService(settings, llm=llm, manager_factory=factory)
    if store_fallback:
        # exactly what startup() does when the database is unreachable
        service.store = None
        service.approvals.store = None
    return service


def _app(service: AgentService):
    return create_app(service=service)


async def _client(app, *, lifespan: bool):
    if lifespan:
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
                yield http
    else:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
            yield http


# -- liveness --------------------------------------------------------------------


async def test_health_is_200_without_lifespan_or_dependencies():
    """Liveness needs no service startup and probes nothing: a completely
    uninitialized app (no lifespan, broken deps below) still serves 200."""
    llm = ProbeLLM(up=False)
    app = _app(_service(llm))
    async for http in _client(app, lifespan=False):
        response = await http.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert llm.checks == 0  # liveness never probed anything


# -- readiness -------------------------------------------------------------------


async def test_ready_when_required_dependencies_available():
    llm = ProbeLLM(up=True)
    app = _app(_service(llm))
    async for http in _client(app, lifespan=True):
        response = await http.get("/ready")
    body = response.json()
    assert response.status_code == 200
    assert body["status"] == "ready"
    assert body["checks"]["ollama"]["up"] is True
    assert body["checks"]["mcp_servers"]["mcp"]["up"] is True
    # state store: informational, present, never gating
    assert body["checks"]["state_store"]["gating"] is False
    assert body["checks"]["state_store"]["mode"] == "persistent"


async def test_ready_stays_ready_when_state_store_fell_back_to_memory():
    """PostgreSQL unavailable -> in-memory fallback (startup() semantics).
    The app must NOT become unready: read-only investigations still work."""
    llm = ProbeLLM(up=True)
    app = _app(_service(llm, store_fallback=True))
    async for http in _client(app, lifespan=True):
        response = await http.get("/ready")
    body = response.json()
    assert response.status_code == 200
    assert body["status"] == "ready"
    assert body["checks"]["state_store"] == {"gating": False, "mode": "in_memory_fallback"}


async def test_not_ready_when_llm_unavailable():
    llm = ProbeLLM(up=False)
    app = _app(_service(llm))
    async for http in _client(app, lifespan=True):
        response = await http.get("/ready")
    body = response.json()
    assert response.status_code == 503
    assert body["status"] == "not_ready"
    assert body["checks"]["ollama"]["up"] is False


async def test_not_ready_when_mcp_server_unavailable():
    class BrokenFactory:
        async def __aenter__(self):
            raise LLMError("spawn failed")

        async def __aexit__(self, *exc):
            return False

    settings = Settings(max_investigation_seconds=90)
    service = AgentService(settings, llm=ProbeLLM(up=True), manager_factory=BrokenFactory)
    app = _app(service)
    async for http in _client(app, lifespan=True):
        response = await http.get("/ready")
    body = response.json()
    assert response.status_code == 503
    assert body["status"] == "not_ready"
    assert body["checks"]["mcp_servers"]["mcp"]["up"] is False


async def test_readiness_probe_is_cached_across_requests():
    llm = ProbeLLM(up=True)
    service = _service(llm)
    app = _app(service)

    class FakeClock:
        now = 1000.0

        def __call__(self):
            return self.now

    clock = FakeClock()
    service._readiness._clock = clock  # injectable clock: deterministic TTL
    async for http in _client(app, lifespan=True):
        first = await http.get("/ready")
        second = await http.get("/ready")
        clock.now += 31.0  # past the 30s TTL
        third = await http.get("/ready")

    assert llm.checks == 2  # one shared probe, then a fresh one after expiry
    assert first.json()["cached"] is False
    assert second.json()["cached"] is True
    assert third.json()["cached"] is False


async def test_concurrent_ready_requests_share_one_probe():
    llm = ProbeLLM(up=True)
    service = _service(llm)
    app = _app(service)

    async def _slow_check():
        # widen the race window so concurrent requests park on the in-flight probe
        await asyncio.sleep(0.05)
        llm.checks += 1

    async for http in _client(app, lifespan=True):
        original = llm.check_provider
        llm.check_provider = _slow_check  # type: ignore[method-assign]
        responses = await asyncio.gather(*(http.get("/ready") for _ in range(8)))
        llm.check_provider = original  # type: ignore[method-assign]

    assert all(r.status_code == 200 for r in responses)
    assert llm.checks == 1  # eight concurrent requests, one probe


# -- preserved /api/health contract ------------------------------------------------


async def test_api_health_keeps_its_phase3_response_shape():
    llm = ProbeLLM(up=True)
    app = _app(_service(llm))
    async for http in _client(app, lifespan=True):
        response = await http.get("/api/health")
    body = response.json()
    assert response.status_code == 200
    assert set(body) == {"status", "model", "ollama", "mcp_servers"}
    assert body["status"] == "ok"
    assert body["model"] == "stub-model"
    assert body["ollama"] == "up"
    assert body["mcp_servers"]["mcp"].startswith("up")


# -- registry reuse across repeated app creation ------------------------------------


async def test_repeated_app_creation_keeps_private_registry_and_metrics_serving():
    """Several create_app() cycles in one process (test fixtures do this) must
    keep one working private registry — no duplicate-registration errors, and
    /metrics serves each app without a trailing-slash redirect."""
    bodies = []
    for _ in range(3):
        app = _app(_service(ProbeLLM(up=True)))
        async for http in _client(app, lifespan=True):
            metrics = await http.get("/metrics")
            redirected = await http.get("/metrics", follow_redirects=False)
            ready = await http.get("/ready")
        bodies.append((metrics.status_code, redirected.status_code, ready.status_code))
    assert bodies == [(200, 200, 200)] * 3
    assert "ops_agent_investigations_total" in metrics.text
