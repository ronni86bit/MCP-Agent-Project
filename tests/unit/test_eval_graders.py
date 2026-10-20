"""Phase 9 Gate 1: grader parity and purity tests.

The graders reuse existing evaluation methodology. These tests PROVE the
reuse: `selection_passed` and `canonical_checks` are compared against verbatim
reference transcriptions of the existing tests' assertions, and `window_days`
against the original helper imported from `tests/evaluation/test_canonical_smoke.py`.
No model, no network, no Postgres.
"""

import uuid

from backend.agent.models import (
    Claim,
    Confidence,
    InvestigationReport,
    Plan,
    PlanStep,
    ToolCallObservation,
    VerificationResult,
)
from backend.agent.state import InvestigationRecord
from backend.evaluation import graders
from backend.evaluation.graders import canonical_checks, selection_passed, window_days
from tests.evaluation.test_canonical_smoke import _window_days as ORIGINAL_window_days

EXPECTED_SERVERS = frozenset({"postgres-mcp", "monitoring-mcp", "github-mcp"})


class _Unset:
    """Sentinel: lets a test pass verification=None explicitly."""


_UNSET = _Unset()


# -- verbatim reference transcriptions (the parity anchors) ---------------------


def _ref_selection(row) -> bool:
    """Verbatim reference of tests/evaluation/test_model_smoke.py's assertions."""
    return (
        row["selected_tool"] == row["expected_tool"]
        and row["arguments_valid"]
        and row["invocation_ok"]
        and row["selection_ms"] < 180_000
    )


def _ref_window_days(observation_arguments):
    """Verbatim copy of test_canonical_smoke._window_days (second anchor so a
    future edit of the original cannot silently break the comparison)."""
    from datetime import date

    try:
        a = (date.fromisoformat(observation_arguments["window_a_end"])
             - date.fromisoformat(observation_arguments["window_a_start"])).days + 1
        b = (date.fromisoformat(observation_arguments["window_b_end"])
             - date.fromisoformat(observation_arguments["window_b_start"])).days + 1
    except (KeyError, TypeError, ValueError):
        return None
    return a, b


def _ref_canonical(record, expected_servers=EXPECTED_SERVERS, min_evidence=3) -> bool:
    """Verbatim reference of test_canonical_smoke.py's assertion list."""
    if record.status != "complete":
        return False
    if not (record.plan and len(record.plan.steps) >= 1):
        return False
    if not len(record.evidence_ids) >= 3:
        return False
    evidence_servers = {obs.tool.split("__")[0] for obs in record.tool_calls if obs.status == "ok"}
    if not evidence_servers >= set(expected_servers):
        return False
    if record.report is None:
        return False
    if not len(record.report.conclusion) > 30:
        return False
    if not record.report.claims:
        return False
    for claim in record.report.claims:
        if not claim.evidence_ids:
            return False
    if record.verification is None:
        return False
    for obs in record.tool_calls:
        if obs.tool.endswith("__compare_sales") and obs.status == "ok":
            windows = _ref_window_days(obs.arguments)
            if not (windows is not None and windows[0] == windows[1]):
                return False
    return True


# -- synthetic record builders ---------------------------------------------------


def _tool_obs(tool: str, *, status: str = "ok", arguments: dict | None = None,
              error_class: str | None = None, evidence_id: str | None = "ev-x") -> ToolCallObservation:
    return ToolCallObservation(
        step=1, tool=tool, arguments=arguments or {}, status=status,
        error_class=error_class, evidence_id=evidence_id,
    )


def _record(*, status: str = "complete", steps: int = 1, evidence: int = 3,
            servers: tuple[str, ...] = ("postgres-mcp", "monitoring-mcp", "github-mcp"),
            conclusion: str = "Revenue fell during the incident window across channels.",
            claims: list[Claim] | None = None,
            verification: VerificationResult | None | _Unset = _UNSET,
            extra_obs: list[ToolCallObservation] | None = None) -> InvestigationRecord:
    obs = [_tool_obs(f"{s}__tool_{i}") for i, s in enumerate(servers)]
    obs.extend(extra_obs or [])
    if isinstance(verification, _Unset):
        verification = VerificationResult(ok=True)
    record = InvestigationRecord(
        investigation_id=f"inv-{uuid.uuid4().hex[:8]}",
        request_id=f"req-{uuid.uuid4().hex[:8]}",
        question="Why did today's sales drop?",
        status=status,
        plan=Plan(
            goal="investigate",
            steps=[PlanStep(id=i + 1, action=f"step {i + 1}", evidence_needed="e")
                   for i in range(steps)],
            expected_evidence=["e"], completion_criteria="done",
        ),
        report=InvestigationReport(
            conclusion=conclusion,
            claims=claims if claims is not None else [
                Claim(text="Revenue fell during the incident.", evidence_ids=["ev-0001"]),
            ],
            confidence=Confidence.medium,
            confidence_rationale="grounded",
        ),
        verification=verification,
    )
    record.evidence_ids = [f"ev-{i:04d}" for i in range(1, evidence + 1)]
    record.tool_calls = obs
    return record


