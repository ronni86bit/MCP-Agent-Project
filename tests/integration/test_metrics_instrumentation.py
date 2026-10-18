"""Phase 8 Gate B: instrumentation of the live components.

Scripted-loop, executor, breaker/rate-limit, approval-lifecycle, and /metrics
endpoint coverage. Every metric assertion is a before/after delta, so tests do
not depend on execution order or on counters left over from other tests.
"""

import asyncio
import uuid
from types import SimpleNamespace

import httpx
import pytest

from backend.agent.evidence import EvidenceStore
from backend.agent.executor import Executor
from backend.agent.llm.adapter import LLMToolCall
from backend.agent.loop import InvestigationLoop
from backend.agent.models import (
    ActionProposal,
    Claim,
    Confidence,
    InvestigationReport,
    Plan,
    PlanStep,
    Understanding,
)
from backend.agent.state import InvestigationRecord
from backend.config import Settings
from backend.main import create_app
from backend.mcp.client_manager import MultiMCPManager
from backend.mcp.errors import ToolTimeoutError, TransportError
from backend.observability.metrics import REGISTRY
from backend.reliability import CircuitOpenError, CircuitState
from backend.security.approval_service import ApprovalService
from tests.integration.test_agent_loop import StubMulti, StubServerManager
from tests.scripted import RaisingLLM, ScriptedLLM
from tests.unit.test_approval_lifecycle import (
    ALERT,
    FakeSession,
    FakeSessionCM,
    FixedClock,
    _evidence,
    _now,
)


def _sample(name: str, labels: dict | None = None) -> float | None:
    return REGISTRY.get_sample_value(name, labels or {})


# -- scripted investigation: each logical event counted exactly once ----------


def _loop_settings() -> Settings:
    return Settings(
        max_investigation_steps=3,
        max_plan_steps=2,
        max_llm_calls=16,
        max_investigation_seconds=60,
        mcp_call_timeout_seconds=15,
    )


def _scripted_llm() -> ScriptedLLM:
    return ScriptedLLM(
        understanding=Understanding(
            goal="explain the drop", time_frame="2026-10-05",
            focus_areas=["checkout"], success_criteria="grounded answer",
        ),
        plan=Plan(
            goal="investigate the sales drop",
            steps=[
                PlanStep(id=1, action="establish daily sales", evidence_needed="daily totals"),
                PlanStep(id=2, action="check error rate", evidence_needed="error rate"),
            ],
            expected_evidence=["totals", "errors"],
            completion_criteria="evidence supports a conclusion",
        ),
        selections=[
            LLMToolCall(id="c1", name="postgres-mcp__get_daily_sales",
                        arguments={"start_date": "2026-10-03", "end_date": "2026-10-05"}),
            LLMToolCall(id="c2", name="monitoring-mcp__get_error_rate",
                        arguments={"service": "checkout", "window_start": "2026-10-03",
                                   "window_end": "2026-10-05"}),
        ],
        report=InvestigationReport(
            conclusion="Revenue fell while checkout errors rose; the two are consistent.",
            claims=[
                Claim(text="Sales fell over the window.", evidence_ids=["ev-0001"]),
                Claim(text="Checkout error rate rose.", evidence_ids=["ev-0002"]),
            ],
            confidence=Confidence.medium,
            confidence_rationale="two independent sources agree",
        ),
    )


async def test_scripted_investigation_counts_each_event_exactly_once():
    record = InvestigationRecord(
        investigation_id=f"inv-{uuid.uuid4().hex[:8]}",
        request_id=f"req-{uuid.uuid4().hex[:8]}",
        question="Why did today's sales drop?",
        status="running",
    )
    servers = {
        "postgres-mcp": StubServerManager(["get_daily_sales"]),
        "monitoring-mcp": StubServerManager(["get_error_rate"]),
    }
    loop = InvestigationLoop(
        llm=_scripted_llm(),
        manager_factory=lambda: StubMulti(servers),
        record=record,
        settings=_loop_settings(),
    )
    purposes = ("understand", "plan", "select_tool", "review_plan", "synthesize")

    def _llm_snapshot() -> dict:
        return {p: _sample("ops_agent_llm_calls_total", {"purpose": p, "outcome": "ok"}) or 0.0
                for p in purposes}

    llm_before = _llm_snapshot()
    tool_before = _sample("ops_agent_mcp_tool_calls_total",
                          {"server": "postgres-mcp", "tool": "get_daily_sales", "outcome": "ok"}) or 0.0
    tool_ms_before = _sample("ops_agent_mcp_tool_duration_seconds_count",
                             {"server": "postgres-mcp", "tool": "get_daily_sales"}) or 0.0
    inv_before = _sample("ops_agent_investigations_total", {"status": "complete"}) or 0.0
    dur_before = _sample("ops_agent_investigation_duration_seconds_count") or 0.0
    budget_before = _sample("ops_agent_investigation_budget_exhausted_total",
                            {"budget_class": "wall_clock"}) or 0.0

    await loop.run()

    assert record.status == "complete"
    llm_after = _llm_snapshot()
    # understand + plan + 2x select_tool + 1x review_plan (post-step review) + synthesize
    assert sum(llm_after[p] - llm_before[p] for p in purposes) == record.llm_calls == 6
    assert llm_after["select_tool"] - llm_before["select_tool"] == 2
    assert all(llm_after[p] - llm_before[p] == 1 for p in
               ("understand", "plan", "review_plan", "synthesize"))
    assert _sample("ops_agent_mcp_tool_calls_total",
                   {"server": "postgres-mcp", "tool": "get_daily_sales", "outcome": "ok"}) - tool_before == 1
    assert _sample("ops_agent_mcp_tool_duration_seconds_count",
                   {"server": "postgres-mcp", "tool": "get_daily_sales"}) - tool_ms_before == 1
    assert _sample("ops_agent_investigations_total", {"status": "complete"}) - inv_before == 1
    assert _sample("ops_agent_investigation_duration_seconds_count") - dur_before == 1
    assert (_sample("ops_agent_investigation_budget_exhausted_total",
                    {"budget_class": "wall_clock"}) or 0.0) == budget_before


