"""Phase 9 Gate 1: deterministic evaluation cases over the REAL loop.

Every case runs `InvestigationLoop` with injected deterministic doubles
(ScriptedLLM + stub/failing managers, or the real postgres-mcp subprocess for
the D-SEL boundary). Model-dependent evaluation is NOT introduced here.
PostgreSQL-dependent cases (D-SEL, D-APPROVE) skip with the shared fixtures
when the database is unreachable, like the rest of the integration suite.
"""

import asyncio
import time

import pytest

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
from backend.config import Settings
from backend.evaluation import cases_deterministic as cases
from backend.mcp.client_manager import MultiMCPManager
from backend.reliability import CircuitBreaker, CircuitState
from tests.integration.test_agent_loop import StubMulti, StubServerManager
from tests.scripted import ScriptedLLM
from tests.unit.test_approval_lifecycle import (
    ALERT,
    FakeSession,
    FakeSessionCM,
    FixedClock,
    _now,
)


def _settings(**overrides) -> Settings:
    values = dict(
        max_investigation_steps=3,
        max_plan_steps=2,
        max_llm_calls=16,
        max_investigation_seconds=60,
        mcp_call_timeout_seconds=15,
    )
    values.update(overrides)
    return Settings(**values)


def _scripted(selections: list, report: InvestigationReport | None = None,
              proposal: ActionProposal | None = None) -> ScriptedLLM:
    return ScriptedLLM(
        understanding=Understanding(
            goal="explain the drop", time_frame="2026-10-05",
            focus_areas=["checkout"], success_criteria="grounded answer",
        ),
        plan=Plan(
            goal="investigate the sales drop",
            steps=[PlanStep(id=i + 1, action=f"step {i + 1}", evidence_needed="evidence")
                   for i in range(max(len(selections), 1))],
            expected_evidence=["evidence"],
            completion_criteria="evidence supports a conclusion",
        ),
        selections=selections,
        report=report or InvestigationReport(
            conclusion="Revenue fell during the incident window, consistent with the evidence.",
            claims=[Claim(text="Revenue fell during the incident.", evidence_ids=["ev-0001"])],
            confidence=Confidence.medium,
            confidence_rationale="grounded",
        ),
        proposal=proposal,
    )


def _daily_call(**kwargs) -> LLMToolCall:
    return LLMToolCall(
        id="c-daily", name="postgres-mcp__get_daily_sales",
        arguments={"start_date": "2026-10-05", "end_date": "2026-10-05", **kwargs},
    )


class HangingFactory:
    """Never becomes ready: for wall-clock budget and cancellation cases."""

    def __init__(self, hold_seconds: float = 30.0):
        self._hold = hold_seconds

    async def __aenter__(self):
        await asyncio.sleep(self._hold)
        return self

    async def __aexit__(self, *exc):
        return False


# -- D-SEL: the verified server-side boundary (real postgres-mcp) -----------------


async def test_dsel_unequal_windows_rejected_server_side(pg_manager):
    unequal = LLMToolCall(
        id="c-unequal", name="postgres-mcp__compare_sales",
        arguments={
            "window_a_start": "2026-10-03", "window_a_end": "2026-10-05",
            "window_b_start": "2026-10-04", "window_b_end": "2026-10-05",  # 3 vs 2 days
        },
    )
    equal = LLMToolCall(
        id="c-equal", name="postgres-mcp__compare_sales",
        arguments={
            "window_a_start": "2026-10-03", "window_a_end": "2026-10-05",
            "window_b_start": "2026-09-30", "window_b_end": "2026-10-02",  # 3 vs 3 days
        },
    )
    managers = MultiMCPManager({"postgres-mcp": lambda: pg_manager()})
    async with managers:
        case = await cases.case_selection_arguments(
            llm=_scripted([unequal, equal]), managers=managers, settings=_settings(),
        )

    names = {c.name: c for c in case.checks}
    # boundary findings, pinned by this run:
    #  - the agent-side jsonschema gate PASSES the invalid arguments (a pydantic
    #    mode="after" model validator cannot appear in the tool's JSON Schema)
    assert names["schema_gate_passes_invalid_windows"].passed is True
    #  - the rejection happens server-side, before any query, and arrives typed
    assert names["typed_error_preserved"].passed is True
    assert names["server_side_rule_named"].passed is True
    assert "equal-duration" in names["server_side_rule_named"].detail
    #  - the failed call produced no evidence; the equal-window control did
    assert names["no_evidence_from_failed_call"].passed is True
    assert names["equal_duration_control_executes"].passed is True
    assert case.passed is True


