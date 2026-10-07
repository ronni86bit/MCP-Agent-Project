"""InvestigationLoop integration tests: scripted LLM, real MCP (plus a stub
manager for failure paths). Deterministic — no Ollama required."""

import asyncio
from types import SimpleNamespace

import pytest

from backend.agent.loop import InvestigationLoop
from backend.agent.llm.adapter import LLMToolCall, MalformedOutputError, ProviderUnavailableError
from backend.agent.models import (
    Claim,
    Confidence,
    InvestigationReport,
    Plan,
    PlanStep,
    Understanding,
)
from backend.agent.state import InvestigationRecord
from backend.config import Settings
from backend.mcp.client_manager import ToolInfo
from backend.mcp.errors import ToolExecutionError
from tests.scripted import MalformedLLM, RaisingLLM, ScriptedLLM

OBJECT_SCHEMA = {"type": "object", "properties": {}, "additionalProperties": True}


def _settings(**overrides) -> Settings:
    values = dict(
        max_investigation_steps=4,
        max_plan_steps=4,
        max_llm_calls=16,
        max_investigation_seconds=60,
        mcp_call_timeout_seconds=15,
    )
    values.update(overrides)
    return Settings(**values)


def _daily_call(step_args: dict | None = None) -> LLMToolCall:
    return LLMToolCall(
        id="call-1",
        name="get_daily_sales",
        arguments=step_args or {"start_date": "2026-10-05", "end_date": "2026-10-05"},
    )


def _scripted(selections: list, report: InvestigationReport | None = None) -> ScriptedLLM:
    return ScriptedLLM(
        understanding=Understanding(
            goal="explain the sales drop",
            time_frame="today (2026-10-05)",
            focus_areas=["revenue"],
            success_criteria="a grounded explanation",
        ),
        plan=Plan(
            goal="investigate the sales drop",
            steps=[
                PlanStep(id=1, action="establish daily sales", evidence_needed="daily totals"),
                PlanStep(id=2, action="compare against baseline", evidence_needed="delta pct"),
                PlanStep(id=3, action="view regional impact", evidence_needed="regional totals"),
            ],
            expected_evidence=["daily totals", "comparison", "regions"],
            completion_criteria="evidence supports a conclusion",
        ),
        selections=selections,
        report=report
        or InvestigationReport(
            conclusion="Revenue fell sharply on 2026-10-05, consistent with the collected evidence.",
            claims=[
                Claim(text="Revenue dropped versus baseline.", evidence_ids=["ev-0001", "ev-0002"]),
                Claim(text="Multiple regions were affected.", evidence_ids=["ev-0003"]),
            ],
            confidence=Confidence.medium,
            confidence_rationale="several independent tool results agree",
        ),
    )


class StubManager:
    """Duck-typed MCP manager: deterministic tools, payloads, and failures."""

    def __init__(self, tools: list[str], *, payloads: dict | None = None, fail_names: set | None = None):
        self.name = "stub-postgres"
        self._tools = [
            ToolInfo(name=name, description=f"{name} stub", input_schema=OBJECT_SCHEMA) for name in tools
        ]
        self._payloads = payloads or {}
        self._fail = set(fail_names or ())
        self.calls: list[tuple[str, dict]] = []

    async def __aenter__(self) -> "StubManager":
        return self

    async def __aexit__(self, *exc_info) -> bool:
        return False

    def tools(self) -> list[ToolInfo]:
        return self._tools

    async def call_tool(self, name: str, arguments: dict):
        self.calls.append((name, arguments))
        if name in self._fail:
            raise ToolExecutionError(f"{name} exploded", tool=name)
        payload = self._payloads.get(name, {"count": 1, "rows": [{"date": "2026-10-05", "revenue": 100.0}]})
        return SimpleNamespace(structured_content=payload, content=[])


