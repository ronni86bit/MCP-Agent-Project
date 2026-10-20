"""Phase 9 Gate 3: model-suite harness — recorder, classifier, case wiring.

Deterministic tests use scripted doubles end-to-end (M-SEL over stub servers,
M-CANON over a scripted AgentService) so NO real model is needed for the
harness contract. Exactly one Ollama-marked smoke test exercises the harness
against the real model when it is available; it is excluded from the
deterministic gate.
"""

import asyncio

import pytest

from backend.agent.llm.adapter import (
    LLMError,
    LLMMessage,
    LLMResponse,
    LLMToolCall,
    MalformedOutputError,
)
from backend.agent.models import (
    Claim,
    Confidence,
    InvestigationReport,
    Plan,
    PlanStep,
    Understanding,
)
from backend.config import Settings
from backend.evaluation import cases_model
from backend.evaluation.failure_classification import (
    FailureEvidence,
    classify_failure,
)
from backend.evaluation.recording import RecordingLLM
from backend.evaluation.schema import CaseResult
from backend.mcp.client_manager import MultiMCPManager
from tests.integration.test_agent_loop import StubMulti, StubServerManager
from tests.scripted import ScriptedLLM


# -- doubles ----------------------------------------------------------------------


class UsageLLM(ScriptedLLM):
    """ScriptedLLM whose responses carry token counts (as the real adapter does)."""

    def __init__(self, base: ScriptedLLM, prompt_tokens=11, completion_tokens=7):
        super().__init__(understanding=base.understanding, plan=base.plan,
                         selections=list(base.selections), report=base.report)
        self._usage = (prompt_tokens, completion_tokens)

    def _response(self):
        return LLMResponse(model=self.model_name, prompt_tokens=self._usage[0],
                           completion_tokens=self._usage[1])

    async def complete_structured(self, messages, schema_model, *, max_tokens=None):
        if schema_model is Understanding:
            return self.understanding, self._response()
        if schema_model is Plan:
            return self.plan, self._response()
        if schema_model is InvestigationReport:
            return self.report, self._response()
        raise MalformedOutputError("unexpected schema")

    async def chat_with_tools(self, messages, tools, *, max_tokens=None):
        self.select_calls += 1
        if not self.selections:
            return self._response()
        return LLMResponse(content="", tool_calls=[self.selections.pop(0)],
                           prompt_tokens=self._usage[0], completion_tokens=self._usage[1])


class RaisingInner(ScriptedLLM):
    def __init__(self, base: ScriptedLLM):
        super().__init__(understanding=base.understanding, plan=base.plan,
                         selections=list(base.selections), report=base.report)

    async def chat_with_tools(self, messages, tools, *, max_tokens=None):
        raise LLMError("provider exploded")


def _scripted(selections, report=None):
    return ScriptedLLM(
        understanding=Understanding(goal="explain the drop", time_frame="2026-10-05",
                                    focus_areas=["checkout"],
                                    success_criteria="grounded answer"),
        plan=Plan(goal="investigate", steps=[PlanStep(id=i + 1, action=f"step {i + 1}",
                                            evidence_needed="e")
                                   for i in range(max(len(selections), 1))],
                  expected_evidence=["evidence"], completion_criteria="evidence supports a conclusion"),
        selections=list(selections),
        report=report or InvestigationReport(
            conclusion="Revenue fell during the incident window across channels.",
            claims=[Claim(text="Revenue fell during the incident.",
                          evidence_ids=["ev-0001"])],
            confidence=Confidence.medium, confidence_rationale="r"),
    )


def _stub_managers():
    return StubMulti({
        "postgres-mcp": StubServerManager(["get_daily_sales", "compare_sales",
                                           "get_region_sales", "get_sales_trend"]),
        "monitoring-mcp": StubServerManager(["get_error_rate"]),
        "github-mcp": StubServerManager(["get_deployments"]),
    })


# -- requirements 1-4: RecordingLLM ------------------------------------------------