# -- stub-based deterministic cases (no PostgreSQL) --------------------------------


async def test_dev_covers_expected_servers():
    selections = [_daily_call(), LLMToolCall(
        id="c-err", name="monitoring-mcp__get_error_rate",
        arguments={"service": "checkout", "window_start": "2026-10-03",
                   "window_end": "2026-10-05"},
    )]
    managers = StubMulti({
        "postgres-mcp": StubServerManager(["get_daily_sales"]),
        "monitoring-mcp": StubServerManager(["get_error_rate"]),
    })
    case = await cases.case_evidence_coverage(
        llm=_scripted(selections), manager_factory=lambda: managers,
        settings=_settings(), expected_servers=frozenset({"postgres-mcp", "monitoring-mcp"}),
    )
    assert case.passed, [c.model_dump() for c in case.checks if not c.passed]


async def test_dground_flags_fabrication_and_causal_language():
    report = InvestigationReport(
        conclusion="Checkout revenue fell exactly 31% during the incident window.",
        claims=[Claim(text="Revenue fell exactly 31%, which proves the deployment caused it.",
                      evidence_ids=["ev-0001"])],
        confidence=Confidence.high,
        confidence_rationale="single strong signal",
    )
    managers = StubMulti({"postgres-mcp": StubServerManager(["get_daily_sales"])})
    case = await cases.case_grounding(
        llm=_scripted([_daily_call()], report=report),
        manager_factory=lambda: managers, settings=_settings(),
        expect_ok=False, expect_flagged=True, expect_downgraded=True,
    )
    assert case.passed, [c.model_dump() for c in case.checks if not c.passed]


async def test_dgap_no_evidence_fails_explicitly():
    managers = StubMulti({"postgres-mcp": StubServerManager(["get_daily_sales"])})
    case = await cases.case_insufficient_evidence(
        llm=_scripted([]), manager_factory=lambda: managers, settings=_settings(),
    )
    assert case.passed, [c.model_dump() for c in case.checks if not c.passed]
    assert case.observed["status"] == "failed"
    assert "no evidence" in (case.observed["failure_reason"] or "")


async def test_dfail_typed_failure_then_recovery():
    managers = StubMulti({"postgres-mcp": StubServerManager(
        ["get_daily_sales", "get_sales_trend"], fail_names={"get_daily_sales"},
    )})
    trend = LLMToolCall(
        id="c-trend", name="postgres-mcp__get_sales_trend",
        arguments={"start_date": "2026-09-28", "end_date": "2026-10-05", "granularity": "day"},
    )
    case = await cases.case_tool_failure_recovery(
        llm=_scripted([_daily_call(), trend]),
        manager_factory=lambda: managers, settings=_settings(),
        failing_tool="postgres-mcp__get_daily_sales",
    )
    assert case.passed, [c.model_dump() for c in case.checks if not c.passed]


async def test_dcircuit_source_unavailability_is_reported():
    breaker = CircuitBreaker("monitoring-mcp", failure_threshold=3, cooldown_seconds=60.0)
    breaker.state = CircuitState.open
    breaker.opened_at = time.monotonic()
    managers = StubMulti({
        "postgres-mcp": StubServerManager(["get_daily_sales"]),
        "monitoring-mcp": StubServerManager(["get_error_rate"]),
    })
    managers.breakers = {"monitoring-mcp": breaker}
    err = LLMToolCall(
        id="c-err", name="monitoring-mcp__get_error_rate",
        arguments={"service": "checkout", "window_start": "2026-10-03",
                   "window_end": "2026-10-05"},
    )
    case = await cases.case_circuit_open(
        llm=_scripted([_daily_call(), err]),
        manager_factory=lambda: managers, settings=_settings(),
        unavailable_server="monitoring-mcp",
    )
    assert case.passed, [c.model_dump() for c in case.checks if not c.passed]


