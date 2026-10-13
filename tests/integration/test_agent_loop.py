"""InvestigationLoop integration tests: scripted LLM, real MCP (plus stub
managers for failure paths). Deterministic — no Ollama required.

Phase 4: the loop addresses tools by qualified name (server__tool) and can
correlate evidence across postgres-mcp, monitoring-mcp, and github-mcp.
"""

import asyncio
from types import SimpleNamespace

import pytest

from backend.agent.loop import InvestigationLoop
from backend.agent.llm.adapter import LLMToolCall, MalformedOutputError, ProviderUnavailableError
from backend.agent.catalog import SERVER_DOMAINS, SERVER_ALLOWLISTS
from backend.agent.evidence import EvidenceStore
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
from backend.mcp.client_manager import MultiMCPManager, ToolInfo
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


def _pg_multi(pg_manager):
    return lambda: MultiMCPManager({"postgres-mcp": lambda: pg_manager()})


def _daily_call(step_args: dict | None = None) -> LLMToolCall:
    return LLMToolCall(
        id="call-1",
        name="postgres-mcp__get_daily_sales",
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


class StubServerManager:
    """Duck-typed per-server MCP manager: deterministic tools, payloads, failures."""

    def __init__(self, tools: list[str], *, payloads: dict | None = None, fail_names: set | None = None):
        self.name = "stub"
        self._tools = [
            ToolInfo(name=name, description=f"{name} stub", input_schema=OBJECT_SCHEMA) for name in tools
        ]
        self._payloads = payloads or {}
        self._fail = set(fail_names or ())
        self.calls: list[tuple[str, dict]] = []

    def tools(self) -> list[ToolInfo]:
        return self._tools

    async def call_tool(self, name: str, arguments: dict):
        self.calls.append((name, arguments))
        if name in self._fail:
            raise ToolExecutionError(f"{name} exploded", tool=name)
        payload = self._payloads.get(name, {"count": 1, "rows": [{"date": "2026-10-05", "revenue": 100.0}]})
        return SimpleNamespace(structured_content=payload, content=[])


class StubMulti:
    """Duck-typed MultiMCPManager over stub servers."""

    def __init__(self, servers: dict[str, StubServerManager]):
        self.managers = servers

    async def __aenter__(self) -> "StubMulti":
        return self

    async def __aexit__(self, *exc_info) -> bool:
        return False

    async def call_tool(self, server: str, name: str, arguments: dict):
        return await self.managers[server].call_tool(name, arguments)


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
    record = await _run(scripted, _pg_multi(pg_manager))
    assert record.status == "complete"
    assert record.plan is not None and record.understanding is not None
    assert record.evidence_ids == ["ev-0001"]
    assert record.steps[0].status == "ok"
    assert record.report is not None
    assert record.verification is not None and record.verification.ok
    for claim in record.report.claims:
        assert claim.evidence_ids, "every claim must cite evidence"


async def test_multi_step_investigation_accumulates_evidence(pg_manager):
    selections = [
        _daily_call({"start_date": "2026-10-01", "end_date": "2026-10-05"}),
        LLMToolCall(
            id="call-2",
            name="postgres-mcp__compare_sales",
            arguments={
                "window_a_start": "2026-09-30", "window_a_end": "2026-10-02",
                "window_b_start": "2026-10-03", "window_b_end": "2026-10-05",
            },
        ),
        LLMToolCall(
            id="call-3",
            name="postgres-mcp__get_region_sales",
            arguments={"start_date": "2026-10-03", "end_date": "2026-10-05"},
        ),
    ]
    record = await _run(_scripted(selections), _pg_multi(pg_manager))
    assert record.status == "complete"
    assert record.evidence_ids == ["ev-0001", "ev-0002", "ev-0003"]
    assert all(step.status == "ok" for step in record.steps[:3])
    assert record.llm_calls == 6  # understand + plan + 3 selections + synthesis


async def test_cross_system_evidence_spans_three_servers():
    servers = {
        "postgres-mcp": StubServerManager(
            ["compare_sales"],
            payloads={"compare_sales": {"metric": "revenue", "delta_pct": -31.0, "count": 0}},
        ),
        "monitoring-mcp": StubServerManager(
            ["get_error_rate"],
            payloads={"get_error_rate": {"service": "checkout", "summary": {"window_avg_pct": 9.4}, "count": 0}},
        ),
        "github-mcp": StubServerManager(
            ["get_deployments"],
            payloads={"get_deployments": {"count": 1, "deployments": [{"service": "checkout", "source": "scenario-fixture"}]}},
        ),
    }
    selections = [
        LLMToolCall(
            id="c1",
            name="postgres-mcp__compare_sales",
            arguments={
                "window_a_start": "2026-09-30", "window_a_end": "2026-10-02",
                "window_b_start": "2026-10-03", "window_b_end": "2026-10-05",
            },
        ),
        LLMToolCall(
            id="c2",
            name="monitoring-mcp__get_error_rate",
            arguments={"service": "checkout", "window_start": "2026-10-03", "window_end": "2026-10-05"},
        ),
        LLMToolCall(id="c3", name="github-mcp__get_deployments", arguments={}),
    ]
    report = InvestigationReport(
        conclusion="Sales decline is temporally correlated with checkout degradation after the deployment.",
        claims=[
            Claim(text="Revenue fell 31% vs the prior window.", evidence_ids=["ev-0001"]),
            Claim(text="Checkout error rate averaged 9.4%.", evidence_ids=["ev-0002"]),
            Claim(text="A checkout deployment preceded the degradation.", evidence_ids=["ev-0003"]),
        ],
        confidence=Confidence.medium,
        confidence_rationale="three systems align temporally",
    )
    record = await _run(_scripted(selections, report=report), lambda: StubMulti(servers))
    assert record.status == "complete"

    assert len(record.evidence_ids) == 3
    # evidence preserves server ownership across all three systems
    tool_servers = {
        observation.tool.split("__")[0] for observation in record.tool_calls if observation.status == "ok"
    }
    assert tool_servers == {"postgres-mcp", "monitoring-mcp", "github-mcp"}
    assert record.report.claims and all(claim.evidence_ids for claim in record.report.claims)
    assert record.verification is not None and record.verification.ok
    assert SERVER_DOMAINS["monitoring-mcp"] == "operational"


async def test_invalid_arguments_get_exactly_one_repair_attempt(pg_manager):
    selections = [
        _daily_call({"start_date": "2026-10-05", "end_date": "2026-10-05", "limit": "many"}),
        _daily_call(),
    ]
    record = await _run(_scripted(selections), _pg_multi(pg_manager))
    assert record.status == "complete"
    rejections = [obs for obs in record.tool_calls if obs.status == "error" and obs.attempt == 1]
    assert len(rejections) == 1 and rejections[0].tool == "postgres-mcp__get_daily_sales"
    repaired = [obs for obs in record.tool_calls if obs.attempt == 2]
    assert len(repaired) == 1 and repaired[0].status == "ok"
    assert record.evidence_ids == ["ev-0001"]


async def test_unknown_tool_is_rejected_without_execution(pg_manager):
    selections = [
        LLMToolCall(id="call-bad", name="postgres-mcp__delete_all_sales", arguments={}),
        _daily_call(),
    ]
    record = await _run(_scripted(selections), _pg_multi(pg_manager))
    assert record.status == "complete"
    rejected = [obs for obs in record.tool_calls if obs.attempt == 1]
    assert rejected[0].status == "error" and "unknown tool" in (rejected[0].error or "")
    assert [obs.tool for obs in record.tool_calls if obs.status == "ok"] == ["postgres-mcp__get_daily_sales"]


async def test_step_limit_bounds_the_loop():
    servers = {"postgres-mcp": StubServerManager(["get_daily_sales"])}
    selections = [_daily_call({"start_date": f"2026-10-0{i}", "end_date": f"2026-10-0{i}"}) for i in range(1, 6)]
    record = await _run(
        _scripted(selections), lambda: StubMulti(servers), _settings(max_investigation_steps=2)
    )
    assert record.status == "complete"
    assert len(record.steps) == 2
    assert record.llm_calls <= 2 + 3  # selections + understand/plan/synthesis


async def test_tool_failure_is_recorded_then_recovered_via_next_step():
    servers = {
        "postgres-mcp": StubServerManager(["get_daily_sales", "get_sales_trend"], fail_names={"get_daily_sales"}),
    }
    selections = [
        _daily_call(),
        LLMToolCall(
            id="call-2",
            name="postgres-mcp__get_sales_trend",
            arguments={"start_date": "2026-10-01", "end_date": "2026-10-05"},
        ),
    ]
    record = await _run(_scripted(selections), lambda: StubMulti(servers))
    assert record.status == "complete"
    failed = [obs for obs in record.tool_calls if obs.status == "error" and obs.error_class == "ToolExecutionError"]
    assert failed and failed[0].step == 1
    assert record.evidence_ids == ["ev-0001"]
    assert any("step 1" in warning for warning in record.warnings)


async def test_all_tool_failures_terminate_with_explicit_gap():
    servers = {"postgres-mcp": StubServerManager(["get_daily_sales"], fail_names={"get_daily_sales"})}
    record = await _run(_scripted([_daily_call()]), lambda: StubMulti(servers))
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
    record = await _run(scripted, _pg_multi(pg_manager))
    assert record.status == "failed"
    assert "llm failure" in record.failure_reason
    assert record.evidence_ids == ["ev-0001"]  # collected evidence preserved


async def test_provider_unavailable_fails_fast():
    scripted = RaisingLLM(ProviderUnavailableError("cannot reach LLM provider"))
    servers = {"postgres-mcp": StubServerManager(["get_daily_sales"])}
    record = await _run(scripted, lambda: StubMulti(servers))
    assert record.status == "failed"
    assert "cannot reach LLM provider" in record.failure_reason


async def test_wall_clock_budget_terminates_the_loop():
    class SlowServer(StubServerManager):
        async def call_tool(self, name, arguments):
            await asyncio.sleep(1.0)
            return await super().call_tool(name, arguments)

    servers = {"postgres-mcp": SlowServer(["get_daily_sales"])}
    record = await _run(
        _scripted([_daily_call()]),
        lambda: StubMulti(servers),
        _settings(max_investigation_seconds=0.3),
    )
    assert record.status == "failed"
    assert "wall-clock" in record.failure_reason


@pytest.mark.parametrize("tool,server", [(t, s) for s, tools in SERVER_ALLOWLISTS.items() for t in sorted(tools)])
def test_allowlists_cover_exactly_fourteen_readonly_tools(tool, server):
    assert len(SERVER_ALLOWLISTS) == 3
    total = sum(len(tools) for tools in SERVER_ALLOWLISTS.values())
    assert total == 14
    assert SERVER_DOMAINS[server] in {"business", "operational", "engineering"}
