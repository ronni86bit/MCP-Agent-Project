"""Deterministic evaluation cases (Phase 9 Gate 1).

Each runner executes the REAL InvestigationLoop over injected deterministic
doubles (`tests/scripted.py` ScriptedLLM + stub/failing managers, or the real
postgres-mcp for boundary cases) and grades the observed record with the pure
graders. Doubles are always INJECTED — this module never imports the test
suite, and no case reaches a real LLM or a real external write.

The approval case grades persisted rows only; the caller (pytest fixture or,
later, the Gate 2 runner) supplies the existing deterministic ApprovalService
harness. No case executes a real external action.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Any

from backend.agent.models import ApprovalRecord
from backend.agent.state import InvestigationRecord
from backend.config import Settings
from backend.observability.metrics import REGISTRY
from backend.evaluation import graders
from backend.evaluation.schema import CaseResult, check

QUESTION = "Why did today's sales drop?"

# metric names used for budget cross-checks (Phase 8 contract)
_BUDGET_METRIC = "ops_agent_investigation_budget_exhausted_total"


def _metric_delta(name: str, labels: dict, before: float | None) -> dict[str, float]:
    after = REGISTRY.get_sample_value(name, labels) or 0.0
    return {str(labels): after - (before or 0.0)}  # type: ignore[dict-item]


async def _run_loop(
    llm: Any,
    manager_factory: Callable[[], Any],
    settings: Settings,
    *,
    state_store: Any | None = None,
    approval_service: Any | None = None,
    question: str = QUESTION,
) -> tuple[InvestigationRecord, float]:
    from backend.agent.loop import InvestigationLoop

    record = InvestigationRecord(
        investigation_id=f"inv-eval-{int(time.monotonic_ns() % 10**10):010d}",
        request_id=f"req-eval-{int(time.monotonic_ns() % 10**10):010d}",
        question=question,
    )
    loop = InvestigationLoop(
        llm=llm,
        manager_factory=manager_factory,
        record=record,
        settings=settings,
        state_store=state_store,
        approval_service=approval_service,
    )
    started = time.perf_counter()
    await loop.run()
    return record, (time.perf_counter() - started) * 1000


def _result(case_id: str, record: InvestigationRecord, latency_ms: float,
            checks: list, **observed: Any) -> CaseResult:
    return CaseResult(
        case_id=case_id,
        observed={
            "status": record.status,
            "llm_calls": record.llm_calls,
            "evidence_ids": list(record.evidence_ids),
            "failure_reason": record.failure_reason,
            **observed,
        },
        checks=checks,
        latency_ms=round(latency_ms, 1),
    )


# -- cases ----------------------------------------------------------------------


async def case_selection_arguments(
    *, llm: Any, managers: Any, settings: Settings, failing_tool: str = "postgres-mcp__compare_sales",
) -> CaseResult:
    """D-SEL: unequal-duration compare_sales is rejected at the verified
    boundary — server-side tool logic (postgres_mcp models.py model_validator),
    NOT JSON Schema. `managers` is a REAL MultiMCPManager: the runner first
    proves the agent-side schema check passes for the invalid arguments (so the
    request reaches the tool), then the loop run proves the server-side typed
    rejection (`ToolExecutionError`), that no evidence was created, and — as
    the positive control — that equal-duration windows execute successfully."""
    # boundary audit: the agent-side schema gate must NOT reject these
    from backend.agent.catalog import SERVER_ALLOWLISTS, ToolCatalog
    from backend.agent.executor import Executor
    from backend.agent.evidence import EvidenceStore
    from backend.agent.models import ToolCallRequest

    catalog = ToolCatalog.from_managers(managers.managers, SERVER_ALLOWLISTS)
    executor = Executor(managers, catalog, EvidenceStore("eval-dsel"), SERVER_ALLOWLISTS)
    invalid = ToolCallRequest(
        name=failing_tool,
        arguments={
            "window_a_start": "2026-10-03", "window_a_end": "2026-10-05",
            "window_b_start": "2026-10-04", "window_b_end": "2026-10-05",
        },
    )
    validation = executor.validate(invalid)
    schema_gate_result = check(
        "schema_gate_passes_invalid_windows",
        validation.ok,
        "agent-side jsonschema cannot express cross-field date equality; the "
        f"request must reach the tool (validation.ok={validation.ok}, error={validation.error})",
    )

    record, latency_ms = await _run_loop(llm, lambda: managers, settings)
    checks = [schema_gate_result, *graders.failure_checks(
        record,
        failing_tool=failing_tool,
        expected_error_class="ToolExecutionError",
    )]
    # the server-side rejection reason must name the actual rule
    rejection = [o for o in record.tool_calls if o.tool == failing_tool and o.status == "error"]
    checks.append(check(
        "server_side_rule_named",
        bool(rejection) and "equal-duration" in (rejection[0].error or ""),
        f"error={(rejection[0].error if rejection else None)}",
    ))
    # positive control: the scripted equal-duration comparison executed ok
    ok_compares = [
        o for o in record.tool_calls
        if o.tool.endswith("__compare_sales") and o.status == "ok"
    ]
    checks.append(check(
        "equal_duration_control_executes",
        bool(ok_compares) and ok_compares[0].evidence_id is not None,
    ))
    return _result(
        "D-SEL", record, latency_ms, checks,
        boundary="server-side model_validator (postgres_mcp/models.py) via ToolError; "
                 "agent-side jsonschema passes",
    )


async def case_evidence_coverage(
    *, llm: Any, manager_factory: Callable[[], Any], settings: Settings,
    expected_servers: frozenset[str],
) -> CaseResult:
    """D-EV: evidence resolves and covers the expected servers."""
    record, latency_ms = await _run_loop(llm, manager_factory, settings)
    return _result(
        "D-EV", record, latency_ms,
        graders.coverage_checks(record, expected_servers=expected_servers),
    )


async def case_grounding(
    *, llm: Any, manager_factory: Callable[[], Any], settings: Settings,
    expect_ok: bool = False, expect_flagged: bool = True, expect_downgraded: bool = True,
) -> CaseResult:
    """D-GROUND: fabricated values and causal wording are caught — graded on
    the verifier's OWN output attached to the record (never recomputed)."""
    record, latency_ms = await _run_loop(llm, manager_factory, settings)
    return _result(
        "D-GROUND", record, latency_ms,
        graders.grounding_checks(
            record, expect_ok=expect_ok,
            expect_flagged=expect_flagged, expect_downgraded=expect_downgraded,
        ),
        verification=dict(record.verification.model_dump()) if record.verification else None,
    )