# -- parity: selection predicates ------------------------------------------------


def test_selection_grader_matches_reference_on_pass_matrix():
    base = {
        "question": "q", "expected_tool": "postgres-mcp__get_daily_sales",
        "selected_tool": "postgres-mcp__get_daily_sales",
        "arguments_valid": True, "invocation_ok": True, "selection_ms": 1000.0,
    }
    assert selection_passed(base) == _ref_selection(base) is True
    assert [c.name for c in graders.selection_checks(base)] == [
        "selected_tool_matches_expected", "arguments_valid",
        "invocation_ok", "selection_latency_within_budget",
    ]


def test_selection_grader_matches_reference_on_each_violation():
    base = {
        "question": "q", "expected_tool": "postgres-mcp__get_daily_sales",
        "selected_tool": "postgres-mcp__get_daily_sales",
        "arguments_valid": True, "invocation_ok": True, "selection_ms": 1000.0,
    }
    violations = [
        {"selected_tool": "postgres-mcp__compare_sales"},          # wrong tool
        {"arguments_valid": False},                                 # schema invalid
        {"invocation_ok": False},                                   # execution failed
        {"selection_ms": 180_000.0},                                # exactly at budget: fails (<)
        {"selection_ms": 180_001.0},                                # over budget
    ]
    for violation in violations:
        row = {**base, **violation}
        assert selection_passed(row) == _ref_selection(row) is False, violation


# -- parity: window_days ----------------------------------------------------------


def test_window_days_matches_original_helper():
    matrices = [
        {"window_a_start": "2026-10-03", "window_a_end": "2026-10-05",
         "window_b_start": "2026-09-30", "window_b_end": "2026-10-02"},   # equal 3-day
        {"window_a_start": "2026-10-03", "window_a_end": "2026-10-05",
         "window_b_start": "2026-10-04", "window_b_end": "2026-10-05"},   # unequal
        {"window_a_start": "2026-10-03", "window_a_end": "2026-10-03",
         "window_b_start": "2026-10-03", "window_b_end": "2026-10-03"},   # single-day
        {},                                                               # missing keys
        {"window_a_start": "not-a-date", "window_a_end": "2026-10-05",
         "window_b_start": "2026-10-03", "window_b_end": "2026-10-05"},   # invalid
    ]
    for args in matrices:
        assert window_days(args) == ORIGINAL_window_days(args) == _ref_window_days(args), args


# -- parity: canonical checks ------------------------------------------------------


def test_canonical_checks_match_reference_on_passing_record():
    record = _record(extra_obs=[_tool_obs(
        "postgres-mcp__compare_sales",
        arguments={"window_a_start": "2026-10-03", "window_a_end": "2026-10-05",
                   "window_b_start": "2026-09-30", "window_b_end": "2026-10-02"},
    )])
    assert _ref_canonical(record) is True
    checks = canonical_checks(record, expected_servers=EXPECTED_SERVERS)
    assert all(c.passed for c in checks)
    assert _ref_canonical(record) == all(c.passed for c in checks)


def test_canonical_checks_match_reference_on_each_violation():
    ok_compare = {"window_a_start": "2026-10-03", "window_a_end": "2026-10-05",
                  "window_b_start": "2026-09-30", "window_b_end": "2026-10-02"}
    bad_compare = {"window_a_start": "2026-10-03", "window_a_end": "2026-10-05",
                   "window_b_start": "2026-10-04", "window_b_end": "2026-10-05"}
    malformed_compare = {"window_a_start": "2026-10-03", "window_b_start": "2026-10-04"}
    records = {
        "failed_status": _record(status="failed"),
        "thin_evidence": _record(evidence=2),
        "missing_server": _record(servers=("postgres-mcp", "monitoring-mcp")),
        "short_conclusion": _record(conclusion="too short"),
        "no_claims": _record(claims=[]),
        "uncited_claim": _record(claims=[Claim(text="c", evidence_ids=[])]),
        "no_verification": _record(verification=None),
        "unequal_windows": _record(extra_obs=[_tool_obs(
            "postgres-mcp__compare_sales", arguments=bad_compare)]),
        "malformed_windows": _record(extra_obs=[_tool_obs(
            "postgres-mcp__compare_sales", status="ok", arguments=malformed_compare)]),
    }
    # the no-plan violation: strip the plan the helper builds
    no_plan = _record()
    no_plan.plan = None
    records["no_plan"] = no_plan
    for label, record in records.items():
        reference = _ref_canonical(record)
        derived = all(c.passed for c in canonical_checks(record, expected_servers=EXPECTED_SERVERS))
        assert reference is False, f"{label} unexpectedly passes the reference"
        assert derived is False, f"{label} unexpectedly passes the grader"
        assert reference == derived, label
    # and the equal-window control still passes both
    control = _record(extra_obs=[_tool_obs("postgres-mcp__compare_sales", arguments=ok_compare)])
    assert _ref_canonical(control) == all(
        c.passed for c in canonical_checks(control, expected_servers=EXPECTED_SERVERS)
    )