async def test_recorder_delegates_and_returns_responses_unchanged():
    selections = [LLMToolCall(id="c1", name="postgres-mcp__get_daily_sales",
                              arguments={"start_date": "2026-10-05",
                                         "end_date": "2026-10-05"})]
    inner = _scripted(selections)
    recorder = RecordingLLM(inner)

    understanding, response = await recorder.complete_structured(
        [LLMMessage(role="user", content="x")], Understanding)
    assert understanding is inner.understanding  # same object, not a copy
    assert isinstance(response, LLMResponse)

    tool_response = await recorder.chat_with_tools(
        [LLMMessage(role="user", content="x")], tools=[{"name": "t"}])
    assert tool_response.tool_calls == selections  # delegation intact
    assert recorder.records[-1].method == "chat_with_tools"
    assert recorder.records[-1].ok is True
    await recorder.check_provider()
    assert [r.method for r in recorder.records] == [
        "complete_structured", "chat_with_tools", "check_provider"]


async def test_recorder_captures_usage_latency_and_unavailable_usage():
    with_usage = RecordingLLM(UsageLLM(_scripted([])))
    await with_usage.complete_structured([LLMMessage(role="user", content="x")],
                                         Understanding)
    await with_usage.chat_with_tools([LLMMessage(role="user", content="x")], tools=[])
    totals = with_usage.totals()
    assert totals["prompt_tokens"] == 22 and totals["completion_tokens"] == 14
    assert totals["calls"] == 2 and totals["usage_missing_calls"] == 0
    assert totals["llm_ms"] >= 0  # scripted calls are sub-millisecond

    # latency recording is real: a deliberately slow inner call dominates llm_ms
    class SlowInner(UsageLLM):
        async def chat_with_tools(self, messages, tools, *, max_tokens=None):
            await asyncio.sleep(0.05)
            return await super().chat_with_tools(messages, tools, max_tokens=max_tokens)

    slow = RecordingLLM(SlowInner(_scripted([])))
    await slow.chat_with_tools([LLMMessage(role="user", content="x")], tools=[])
    assert slow.totals()["llm_ms"] >= 40.0

    without_usage = RecordingLLM(_scripted([]))  # scripted responses carry no usage
    await without_usage.complete_structured([LLMMessage(role="user", content="x")],
                                            Understanding)
    totals = without_usage.totals()
    assert totals["prompt_tokens"] is None and totals["completion_tokens"] is None
    assert totals["usage_missing_calls"] == 1  # unavailable usage is null, not zero


async def test_recorder_preserves_provider_exceptions():
    inner = RaisingInner(_scripted([]))
    recorder = RecordingLLM(inner)
    with pytest.raises(LLMError) as excinfo:
        await recorder.chat_with_tools([LLMMessage(role="user", content="x")], tools=[])
    assert str(excinfo.value) == "provider exploded"  # unchanged exception
    assert recorder.records[-1].ok is False
    assert recorder.records[-1].error_class == "LLMError"
    assert recorder.totals()["calls"] == 1


async def test_recorder_preserves_cancellation():
    class SlowInner(ScriptedLLM):
        def __init__(self, base: ScriptedLLM):
            super().__init__(understanding=base.understanding, plan=base.plan,
                             selections=list(base.selections), report=base.report)

        async def chat_with_tools(self, messages, tools, *, max_tokens=None):
            await asyncio.Event().wait()  # cancelled by the test

    recorder = RecordingLLM(SlowInner(_scripted([])))
    task = asyncio.ensure_future(
        recorder.chat_with_tools([LLMMessage(role="user", content="x")], tools=[]))
    await asyncio.sleep(0.02)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert recorder.records[-1].ok is False
    assert recorder.records[-1].error_class == "CancelledError"


# -- requirement 5-7: conservative classification -----------------------------------


