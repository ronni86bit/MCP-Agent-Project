"""Phase 7 Gate 4: approval API endpoints over the real monitoring-mcp
subprocess and the state store.

The investigation runs through the scripted-LLM loop (proposal persisted by
the propose_action phase); decision/execute flow through the ApprovalService.
No live GitHub, no LLM. Tests are independent: each finds its own proposal
by ID rather than asserting on global listing counts.
"""

import asyncio

import httpx
import pytest

from backend.agent.llm.adapter import LLMToolCall
from backend.agent.service import AgentService
from backend.config import Settings
from backend.main import create_app
from backend.mcp.client_manager import MultiMCPManager


def _monitoring_call() -> LLMToolCall:
    return LLMToolCall(
        id="c2",
        name="monitoring-mcp__get_recent_alerts",
        arguments={"window_start": "2026-10-03", "window_end": "2026-10-05"},
    )


def _scripted_api_llm():
    from tests.scripted import ScriptedLLM
    from backend.agent.models import (
        ActionProposal, Understanding, Plan, PlanStep,
        InvestigationReport, Claim, Confidence,
    )

    proposal = ActionProposal(
        tool="acknowledge_alert",
        arguments={"alert_id": "ALT-2071", "note": "acknowledged during incident response"},
        rationale="Unacknowledged high-severity checkout alert observed in evidence.",
        evidence_ids=["ev-0002"],
    )
    return ScriptedLLM(
        understanding=Understanding(
            goal="explain the drop", time_frame="2026-10-05",
            focus_areas=["checkout"], success_criteria="grounded answer",
        ),
        plan=Plan(
            goal="investigate the sales drop",
            steps=[
                PlanStep(id=1, action="establish daily sales", evidence_needed="daily totals"),
                PlanStep(id=2, action="check alert state", evidence_needed="alerts"),
            ],
            expected_evidence=["totals", "alerts"],
            completion_criteria="evidence supports a conclusion",
        ),
        selections=[
            LLMToolCall(id="c1", name="postgres-mcp__get_daily_sales",
                        arguments={"start_date": "2026-10-03", "end_date": "2026-10-05"}),
            LLMToolCall(id="c2", name="monitoring-mcp__get_recent_alerts",
                        arguments={"window_start": "2026-10-03", "window_end": "2026-10-05"}),
        ],
        report=InvestigationReport(
            conclusion="Revenue fell while an alert was open, consistent with a disruption.",
            claims=[Claim(text="An alert was open during the decline.",
                          evidence_ids=["ev-0002"])],
            confidence=Confidence.low,
            confidence_rationale="limited evidence",
        ),
        proposal=proposal,
    )


def _service(pg_manager, monitoring_manager) -> AgentService:
    settings = Settings(
        max_investigation_seconds=90,
        max_llm_calls=16,
        action_mode_enabled=True,
        approval_ttl_seconds=3600,
    )

    def factory() -> MultiMCPManager:
        return MultiMCPManager({
            "postgres-mcp": lambda: pg_manager(),
            "monitoring-mcp": lambda: monitoring_manager(),
        })

    return AgentService(settings, llm=_scripted_api_llm(), manager_factory=factory)


@pytest.fixture
async def client(pg_manager, monitoring_manager):
    app = create_app(service=_service(pg_manager, monitoring_manager))
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
            yield http


async def _run_investigation(client: httpx.AsyncClient) -> tuple[str, str]:
    """Run a scripted investigation; returns (investigation_id, proposal_id)."""
    created = await client.post(
        "/api/investigations", json={"question": "Why did today's sales drop?"}
    )
    assert created.status_code == 201
    investigation_id = created.json()["investigation_id"]
    deadline = asyncio.get_event_loop().time() + 60
    payload = None
    while asyncio.get_event_loop().time() < deadline:
        await asyncio.sleep(0.1)
        payload = (await client.get(f"/api/investigations/{investigation_id}")).json()
        if payload["status"] != "running":
            break
    assert payload is not None and payload["status"] == "complete"
    assert payload["proposals"], "the scripted investigation must produce a proposal"
    return investigation_id, payload["proposals"][0]["approval_id"]


