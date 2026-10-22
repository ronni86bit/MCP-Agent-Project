"""Phase 10 Gate 2: SSE event-stream endpoint (deterministic).

Scripted LLM + stub servers + one stub tool with a fixed delay guarantee at
least one mid-run snapshot before the terminal event, so status progression
is asserted deterministically. Framing, headers, terminal completion, unknown
IDs, already-terminal investigations, and disconnect cleanup are covered.
The keepalive interval is a timing constant and is NOT timing-tested here.
"""

import asyncio
import json

import httpx
import pytest

from backend.agent.llm.adapter import LLMToolCall
from backend.api import routes as api_routes
from backend.agent.models import (
    Claim,
    Confidence,
    InvestigationReport,
    Plan,
    PlanStep,
    Understanding,
)
from backend.agent.service import AgentService
from backend.config import Settings
from backend.main import create_app
from tests.integration.test_agent_loop import StubMulti, StubServerManager
from tests.scripted import ScriptedLLM


class DelayedStubServerManager(StubServerManager):
    """Adds a fixed per-call delay so the investigation spends observable time
    in the running state (deterministic — no jitter, no wall-clock guessing)."""

    def __init__(self, *args, delay_seconds: float = 0.0, **kwargs):
        super().__init__(*args, **kwargs)
        self._delay = delay_seconds

    async def call_tool(self, name, arguments):
        if self._delay:
            await asyncio.sleep(self._delay)
        return await super().call_tool(name, arguments)


def _scripted(selections, report=None):
    return ScriptedLLM(
        understanding=Understanding(goal="explain the drop", time_frame="2026-10-05",
                                    focus_areas=["checkout"],
                                    success_criteria="grounded answer"),
        plan=Plan(goal="investigate",
                  steps=[PlanStep(id=1, action="placeholder step", evidence_needed="e")],
                  expected_evidence=["e"], completion_criteria="done"),
        selections=list(selections),
        report=report
        or InvestigationReport(
            conclusion="Revenue fell during the incident window across channels.",
            claims=[Claim(text="Revenue fell during the incident.",
                          evidence_ids=["ev-0001"])],
            confidence=Confidence.medium,
            confidence_rationale="grounded"),
    )


def _service(selections, *, delay_seconds: float = 0.0) -> AgentService:
    settings = Settings(max_investigation_seconds=60, max_llm_calls=16)
    managers = StubMulti({
        "postgres-mcp": DelayedStubServerManager(
            ["get_daily_sales"], delay_seconds=delay_seconds),
        "monitoring-mcp": DelayedStubServerManager(
            ["get_error_rate"], delay_seconds=delay_seconds),
    })
    return AgentService(settings, llm=_scripted(selections),
                        manager_factory=lambda: managers)


def _client(app):
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


async def test_sse_framing_progression_and_terminal():
    app = create_app(service=_service([
        LLMToolCall(id="c1", name="postgres-mcp__get_daily_sales",
                    arguments={"start_date": "2026-10-05", "end_date": "2026-10-05"}),
        LLMToolCall(id="c2", name="monitoring-mcp__get_error_rate",
                    arguments={"service": "checkout", "window_start": "2026-10-03",
                               "window_end": "2026-10-05"}),
    ], delay_seconds=0.3))
    async with app.router.lifespan_context(app):
        async with _client(app) as http:
            created = await http.post(
                "/api/investigations", json={"question": "Why did today's sales drop?"})
        assert created.status_code == 201
        iid = created.json()["investigation_id"]

        frames: list[tuple[str, dict | None]] = []
        async with _client(app) as http:
            monkey_patch = pytest.MonkeyPatch()
            monkey_patch.setattr(api_routes, "SSE_POLL_SECONDS", 0.05)
            try:
                async with http.stream("GET", f"/api/investigations/{iid}/events") as resp:
                    assert resp.status_code == 200
                    assert resp.headers["content-type"].startswith("text/event-stream")
                    assert resp.headers["cache-control"] == "no-cache"
                    assert resp.headers["x-accel-buffering"] == "no"
                    event_name = None
                    async for line in resp.aiter_lines():
                        if line.startswith("event: "):
                            event_name = line[len("event: "):]
                        elif line.startswith("data: ") and event_name:
                            frames.append((event_name, json.loads(line[len("data: "):])))
                            if event_name == "terminal":
                                break
            finally:
                monkey_patch.undo()

    statuses = [f["status"] for _, f in frames if f]
    assert frames[-1][0] == "terminal"
    assert frames[-1][1]["status"] == "complete"
    assert frames[-1][1]["investigation_id"] == iid
    # deterministic progression: at least one mid-run snapshot, then terminal
    assert "running" in statuses
    assert statuses[-1] == "complete"
    # terminal is the last frame and every frame is valid SSE with JSON data
    assert all(f is not None for _, f in frames)


