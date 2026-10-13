"""Phase 3 API tests: health, investigation create/get/poll, validation.

Uses the scripted LLM and the real postgres-mcp subprocess, so the full
POST -> loop -> MCP -> evidence -> report path runs without Ollama.
"""

import asyncio
import json

import httpx
import pytest

from backend.agent.llm.adapter import LLMToolCall
from backend.agent.service import AgentService
from backend.config import Settings
from backend.main import create_app
from backend.mcp.client_manager import MultiMCPManager
from tests.integration.test_agent_loop import _daily_call, _scripted


def _service(pg_manager) -> AgentService:
    settings = Settings(max_investigation_seconds=90, max_llm_calls=16)
    factory = lambda: MultiMCPManager({"postgres-mcp": lambda: pg_manager()})  # noqa: E731
    return AgentService(settings, llm=_scripted([_daily_call()]), manager_factory=factory)


@pytest.fixture
async def client(pg_manager):
    app = create_app(service=_service(pg_manager))
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
            yield http


async def test_health_reports_component_status(client):
    response = await client.get("/api/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["ollama"] == "up"  # scripted adapter always checks out
    assert body["mcp_servers"]["mcp"].startswith("up")
    assert body["model"]


async def test_investigation_create_poll_get_grounds_report(client):
    created = await client.post("/api/investigations", json={"question": "Why did today's sales drop?"})
    assert created.status_code == 201
    body = created.json()
    assert body["status"] == "running"
    investigation_id = body["investigation_id"]

    deadline = asyncio.get_event_loop().time() + 60
    payload = None
    while asyncio.get_event_loop().time() < deadline:
        await asyncio.sleep(0.1)
        response = await client.get(f"/api/investigations/{investigation_id}")
        assert response.status_code == 200
        payload = response.json()
        if payload["status"] != "running":
            break
    assert payload is not None and payload["status"] == "complete"
    assert payload["evidence_ids"]
    assert payload["report"]["conclusion"]
    for claim in payload["report"]["claims"]:
        assert claim["evidence_ids"]
    assert payload["plan"]["steps"]
    assert payload["tool_calls"][0]["tool"] == "postgres-mcp__get_daily_sales"


async def test_invalid_question_is_rejected(client):
    response = await client.post("/api/investigations", json={"question": "hi"})
    assert response.status_code == 422


async def test_unknown_investigation_is_404(client):
    response = await client.get("/api/investigations/inv-does-not-exist")
    assert response.status_code == 404