@pytest.mark.parametrize("budget_class", ["llm_calls", "steps", "wall_clock"])
async def test_dbudget_terminates_within_budget_and_records_exhaustion(budget_class):
    if budget_class == "llm_calls":
        settings = _settings(max_llm_calls=2)  # understand + plan exhaust it
        factory = lambda: StubMulti({"postgres-mcp": StubServerManager(["get_daily_sales"])})  # noqa: E731
        llm = _scripted([_daily_call()])
    elif budget_class == "steps":
        settings = _settings(max_investigation_steps=1, max_llm_calls=16)
        factory = lambda: StubMulti({"postgres-mcp": StubServerManager(["get_daily_sales"])})  # noqa: E731
        llm = _scripted([_daily_call()])
    else:  # wall_clock
        settings = _settings(max_investigation_seconds=0.2)
        factory = HangingFactory
        llm = _scripted([])

    case = await cases.case_budget_termination(
        llm=llm, manager_factory=factory, settings=settings, budget_class=budget_class,
    )
    assert case.passed, [c.model_dump() for c in case.checks if not c.passed]
    assert case.observed["budget_metric_delta"][
        "{'budget_class': '%s'}" % budget_class] >= 1


async def test_dcancel_cancellation_propagates_and_never_finalizes():
    case = await cases.case_cancellation(
        llm=_scripted([]), manager_factory=HangingFactory, settings=_settings(),
    )
    assert case.passed, [c.model_dump() for c in case.checks if not c.passed]


# -- D-APPROVE: full lifecycle over the existing deterministic harness (Postgres) --


async def test_dapprove_lifecycle_with_human_gate(state_test_store):
    from backend.security.approval_service import ApprovalService

    store = state_test_store()
    clock = FixedClock(_now())
    session = FakeSession({
        "get_recent_alerts": [
            {"alerts": [dict(ALERT)], "count": 1},  # pre-check: unacknowledged
            {"alerts": [dict(ALERT, acknowledged=True, acknowledged_by="op-1",
                              acknowledged_at="2026-10-05T12:00:01Z")], "count": 1},
        ]
    })
    service = ApprovalService(
        store, lambda: FakeSessionCM({"current": session}),
        clock=clock, execution_timeout_seconds=5.0,
    )

    alerts_payload = {"alerts": [dict(ALERT)], "count": 1}
    managers = StubMulti({
        "postgres-mcp": StubServerManager(["get_daily_sales"]),
        "monitoring-mcp": StubServerManager(
            ["get_recent_alerts"], payloads={"get_recent_alerts": alerts_payload},
        ),
    })
    report = InvestigationReport(
        conclusion="Revenue fell while checkout alert ALT-2071 was open and unacknowledged.",
        claims=[
            Claim(text="Revenue fell during the incident.", evidence_ids=["ev-0001"]),
            Claim(text="Alert ALT-2071 was open.", evidence_ids=["ev-0002"]),
        ],
        confidence=Confidence.medium,
        confidence_rationale="two sources agree",
    )
    proposal = ActionProposal(
        tool="acknowledge_alert",
        arguments={"alert_id": "ALT-2071", "note": "acknowledged during incident response"},
        rationale="unacknowledged high-severity checkout alert in evidence",
        evidence_ids=["ev-0002"],
    )

    # the loop may only PROPOSE; a human decision + execution follow through
    # the deterministic fake session (no real write path exists in this harness)
    from backend.agent.loop import InvestigationLoop
    from backend.agent.state import InvestigationRecord

    record = InvestigationRecord(
        investigation_id=f"inv-eval-{time.monotonic_ns() % 10**10:010d}",
        request_id=f"req-eval-{time.monotonic_ns() % 10**10:010d}",
        question="Why did today's sales drop?",
    )
    loop = InvestigationLoop(
        llm=_scripted([_daily_call(), LLMToolCall(
            id="c-alerts", name="monitoring-mcp__get_recent_alerts",
            arguments={"window_start": "2026-10-03", "window_end": "2026-10-05"},
        )], report=report, proposal=proposal),
        manager_factory=lambda: managers,
        record=record,
        settings=_settings(action_mode_enabled=True),
        state_store=store,
        approval_service=service,
    )
    await loop.run()

    assert record.proposals, "expected the scripted proposal to be persisted"
    persisted = record.proposals[0]
    await service.decide(persisted.approval_id, "approved", "op-1")
    executed = await service.execute(persisted.approval_id, "op-2")
    final = await store.get_approval(persisted.approval_id)

    case = await cases.case_proposal_lifecycle(
        proposal_row=persisted, executed_row=final,
    )
    assert case.passed, [c.model_dump() for c in case.checks if not c.passed]
    assert executed.status == "executed"
    assert session.calls.count("get_recent_alerts") == 2  # pre-check + verification only