def test_classification_table_covers_every_layer():
    def _verdict(ev):
        return classify_failure(ev).layer

    assert _verdict(FailureEvidence(case_passed=True)) == "none"

    assert _verdict(FailureEvidence(
        case_passed=False, infra_error_classes=("ProviderUnavailableError",),
    )) == "infra"
    assert _verdict(FailureEvidence(
        case_passed=False, infra_error_classes=("ToolTimeoutError",),
    )) == "infra"
    assert _verdict(FailureEvidence(
        case_passed=False, rate_limit_signature_seen=True, infra_verified_healthy=True,
    )) == "infra"
    assert _verdict(FailureEvidence(
        case_passed=False, infra_verified_healthy=False,
    )) == "infra"

    assert _verdict(FailureEvidence(
        case_passed=False, budget_exhausted=True, infra_verified_healthy=True,
    )) == "budget"

    assert _verdict(FailureEvidence(
        case_passed=False, infra_verified_healthy=True, terminal=True,
        detail={"failed_checks": ["selected_tool_matches_expected"]},
    )) == "reasoning"

    assert _verdict(FailureEvidence(
        case_passed=False, harness_error_class="CancelledError",
    )) == "unknown"
    assert _verdict(FailureEvidence(
        case_passed=False, terminal=False, infra_verified_healthy=True,
    )) == "unknown"
    # infra errors dominate an ambiguous budget burn (conservative)
    assert _verdict(FailureEvidence(
        case_passed=False, budget_exhausted=True,
        infra_error_classes=("TransportError",),
    )) == "infra"
    # a mere exception with no health evidence is NEVER reasoning
    assert _verdict(FailureEvidence(case_passed=False)) == "unknown"


def test_forced_typed_infra_failure_classified_infra():
    verdict = classify_failure(FailureEvidence(
        case_passed=False, terminal=True,
        infra_error_classes=("ProviderUnavailableError", "TransportError"),
        infra_verified_healthy=None,
        detail={"failed_checks": ["probe_0_invocation_ok"]},
    ))
    assert verdict.layer == "infra"
    assert sorted(verdict.diagnostics["infra_error_classes"]) == [
        "ProviderUnavailableError", "TransportError"]


def test_selection_failure_under_healthy_infrastructure_is_reasoning():
    verdict = classify_failure(FailureEvidence(
        case_passed=False, terminal=True,
        infra_verified_healthy=True,
        detail={"failed_checks": ["probe_1_selected_tool_matches_expected"],
                "probes_completed": 6, "probe_errors": 0},
    ))
    assert verdict.layer == "reasoning"


# -- requirement 8: model rows conform to schema v1 (scripted, deterministic) ------


async def test_msel_row_conforms_and_preserves_grader_semantics():
    good = LLMToolCall(id="c1", name="postgres-mcp__get_daily_sales",
                       arguments={"start_date": "2026-10-05", "end_date": "2026-10-05"})
    wrong = LLMToolCall(id="c2", name="postgres-mcp__get_region_sales",
                        arguments={"start_date": "2026-10-05", "end_date": "2026-10-05"})
    managers = _stub_managers()
    case = await cases_model.case_msel(
        llm=_scripted([good, wrong]), managers=managers, settings=Settings())

    # schema v1 conformance
    payload = case.model_dump(mode="json")
    revived = CaseResult.model_validate(payload)
    assert revived.passed == case.passed and revived.failure_layer == case.failure_layer
    assert case.kind == "model" and case.suite == "model"

    # existing grader semantics preserved: probe 0 passes, the wrong-tool probe
    # fails exactly the selection predicate (thresholds unchanged)
    by_name = {c.name: c for c in case.checks}
    assert by_name["probe_0_selected_tool_matches_expected"].passed is True
    assert by_name["probe_1_selected_tool_matches_expected"].passed is False
    assert not case.passed
    # scripted run under stubs: no invocation errors -> verified healthy -> reasoning
    assert case.failure_layer == "reasoning"
    assert case.observed["totals"]["calls"] == 6  # one chat call per probe