# -- grader unit behavior (non-parity checks) --------------------------------------


def test_failure_checks_detect_typed_error_and_missing_evidence():
    from backend.agent.models import StepRecord

    record = _record(
        status="failed",
        servers=(),
        extra_obs=[_tool_obs("postgres-mcp__compare_sales", status="error",
                             error_class="ToolExecutionError", evidence_id=None)],
    )
    record.evidence_ids = []
    record.steps = [StepRecord(index=1, plan_version=1, plan_step_id=1,
                               action="compare", status="failed")]
    checks = graders.failure_checks(
        record, failing_tool="postgres-mcp__compare_sales",
        expected_error_class="ToolExecutionError",
    )
    assert all(c.passed for c in checks)

    wrong_class = graders.failure_checks(
        record, failing_tool="postgres-mcp__compare_sales",
        expected_error_class="ToolTimeoutError",
    )
    assert not all(c.passed for c in wrong_class)


def test_budget_checks_boundaries():
    record = _record()
    record.llm_calls = 3
    record.warnings.append("step budget exhausted; remaining plan steps were not executed")
    ok = graders.budget_checks(record, max_llm_calls=4, expect_budget_warning=True,
                               budget_metric_delta={"{'budget_class': 'steps'}": 1.0})
    assert all(c.passed for c in ok)
    over = graders.budget_checks(record, max_llm_calls=2, budget_metric_delta={"x": 0.0})
    assert not all(c.passed for c in over)


def test_circuit_checks_require_warning_gap_and_typed_error():
    record = _record(
        servers=("postgres-mcp",),
        extra_obs=[_tool_obs("monitoring-mcp__get_error_rate", status="error",
                             error_class="CircuitOpenError", evidence_id=None)],
    )
    record.evidence_ids = ["ev-0001"]
    record.warnings.append("source unavailable: monitoring-mcp")
    record.report = record.report.model_copy(update={
        "evidence_gaps": ["source unavailable (no evidence collected): monitoring-mcp"]})
    assert all(c.passed for c in graders.circuit_checks(record, unavailable_server="monitoring-mcp"))
    assert not all(c.passed for c in graders.circuit_checks(record, unavailable_server="github-mcp"))


def test_proposal_checks_enforce_lifecycle_invariants():
    from backend.agent.models import ApprovalRecord
    from backend.evaluation.graders import proposal_checks

    def _model(**kw) -> ApprovalRecord:
        base = dict(
            approval_id="apr-1", investigation_id="inv-1", tool="acknowledge_alert",
            arguments={"alert_id": "ALT-2071", "note": "n"},
            status="proposed", args_hash="h",
        )
        base.update(kw)
        return ApprovalRecord(**base)

    persisted = _model(status="proposed")
    executed = _model(status="executed", decided_by="op-1", executed_by="op-2",
                      verification={"ok": True, "details": "d"})
    assert all(c.passed for c in proposal_checks(persisted=persisted, final=executed))

    not_executed = _model(status="approved", decided_by="op-1", executed_by=None,
                          verification=None)
    assert not all(c.passed for c in proposal_checks(persisted=persisted, final=not_executed))

    tampered = _model(status="executed", decided_by="op-1", executed_by="op-2",
                      verification={"ok": True}, arguments={"alert_id": "ALT-9999", "note": "n"})
    checks = proposal_checks(persisted=persisted, final=tampered)
    assert not all(c.passed for c in checks)
    assert any(c.name == "executed_from_stored_arguments" and not c.passed for c in checks)


def test_cancellation_checks():
    running = _record(status="running")
    running.finished_at = None
    assert all(c.passed for c in graders.cancellation_checks(running))
    finalized = _record(status="complete")
    assert not all(c.passed for c in graders.cancellation_checks(finalized))


# -- purity -------------------------------------------------------------------------


def test_graders_are_pure_and_deterministic():
    record = _record(extra_obs=[_tool_obs(
        "postgres-mcp__compare_sales",
        arguments={"window_a_start": "2026-10-03", "window_a_end": "2026-10-05",
                   "window_b_start": "2026-09-30", "window_b_end": "2026-10-02"},
    )])
    snapshot = record.model_dump(mode="json")
    first = canonical_checks(record, expected_servers=EXPECTED_SERVERS)
    second = canonical_checks(record, expected_servers=EXPECTED_SERVERS)
    assert [c.model_dump() for c in first] == [c.model_dump() for c in second]
    assert record.model_dump(mode="json") == snapshot  # inputs never mutated