async def case_insufficient_evidence(
    *, llm: Any, manager_factory: Callable[[], Any], settings: Settings,
) -> CaseResult:
    """D-GAP: with no collectable evidence the loop refuses to fabricate a
    report: failed + explicit reason."""
    record, latency_ms = await _run_loop(llm, manager_factory, settings)
    checks = [
        check("status_failed", record.status == "failed", f"status={record.status}"),
        check(
            "explicit_no_evidence_reason",
            record.failure_reason is not None and "no evidence" in record.failure_reason,
            f"reason={record.failure_reason}",
        ),
        check("report_absent", record.report is None),
        check("no_evidence_collected", not record.evidence_ids),
    ]
    return _result("D-GAP", record, latency_ms, checks)


async def case_tool_failure_recovery(
    *, llm: Any, manager_factory: Callable[[], Any], settings: Settings,
    failing_tool: str, expected_error_class: str = "ToolExecutionError",
) -> CaseResult:
    """D-FAIL: a typed tool failure is recorded and never masked; the loop
    continues (remaining steps still collect evidence)."""
    record, latency_ms = await _run_loop(llm, manager_factory, settings)
    checks = graders.failure_checks(
        record, failing_tool=failing_tool, expected_error_class=expected_error_class,
        expect_no_evidence=False,
    )
    checks.append(graders.check(
        "failure_warning_recorded", any("failed" in w for w in record.warnings),
        f"warnings={record.warnings}",
    ))
    checks.append(graders.check(
        "recovered_with_remaining_evidence",
        record.status == "complete" and bool(record.evidence_ids),
        f"status={record.status} evidence={record.evidence_ids}",
    ))
    return _result("D-FAIL", record, latency_ms, checks)