async def test_sse_unknown_investigation_is_404():
    app = create_app(service=_service([]))
    async with app.router.lifespan_context(app):
        async with _client(app) as http:
            response = await http.get("/api/investigations/inv-does-not-exist/events")
    assert response.status_code == 404
    assert "unknown investigation" in response.json()["detail"]


async def test_sse_terminal_investigation_emits_snapshot_then_closes():
    app = create_app(service=_service([]))
    async with app.router.lifespan_context(app):
        async with _client(app) as http:
            created = await http.post(
                "/api/investigations", json={"question": "Why did today's sales drop?"})
            iid = created.json()["investigation_id"]
            import asyncio

            async def _wait_terminal():
                while True:
                    record = app.state.service.registry.get(iid)
                    if record is not None and record.status != "running":
                        return
                    await asyncio.sleep(0.02)

            await asyncio.wait_for(_wait_terminal(), timeout=10)
            body = (await http.get(f"/api/investigations/{iid}/events")).content
    text = body.decode()
    assert "event: status" in text and "event: terminal" in text
    # static stream: exactly one status frame then one terminal frame
    assert text.count("event: status") == 1
    assert text.count("event: terminal") == 1
    assert text.find("event: terminal") > text.find("event: status")


async def test_sse_client_disconnect_cleans_up_and_app_stays_healthy():
    app = create_app(service=_service([
        LLMToolCall(id="c1", name="postgres-mcp__get_daily_sales",
                    arguments={"start_date": "2026-10-05", "end_date": "2026-10-05"}),
    ], delay_seconds=1.5))
    async with app.router.lifespan_context(app):
        async with _client(app) as http:
            created = await http.post(
                "/api/investigations", json={"question": "Why did today's sales drop?"})
            iid = created.json()["investigation_id"]
            # open the stream, read the first frame, then disconnect mid-run
            async with http.stream("GET", f"/api/investigations/{iid}/events") as resp:
                async for _line in resp.aiter_lines():
                    break  # disconnect immediately after the first frame

        # the investigation itself is untouched by the listener disconnect:
        # it still runs to completion on its own lifecycle
        import asyncio

        async def _wait_complete():
            while True:
                record = app.state.service.registry.get(iid)
                if record is not None and record.status == "complete":
                    return
                await asyncio.sleep(0.05)

        await asyncio.wait_for(_wait_complete(), timeout=30)
        async with _client(app) as http:
            assert (await http.get("/health")).status_code == 200
            final = await http.get(f"/api/investigations/{iid}")
            assert final.json()["status"] == "complete"


def test_sse_snapshot_carries_no_sensitive_payloads():
    from backend.agent.state import InvestigationRecord

    record = InvestigationRecord(
        investigation_id="inv-x", request_id="req-x", question="secret question?")
    snapshot = api_routes._sse_snapshot(record)
    assert set(snapshot) == {"investigation_id", "status", "stage", "evidence_count"}
    assert "secret question?" not in json.dumps(snapshot)