async def test_failed_investigation_counts_llm_outcome_and_terminal_status():
    from backend.agent.llm.adapter import ProviderUnavailableError

    record = InvestigationRecord(
        investigation_id=f"inv-{uuid.uuid4().hex[:8]}",
        request_id=f"req-{uuid.uuid4().hex[:8]}",
        question="Why did today's sales drop?",
        status="running",
    )
    loop = InvestigationLoop(
        llm=RaisingLLM(ProviderUnavailableError("ollama down")),
        manager_factory=lambda: StubMulti({"postgres-mcp": StubServerManager(["get_daily_sales"])}),
        record=record,
        settings=_loop_settings(),
    )
    fail_before = _sample("ops_agent_investigations_total", {"status": "failed"}) or 0.0
    unavailable_before = _sample("ops_agent_llm_calls_total",
                                 {"purpose": "understand", "outcome": "unavailable"}) or 0.0
    duration_before = _sample("ops_agent_investigation_duration_seconds_count") or 0.0

    await loop.run()

    assert record.status == "failed"
    assert _sample("ops_agent_investigations_total", {"status": "failed"}) - fail_before == 1
    assert _sample("ops_agent_llm_calls_total",
                   {"purpose": "understand", "outcome": "unavailable"}) - unavailable_before == 1
    # terminal failures observe the duration too (once per investigation)
    assert _sample("ops_agent_investigation_duration_seconds_count") - duration_before == 1


# -- executor outcomes ---------------------------------------------------------

_TOOL_CALL = SimpleNamespace(name="get_error_rate", arguments={})


def _executor(call_behavior) -> Executor:
    class _Mgrs:
        async def call_tool(self, server, name, arguments):
            return call_behavior()

    entry = SimpleNamespace(server="monitoring-mcp", name="get_error_rate",
                            allowlisted=True, parameters={"type": "object"})
    catalog = SimpleNamespace(resolve=lambda name: entry)
    return Executor(_Mgrs(), catalog, EvidenceStore("inv-exec-metrics"), {})


def test_executor_counts_ok_timeout_and_circuit_open_outcomes():
    ex_ok = _executor(lambda: SimpleNamespace(structured_content={"count": 0}, content=[]))
    ex_timeout = _executor(
        lambda: (_ for _ in ()).throw(ToolTimeoutError("slow", tool="get_error_rate")))
    ex_open = _executor(lambda: (_ for _ in ()).throw(CircuitOpenError("circuit open")))

    def _delta_for(outcome: str, executor: Executor) -> float:
        before = _sample("ops_agent_mcp_tool_calls_total",
                         {"server": "monitoring-mcp", "tool": "get_error_rate", "outcome": outcome}) or 0.0
        asyncio.run(executor.execute(_TOOL_CALL, 1))
        after = _sample("ops_agent_mcp_tool_calls_total",
                        {"server": "monitoring-mcp", "tool": "get_error_rate", "outcome": outcome}) or 0.0
        return after - before

    assert _delta_for("ok", ex_ok) == 1.0
    assert _delta_for("timeout", ex_timeout) == 1.0
    assert _delta_for("circuit_open", ex_open) == 1.0


# -- breaker state/transitions and rate-limit waits -----------------------------


class _BoomManager:
    async def call_tool(self, name, arguments):
        raise TransportError("upstream unreachable", tool=name)


class _OkManager:
    async def call_tool(self, name, arguments):
        return SimpleNamespace(structured_content={"count": 0}, content=[])


def _manager(*, threshold: int, rate_limit: dict | None = None) -> MultiMCPManager:
    return MultiMCPManager(
        {"svc": lambda: None},
        circuit_failure_threshold=threshold,
        circuit_cooldown_seconds=60.0,
        rate_limits_per_minute=rate_limit or {},
    )


def test_breaker_gauge_initialized_at_construction_not_first_call():
    mgr = _manager(threshold=2)
    # __init__ set the gauge for every configured server, before any call
    assert _sample("ops_agent_circuit_breaker_state", {"server": "svc"}) == 0.0


