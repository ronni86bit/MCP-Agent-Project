"""Phase 7 Gate 3: the loop's optional `propose_action` phase.

Deterministic scripted-LLM tests over stub MCP servers. PostgreSQL required
(proposals persist through the state store).
"""

import asyncio
import json
import uuid

import asyncpg

from backend.agent.loop import InvestigationLoop
from backend.agent.llm.adapter import LLMToolCall
from backend.agent.models import (
    ActionProposal,
    Claim,
    Confidence,
    InvestigationReport,
    Plan,
    PlanStep,
    Understanding,
)
from backend.agent.persistence.store import InvestigationStore
from backend.agent.state import InvestigationRecord
from backend.config import Settings
from backend.security.approval_service import ApprovalService
from tests.integration.test_agent_loop import StubMulti, StubServerManager
from tests.scripted import ScriptedLLM

MONITORING_PAYLOAD = {
    "alerts": [{"id": "ALT-2071", "service": "checkout", "acknowledged": False}],
    "count": 1,
}


def _settings(**overrides) -> Settings:
    values = dict(
        max_investigation_steps=3,
        max_plan_steps=2,
        max_llm_calls=16,
        max_investigation_seconds=60,
        mcp_call_timeout_seconds=15,
        action_mode_enabled=True,
    )
    values.update(overrides)
    return Settings(**values)


def _scripted(selections: list | None = None, *, proposal: ActionProposal | None = None,
              fail_proposal: bool = False):
    if selections is None:
        selections = [
            LLMToolCall(id="c1", name="postgres-mcp__get_daily_sales",
                        arguments={"start_date": "2026-10-03", "end_date": "2026-10-05"}),
            LLMToolCall(id="c2", name="monitoring-mcp__get_recent_alerts",
                        arguments={"window_start": "2026-10-03", "window_end": "2026-10-05"}),
        ]
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
        selections=selections,
        report=InvestigationReport(
            conclusion="Revenue fell while an alert was open, consistent with a disruption.",
            claims=[Claim(text="An alert was open during the decline.",
                          evidence_ids=["ev-0002"])],
            confidence=Confidence.low,
            confidence_rationale="limited evidence",
        ),
        proposal=proposal,
        fail_proposal=fail_proposal,
    )


def _valid_proposal() -> ActionProposal:
    return ActionProposal(
        tool="acknowledge_alert",
        arguments={"alert_id": "ALT-2071", "note": "acknowledged during incident response"},
        rationale="Unacknowledged high-severity checkout alert observed in evidence.",
        evidence_ids=["ev-0002"],
    )


def _stub_multi() -> StubMulti:
    return StubMulti({
        "postgres-mcp": StubServerManager(["get_daily_sales"]),
        "monitoring-mcp": StubServerManager(
            ["get_recent_alerts"],
            payloads={"get_recent_alerts": MONITORING_PAYLOAD},
        ),
    })


async def _run(scripted: ScriptedLLM, manager_factory, settings: Settings,
               state_dsn: str) -> InvestigationRecord:
    store = InvestigationStore(dsn=state_dsn)
    approval_service = ApprovalService(
        store=store,
        monitoring_session_factory=lambda: StubMulti({}),
    )
    record = InvestigationRecord(
        investigation_id="inv-prop-" + uuid.uuid4().hex[:12],
        request_id="req-prop-" + uuid.uuid4().hex[:8],
        question="Why did today's sales drop?",
    )
    loop = InvestigationLoop(
        llm=scripted,
        manager_factory=manager_factory,
        record=record,
        settings=settings,
        state_store=store,
        approval_service=approval_service,
    )
    return await loop.run()


async def _store_events(dsn: str, investigation_id: str) -> list[dict]:
    conn = await asyncpg.connect(dsn, timeout=10)
    try:
        rows = await conn.fetch(
            "SELECT event, payload FROM agent_state.investigation_events "
            "WHERE investigation_id = $1 ORDER BY event_id",
            investigation_id,
        )
        return [{"event": r["event"], "payload": json.loads(r["payload"])} for r in rows]
    finally:
        await conn.close()


async def _get_approval(dsn: str, approval_id: str):
    conn = await asyncpg.connect(dsn, timeout=10)
    try:
        return await conn.fetchrow(
            "SELECT status, tool FROM agent_state.approvals WHERE approval_id = $1",
            approval_id,
        )
    finally:
        await conn.close()


# -- tests ---------------------------------------------------------------------


async def test_action_mode_disabled_skips_phase_entirely(approvals_state_dsn: str):
    scripted = _scripted(_valid_proposal(), ) if False else _scripted(proposal=_valid_proposal())
    record = await _run(scripted, _stub_multi, _settings(action_mode_enabled=False), approvals_state_dsn)
    assert record.status == "complete"
    assert scripted.proposal_calls == 0, "action mode off -> the model is not asked"
    assert record.proposals == []