async def _run(scripted: ScriptedLLM, manager_factory, settings: Settings | None = None) -> InvestigationRecord:
    record = InvestigationRecord(investigation_id="inv-test", request_id="req-test", question="Why did today's sales drop?")
    loop = InvestigationLoop(
        llm=scripted,
        manager_factory=manager_factory,
        record=record,
        settings=settings or _settings(),
    )
    return await loop.run()


async def test_single_tool_investigation_is_grounded(pg_manager):
    report = InvestigationReport(
        conclusion="Revenue on 2026-10-05 was far below the preceding days, consistent with the evidence.",
        claims=[Claim(text="Revenue dropped versus baseline.", evidence_ids=["ev-0001"])],
        confidence=Confidence.medium,
        confidence_rationale="one direct tool result",
    )
    scripted = _scripted([_daily_call()], report=report)
    record = await _run(scripted, lambda: pg_manager())
    assert record.status == "complete"
    assert record.plan is not None and record.understanding is not None
    assert record.evidence_ids == ["ev-0001"]
    assert record.steps[0].status == "ok"
    assert record.report is not None
    assert record.verification is not None and record.verification.ok
    for claim in record.report.claims:
        assert claim.evidence_ids, "every claim must cite evidence"


async def test_agent_reselects_a_valid_comparison_after_unequal_window_rejection(pg_manager):
    """Server rejects an unequal-duration compare_sales; the next step's
    equal-duration comparison succeeds — recovery without silent normalization."""
    selections = [
        LLMToolCall(
            id="call-bad",
            name="compare_sales",
            arguments={
                "window_a_start": "2026-09-06", "window_a_end": "2026-10-02",  # 27 days
                "window_b_start": "2026-10-03", "window_b_end": "2026-10-05",  # 3 days
            },
        ),
        LLMToolCall(
            id="call-good",
            name="compare_sales",
            arguments={
                "window_a_start": "2026-09-30", "window_a_end": "2026-10-02",  # 3 days
                "window_b_start": "2026-10-03", "window_b_end": "2026-10-05",  # 3 days
            },
        ),
    ]
    record = await _run(_scripted(selections), lambda: pg_manager())
    assert record.status == "complete"
    rejected = [obs for obs in record.tool_calls if obs.status == "error"]
    assert rejected and "equal-duration" in (rejected[0].error or "")
    assert rejected[0].evidence_id is None, "rejected comparison must produce no evidence"
    recovered = [obs for obs in record.tool_calls if obs.status == "ok" and obs.tool == "compare_sales"]
    assert len(recovered) == 1
    assert record.evidence_ids  # the valid comparison became evidence


async def test_multi_step_investigation_accumulates_evidence(pg_manager):
    selections = [
        _daily_call({"start_date": "2026-10-01", "end_date": "2026-10-05"}),
        LLMToolCall(
            id="call-2",
            name="compare_sales",
            arguments={
                "window_a_start": "2026-09-06", "window_a_end": "2026-09-08",
                "window_b_start": "2026-10-03", "window_b_end": "2026-10-05",
            },
        ),
        LLMToolCall(id="call-3", name="get_region_sales", arguments={"start_date": "2026-10-03", "end_date": "2026-10-05"}),
    ]
    record = await _run(_scripted(selections), lambda: pg_manager())
    assert record.status == "complete"
    assert record.evidence_ids == ["ev-0001", "ev-0002", "ev-0003"]
    assert all(step.status == "ok" for step in record.steps[:3])
    assert record.llm_calls == 6  # understand + plan + 3 selections + synthesis


async def test_invalid_arguments_get_exactly_one_repair_attempt(pg_manager):
    selections = [
        _daily_call({"start_date": "2026-10-05", "end_date": "2026-10-05", "limit": "many"}),
        _daily_call(),
    ]
    record = await _run(_scripted(selections), lambda: pg_manager())
    assert record.status == "complete"
    rejections = [obs for obs in record.tool_calls if obs.status == "error" and obs.attempt == 1]
    assert len(rejections) == 1 and rejections[0].tool == "get_daily_sales"
    repaired = [obs for obs in record.tool_calls if obs.attempt == 2]
    assert len(repaired) == 1 and repaired[0].status == "ok"
    assert record.evidence_ids == ["ev-0001"]