async def case_circuit_open(
    *, llm: Any, manager_factory: Callable[[], Any], settings: Settings,
    unavailable_server: str,
) -> CaseResult:
    """D-CIRCUIT: an open breaker is 'source unavailable' — reported in
    warnings and report gaps, typed CircuitOpenError, never silent."""
    record, latency_ms = await _run_loop(llm, manager_factory, settings)
    return _result(
        "D-CIRCUIT", record, latency_ms,
        graders.circuit_checks(record, unavailable_server=unavailable_server),
    )


async def case_budget_termination(
    *, llm: Any, manager_factory: Callable[[], Any], settings: Settings,
    budget_class: str,
) -> CaseResult:
    """D-BUDGET: the loop terminates within the configured budget and records
    the exhaustion. `budget_class` selects the scenario: 'llm_calls', 'steps',
    or 'wall_clock' (the runner derives the expected metric label from it)."""
    before = REGISTRY.get_sample_value(_BUDGET_METRIC, {"budget_class": budget_class})
    record, latency_ms = await _run_loop(llm, manager_factory, settings)
    delta = _metric_delta(_BUDGET_METRIC, {"budget_class": budget_class}, before)
    return _result(
        "D-BUDGET", record, latency_ms,
        graders.budget_checks(
            record, max_llm_calls=settings.max_llm_calls,
            # llm/steps paths add an explicit warning; the wall-clock path
            # converts TimeoutError straight to failure (metric only)
            expect_budget_warning=budget_class in ("llm_calls", "steps"),
            budget_metric_delta=delta,
        ),
        budget_class=budget_class,
        budget_metric_delta=delta,
    )


async def case_proposal_lifecycle(
    *, proposal_row: ApprovalRecord, executed_row: ApprovalRecord, latency_ms: float = 0.0,
) -> CaseResult:
    """D-APPROVE: grades the PERSISTED approval rows produced by the existing
    deterministic harness (ScriptedLLM proposal + ApprovalService + fake
    monitoring session). No real write is possible in this path."""
    return CaseResult(
        case_id="D-APPROVE",
        observed={
            "initial_status": proposal_row.status,
            "final_status": executed_row.status,
            "tool": executed_row.tool,
        },
        checks=graders.proposal_checks(persisted=proposal_row, final=executed_row),
        latency_ms=round(latency_ms, 1),
    )


async def case_cancellation(
    *, llm: Any, manager_factory: Callable[[], Any], settings: Settings,
) -> CaseResult:
    """D-CANCEL: hard cancellation propagates (never converted) and the run
    never finalizes (the Phase 8 metric caveat, regression-pinned)."""
    from backend.agent.loop import InvestigationLoop

    record = InvestigationRecord(
        investigation_id=f"inv-eval-{int(time.monotonic_ns() % 10**10):010d}",
        request_id=f"req-eval-{int(time.monotonic_ns() % 10**10):010d}",
        question=QUESTION,
    )
    loop = InvestigationLoop(
        llm=llm, manager_factory=manager_factory, record=record, settings=settings,
    )
    task = asyncio.ensure_future(loop.run())
    await asyncio.sleep(0.05)
    task.cancel()
    cancelled_cleanly = True
    detail = ""
    try:
        await task
        cancelled_cleanly, detail = False, "run() returned normally after cancel()"
    except asyncio.CancelledError:
        detail = "CancelledError propagated"
    except Exception as exc:  # any other escape is a regression
        cancelled_cleanly, detail = False, f"{type(exc).__name__}: {exc}"
    checks = [
        check("cancellation_propagates", cancelled_cleanly, detail),
        *graders.cancellation_checks(record),
    ]
    return CaseResult(
        case_id="D-CANCEL",
        observed={"status": record.status},
        checks=checks,
        latency_ms=0.0,
    )


# -- registry (Gate 2 runner consumes this) --------------------------------------

CASES: dict[str, Callable[..., Awaitable[CaseResult]]] = {
    "D-SEL": case_selection_arguments,
    "D-EV": case_evidence_coverage,
    "D-GROUND": case_grounding,
    "D-GAP": case_insufficient_evidence,
    "D-FAIL": case_tool_failure_recovery,
    "D-CIRCUIT": case_circuit_open,
    "D-BUDGET": case_budget_termination,
    "D-APPROVE": case_proposal_lifecycle,
    "D-CANCEL": case_cancellation,
}