def test_breaker_transitions_counter_and_gauge_track_open():
    mgr = _manager(threshold=2)
    mgr.managers["svc"] = _BoomManager()
    labels = {"server": "svc", "from_state": "closed", "to_state": "open"}

    async def _fail_twice():
        for _ in range(2):
            with pytest.raises(TransportError):
                await mgr.call_tool("svc", "t", {})

    before = _sample("ops_agent_circuit_breaker_transitions_total", labels) or 0.0
    asyncio.run(_fail_twice())
    assert (_sample("ops_agent_circuit_breaker_transitions_total", labels) or 0.0) - before == 1.0
    assert mgr.breakers["svc"].state == CircuitState.open
    assert _sample("ops_agent_circuit_breaker_state", {"server": "svc"}) == 2.0

    # open circuit: rejected fast; no further transition is recorded
    rejected_before = _sample("ops_agent_circuit_breaker_transitions_total", labels)
    with pytest.raises(CircuitOpenError):
        asyncio.run(mgr.call_tool("svc", "t", {}))
    assert _sample("ops_agent_circuit_breaker_transitions_total", labels) == rejected_before


def test_rate_limit_wait_observed_once_per_waiting_call():
    mgr = _manager(threshold=2, rate_limit={"svc": 600})  # 10 tokens/s: ~0.1s wait
    mgr.managers["svc"] = _OkManager()
    bucket = mgr.buckets["svc"]
    bucket._tokens = 0.0
    bucket._updated = bucket._clock()
    # both fields must be reset, or elapsed wall-clock time re-arms the
    # bucket via refill before the call even starts

    waited_before = _sample("ops_agent_rate_limit_waits_total", {"server": "svc"}) or 0.0
    asyncio.run(mgr.call_tool("svc", "t", {}))
    assert (_sample("ops_agent_rate_limit_waits_total", {"server": "svc"}) or 0.0) - waited_before == 1.0
    assert _sample("ops_agent_rate_limit_wait_seconds_count", {"server": "svc"}) is not None


# -- approval lifecycle (Postgres-backed; skips when DB unreachable) ------------


async def test_approval_lifecycle_metrics_fire_once_per_transition(state_test_store):
    store = state_test_store()
    await store.ensure_schema()
    clock = FixedClock(_now())
    record = InvestigationRecord(
        investigation_id=f"inv-{uuid.uuid4().hex[:8]}",
        request_id=f"req-{uuid.uuid4().hex[:8]}",
        question="Why did today's sales drop?",
        status="complete",
    )
    record.plan = Plan(
        goal="investigate",
        steps=[PlanStep(id=1, action="check alerts", evidence_needed="alerts")],
        expected_evidence=["alerts"],
        completion_criteria="done",
    )
    await store.persist_record(record)  # approvals FK -> investigations
    session = FakeSession({
        "get_recent_alerts": [
            {"alerts": [dict(ALERT)], "count": 1},  # pre-check
            {"alerts": [dict(ALERT, acknowledged=True, acknowledged_by="op-1",
                              acknowledged_at="2026-10-05T12:00:01Z")], "count": 1},
        ]
    })
    service = ApprovalService(store, lambda: FakeSessionCM({"current": session}),
                              clock=clock, execution_timeout_seconds=5.0)

    proposal = ActionProposal(
        tool="acknowledge_alert",
        arguments={"alert_id": "ALT-2071", "note": "acknowledged during response"},
        rationale="unacknowledged high-severity alert",
        evidence_ids=["ev-0001"],
    )

    def _approval_sample(status: str) -> float:
        return _sample("ops_agent_approvals_total",
                       {"tool": "acknowledge_alert", "status": status}) or 0.0

    before = {s: _approval_sample(s) for s in ("proposed", "approved", "executing", "executed")}
    verification_before = _sample("ops_agent_approval_verification_total",
                                  {"tool": "acknowledge_alert", "ok": "true"}) or 0.0

    approval = await service.persist_proposal(record, _evidence(), proposal)
    await service.decide(approval.approval_id, "approved", "op-1")
    executed = await service.execute(approval.approval_id, "op-2")

    assert executed.status == "executed"
    after = {s: _approval_sample(s) for s in before}
    assert all(after[s] - before[s] == 1.0 for s in before), (
        f"expected exactly one transition into each lifecycle status; deltas: "
        f"{ {s: after[s] - before[s] for s in before} }"
    )
    assert (_sample("ops_agent_approval_verification_total",
                    {"tool": "acknowledge_alert", "ok": "true"}) or 0.0) - verification_before == 1.0


# -- /metrics endpoint -----------------------------------------------------------


async def test_metrics_endpoint_serves_the_private_registry():
    app = create_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        response = await http.get("/metrics")
    assert response.status_code == 200
    assert "text/plain" in response.headers["content-type"]
    assert "ops_agent_investigations_total" in response.text
    # the private registry only — no default-registry process metrics attached
    assert "python_gc_" not in response.text