async def test_mcanon_row_conforms_and_classifies_reasoning_when_healthy():
    daily = LLMToolCall(id="c1", name="postgres-mcp__get_daily_sales",
                        arguments={"start_date": "2026-10-05", "end_date": "2026-10-05"})
    alerts = LLMToolCall(id="c2", name="monitoring-mcp__get_recent_alerts",
                         arguments={"window_start": "2026-10-03",
                                    "window_end": "2026-10-05"})
    settings = Settings(max_investigation_seconds=60, max_llm_calls=16)
    assert settings.action_mode_enabled is False
    case = await cases_model.case_mcanon(
        llm=_scripted([daily, alerts]), settings=settings,
        manager_factory=lambda: _stub_managers(),
    )
    CaseResult.model_validate(case.model_dump(mode="json"))
    assert case.observed["status"] == "complete"
    assert case.observed["totals"]["calls"] >= 4
    # scripted canon collected 2 servers / 2 evidence: the canonical checks fail
    # on coverage, infrastructure is verified healthy -> reasoning (conservative
    # classification exercised end-to-end)
    failed = {c.name for c in case.checks if not c.passed}
    assert "server_coverage" in failed and "fixture_decline_present" in failed
    assert "action_mode_disabled" in {c.name for c in case.checks}
    assert case.failure_layer == "reasoning"
    assert "server_coverage" in case.failure_diagnostics["failed_checks"]


# -- requirement 9: the deterministic suite never includes model cases --------------


def test_model_cases_are_not_in_the_deterministic_suite():
    import scripts.run_benchmark as runner

    deterministic_ids = [s.case_id for s in runner.build_case_specs()]
    assert not any(cid.startswith("M-") for cid in deterministic_ids)
    model_ids = [s.case_id for s in runner.build_model_specs(lambda: _scripted([]))]
    assert model_ids == ["M-SEL", "M-CANON"]


# -- requirement 10: existing evaluation methodology untouched ----------------------


def test_existing_evaluation_tests_keep_their_assertions():
    import inspect

    from tests.evaluation import test_canonical_smoke, test_model_smoke

    # the preserved threshold, still written in the existing test
    source = inspect.getsource(test_model_smoke)
    assert 'row["selection_ms"] < 180_000' in source
    # the preserved assertion entry points exist
    assert hasattr(test_model_smoke, "test_model_selects_expected_tools")
    assert hasattr(test_canonical_smoke, "test_canonical_question_correlates_three_systems")
    assert hasattr(test_canonical_smoke, "_window_days")
    # and the Gate 1 graders still mirror the same threshold
    from backend.evaluation import graders
    assert graders.SELECTION_LATENCY_BUDGET_MS == 180_000.0


# -- the single Ollama-marked harness smoke test ------------------------------------
# Runs only when the local Ollama endpoint and model are reachable (session
# fixture skips otherwise). Verifies the HARNESS (recording, row production)
# with one real probe — NOT selection quality, which stays the benchmark's job.


@pytest.mark.ollama
async def test_harness_smoke_real_model(real_llm):
    from backend.agent.service import default_server_factories
    from backend.agent.smoke import DEFAULT_PROBES, run_selection_probes

    recorder = RecordingLLM(real_llm)
    managers = MultiMCPManager(default_server_factories(Settings()))
    async with managers:
        rows = await run_selection_probes(recorder, managers, DEFAULT_PROBES[:1])
    assert len(rows) == 1
    row = rows[0]
    assert set(row) >= {"question", "expected_tool", "selected_tool", "arguments_valid",
                        "invocation_ok", "selection_ms", "execution_ms"}
    assert row["selection_ms"] > 0
    totals = recorder.totals()
    assert totals["calls"] == 1 and totals["llm_ms"] > 0
    print(f"[harness-smoke] probe: selected={row['selected_tool']} "
          f"invoked={row['invocation_ok']} selection_ms={row['selection_ms']} "
          f"usage={totals}")