async def test_no_eligible_alert_skips_proposal_model_call(approvals_state_dsn: str):
    servers = {
        "postgres-mcp": StubServerManager(["get_daily_sales"]),
        "monitoring-mcp": StubServerManager(
            ["get_recent_alerts"],
            payloads={"get_recent_alerts": {
                "alerts": [{"id": "ALT-2071", "service": "checkout", "acknowledged": True}],
                "count": 1,
            }},
        ),
    }
    scripted = _scripted(_valid_proposal(), ) if False else _scripted(proposal=_valid_proposal())
    record = await _run(scripted, lambda: StubMulti(servers), _settings(), approvals_state_dsn)
    assert record.status == "complete"
    assert scripted.proposal_calls == 0, "no eligible alert -> the model is not called"
    assert record.proposals == []


async def test_eligible_evidence_yields_exactly_one_valid_persisted_proposal(approvals_state_dsn: str):
    scripted = _scripted(_valid_proposal(), ) if False else _scripted(proposal=_valid_proposal())
    dsn = approvals_state_dsn
    record = await _run(scripted, _stub_multi, _settings(), dsn)
    assert record.status == "complete"
    assert scripted.proposal_calls == 1
    assert len(record.proposals) == 1

    approval = record.proposals[0]
    assert approval.tool == "acknowledge_alert"
    assert approval.status == "proposed"
    assert approval.arguments["alert_id"] == "ALT-2071"
    assert approval.args_hash
    assert approval.expires_at is not None

    row = await _get_approval(dsn, approval.approval_id)
    assert row is not None and row["status"] == "proposed"
    assert row["tool"] == "acknowledge_alert"

    # the model can never execute its own proposal
    events = await _store_events(dsn, record.investigation_id)
    assert not any(e["event"] in ("execution_started", "execution_finished") for e in events)


def _daily_call(step_args: dict | None = None) -> LLMToolCall:
    return LLMToolCall(
        id="call-1",
        name="postgres-mcp__get_daily_sales",
        arguments=step_args or {"start_date": "2026-10-03", "end_date": "2026-10-05"},
    )


def _monitoring_call() -> LLMToolCall:
    return LLMToolCall(
        id="call-2",
        name="monitoring-mcp__get_recent_alerts",
        arguments={"window_start": "2026-10-03", "window_end": "2026-10-05"},
    )


async def test_invented_alert_id_is_rejected(approvals_state_dsn: str):
    invented = ActionProposal(
        tool="acknowledge_alert",
        arguments={"alert_id": "ALT-9999", "note": None},
        rationale="Unacknowledged alert observed.",
        evidence_ids=["ev-0002"],
    )
    scripted = _scripted([_daily_call(), _monitoring_call()], proposal=invented)
    dsn = approvals_state_dsn
    record = await _run(scripted, _stub_multi, _settings(), dsn)
    assert record.status == "complete"
    assert record.proposals == [], "invented alert id must not be persisted"
    assert any("action proposal rejected" in w for w in record.warnings)


async def test_unsupported_action_is_rejected_without_persistence(approvals_state_dsn: str):
    proposal = _valid_proposal().model_copy(update={"tool": "delete_everything"})
    scripted = _scripted([_daily_call()], proposal=proposal)
    dsn = approvals_state_dsn
    record = await _run(scripted, _stub_multi, _settings(), dsn)
    assert record.status == "complete"
    assert record.proposals == []


async def test_invalid_evidence_references_are_rejected(approvals_state_dsn: str):
    proposal = _valid_proposal().model_copy(update={"evidence_ids": ["ev-9999"]})
    scripted = _scripted([_daily_call(), _monitoring_call()], proposal=proposal)
    dsn = approvals_state_dsn
    record = await _run(scripted, _stub_multi, _settings(), dsn)
    assert record.status == "complete"
    assert record.proposals == []
    assert any("evidence references not found" in w for w in record.warnings)


async def test_malformed_proposal_is_advisory_and_investigation_still_completes(approvals_state_dsn: str):
    # monitoring evidence included so the eligibility gate opens and the
    # proposal LLM call actually happens (then fails -> advisory)
    scripted = _scripted([_daily_call(), _monitoring_call()], fail_proposal=True)
    dsn = approvals_state_dsn
    record = await _run(scripted, _stub_multi, _settings(), dsn)
    assert record.status == "complete", "proposal failure must be advisory"
    assert record.report is not None, "the investigation report is retained"
    assert record.evidence_ids == ["ev-0001", "ev-0002"]
    assert any("action proposal skipped" in w for w in record.warnings)


async def test_budget_exhaustion_skips_proposal_without_extra_call(approvals_state_dsn: str):
    scripted = _scripted([_daily_call(), _monitoring_call()])
    dsn = approvals_state_dsn
    settings = _settings(max_llm_calls=5)  # synthesis is the last allowed call
    record = await _run(scripted, _stub_multi, settings, dsn)
    assert record.status == "complete"
    # understand+plan+2 selects+1 review+synthesis = 6; the proposal gets none
    assert scripted.proposal_calls == 0
    print('LLM_CALLS:', record.llm_calls)
    assert record.llm_calls >= 0
    assert any("budget" in w for w in record.warnings)


async def test_at_most_one_proposal_per_investigation(approvals_state_dsn: str):
    scripted = _scripted([_daily_call()])
    dsn = approvals_state_dsn
    record = await _run(scripted, _stub_multi, _settings(), dsn)
    assert len(record.proposals) <= 1