async def test_unknown_tool_is_rejected_without_execution(pg_manager):
    manager = pg_manager()
    selections = [
        LLMToolCall(id="call-bad", name="delete_all_sales", arguments={}),
        _daily_call(),
    ]
    record = await _run(_scripted(selections), lambda: manager)
    assert record.status == "complete"
    rejected = [obs for obs in record.tool_calls if obs.attempt == 1]
    assert rejected[0].status == "error" and "unknown tool" in (rejected[0].error or "")
    # the unknown tool never reached MCP: only the repaired call was executed
    assert [obs.tool for obs in record.tool_calls if obs.status == "ok"] == ["get_daily_sales"]


async def test_step_limit_bounds_the_loop():
    manager = StubManager(["get_daily_sales"])
    selections = [_daily_call({"start_date": f"2026-10-0{i}", "end_date": f"2026-10-0{i}"}) for i in range(1, 6)]
    record = await _run(_scripted(selections), lambda: manager, _settings(max_investigation_steps=2))
    assert record.status == "complete"
    assert len(record.steps) == 2
    assert record.llm_calls <= 2 + 3  # selections + understand/plan/synthesis


async def test_tool_failure_is_recorded_then_recovered_via_next_step():
    manager = StubManager(
        ["get_daily_sales", "get_sales_trend"],
        fail_names={"get_daily_sales"},
    )
    selections = [
        _daily_call(),
        LLMToolCall(id="call-2", name="get_sales_trend", arguments={"start_date": "2026-10-01", "end_date": "2026-10-05"}),
    ]
    record = await _run(_scripted(selections), lambda: manager)
    assert record.status == "complete"
    failed = [obs for obs in record.tool_calls if obs.status == "error" and obs.error_class == "ToolExecutionError"]
    assert failed and failed[0].step == 1
    assert record.evidence_ids == ["ev-0001"]
    assert any("step 1" in warning for warning in record.warnings)


async def test_all_tool_failures_terminate_with_explicit_gap():
    manager = StubManager(["get_daily_sales"], fail_names={"get_daily_sales"})
    record = await _run(_scripted([_daily_call()]), lambda: manager)
    assert record.status == "failed"
    assert "no evidence" in record.failure_reason
    assert record.tool_calls[0].error_class == "ToolExecutionError"


async def test_malformed_llm_output_fails_gracefully(pg_manager):
    scripted = MalformedLLM(
        understanding=Understanding(goal="explain", time_frame="today", success_criteria="grounded answer"),
        plan=Plan(goal="investigate", steps=[PlanStep(id=1, action="daily sales", evidence_needed="totals")], completion_criteria="done"),
        selections=[_daily_call()],
        report=InvestigationReport(conclusion="placeholder conclusion"),
    )
    record = await _run(scripted, lambda: pg_manager())
    assert record.status == "failed"
    assert "llm failure" in record.failure_reason
    assert record.evidence_ids == ["ev-0001"]  # collected evidence preserved


async def test_provider_unavailable_fails_fast():
    scripted = RaisingLLM(ProviderUnavailableError("cannot reach LLM provider"))
    record = await _run(scripted, lambda: StubManager(["get_daily_sales"]))
    assert record.status == "failed"
    assert "cannot reach LLM provider" in record.failure_reason


async def test_wall_clock_budget_terminates_the_loop():
    class SlowManager(StubManager):
        async def call_tool(self, name, arguments):
            await asyncio.sleep(1.0)
            return await super().call_tool(name, arguments)

    record = await _run(
        _scripted([_daily_call()]),
        lambda: SlowManager(["get_daily_sales"]),
        _settings(max_investigation_seconds=0.3),
    )
    assert record.status == "failed"
    assert "wall-clock" in record.failure_reason