async def test_list_approvals_and_filter_by_status(client):
    investigation_id, proposal_id = await _run_investigation(client)

    listed = await client.get("/api/approvals")
    assert listed.status_code == 200
    ours = [a for a in listed.json()["approvals"] if a["approval_id"] == proposal_id]
    assert len(ours) == 1, f"proposal {proposal_id} must appear in the listing"
    assert ours[0]["tool"] == "acknowledge_alert"

    proposed = await client.get("/api/approvals", params={"status": "proposed"})
    assert any(a["approval_id"] == proposal_id for a in proposed.json()["approvals"])

    invalid = await client.get("/api/approvals", params={"status": "bogus"})
    assert invalid.status_code == 422


async def test_get_approval_and_404(client):
    _, proposal_id = await _run_investigation(client)
    one = await client.get(f"/api/approvals/{proposal_id}")
    assert one.status_code == 200
    assert one.json()["tool"] == "acknowledge_alert"
    assert one.json()["verification"] is None, "not executed yet: no verification"

    missing = await client.get("/api/approvals/apr-nope")
    assert missing.status_code == 404


async def test_approve_then_execute_happy_path(client):
    _, approval_id = await _run_investigation(client)

    decision = await client.post(
        f"/api/approvals/{approval_id}/decision",
        json={"decision": "approved", "decided_by": "operator-1", "comment": "evidence is clear"},
    )
    assert decision.status_code == 200
    assert decision.json()["status"] == "approved"

    executed = await client.post(
        f"/api/approvals/{approval_id}/execute", json={"executed_by": "op-2"}
    )
    assert executed.status_code == 200
    body = executed.json()
    assert body["status"] == "executed"
    assert body["executed"] is True
    assert body["verification"] is not None and body["verification"]["ok"] is True, \
        "read-back must confirm the acknowledgement"


async def test_rejected_proposal_cannot_be_executed(client):
    _, approval_id = await _run_investigation(client)

    rejected = await client.post(
        f"/api/approvals/{approval_id}/decision",
        json={"decision": "rejected", "decided_by": "op-1", "comment": "insufficient evidence"},
    )
    assert rejected.status_code == 200

    executed = await client.post(
        f"/api/approvals/{approval_id}/execute", json={"executed_by": "op-2"}
    )
    assert executed.status_code == 409
    assert "not in 'approved'" in executed.json()["detail"]


async def test_second_decision_after_approval_is_409(client):
    _, approval_id = await _run_investigation(client)

    first = await client.post(
        f"/api/approvals/{approval_id}/decision",
        json={"decision": "approved", "decided_by": "op-1"},
    )
    assert first.status_code == 200
    second = await client.post(
        f"/api/approvals/{approval_id}/decision",
        json={"decision": "rejected", "decided_by": "op-2"},
    )
    assert second.status_code == 409


async def test_missing_or_blank_operator_labels_are_rejected(client):
    _, approval_id = await _run_investigation(client)

    for body in (
        {"decision": "approved"},
        {"decision": "approved", "decided_by": "   "},
        {"decision": "approved", "decided_by": "op", "note": "extra"},
    ):
        response = await client.post(f"/api/approvals/{approval_id}/decision", json=body)
        assert response.status_code == 422, body

    for body in (
        {},
        {"executed_by": " "},
        {"executed_by": "op", "alert_id": "ALT-2071"},
    ):
        response = await client.post(f"/api/approvals/{approval_id}/execute", json=body)
        assert response.status_code == 422, body


async def test_execute_unknown_approval_is_404(client):
    response = await client.post(
        "/api/approvals/apr-nope/execute", json={"executed_by": "op"}
    )
    assert response.status_code == 404
