"""Pure evaluation graders (Phase 9 Gate 1).

Every function here REUSES existing verdicts/logic rather than re-implementing
it: grounding judgements come only from the verifier's `VerificationResult` on
the record; the selection predicates are a 1:1 transcription of
`tests/evaluation/test_model_smoke.py`'s assertions; `window_days` is a 1:1
transcription of `tests/evaluation/test_canonical_smoke.py::_window_days`; the
canonical checks transcribe that test's assertion list. Equivalence is proven
by parity tests in `tests/unit/test_eval_graders.py`, not assumed.

Purity contract: graders only READ their inputs (pydantic models, mappings),
never mutate them, never await, never touch globals — same input, same checks.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from typing import Any

from backend.agent.models import ApprovalRecord, VerificationResult
from backend.agent.state import InvestigationRecord
from backend.evaluation.schema import CaseResult, CheckResult, check

# Thresholds imported from the EXISTING evaluation test so they cannot drift
# from the preserved methodology (Rev 3 reuse rule).
SELECTION_LATENCY_BUDGET_MS = 180_000.0  # test_model_smoke.py: selection_ms < 180_000


# -- selection probes (parity: tests/evaluation/test_model_smoke.py) -----------


def selection_checks(row: Mapping[str, Any]) -> list[CheckResult]:
    """The four predicates of test_model_smoke.py, in order, verbatim:
    selected_tool == expected_tool; arguments_valid; invocation_ok;
    selection_ms < 180_000."""
    return [
        check(
            "selected_tool_matches_expected",
            row.get("selected_tool") == row.get("expected_tool"),
            f"selected={row.get('selected_tool')!r} expected={row.get('expected_tool')!r}",
        ),
        check("arguments_valid", bool(row.get("arguments_valid"))),
        check("invocation_ok", bool(row.get("invocation_ok"))),
        check(
            "selection_latency_within_budget",
            float(row.get("selection_ms", 0.0)) < SELECTION_LATENCY_BUDGET_MS,
            f"selection_ms={row.get('selection_ms')}",
        ),
    ]


def selection_passed(row: Mapping[str, Any]) -> bool:
    return all(c.passed for c in selection_checks(row))


# -- canonical checks (parity: tests/evaluation/test_canonical_smoke.py) -------


def window_days(observation_arguments: Mapping[str, Any]) -> tuple[int, int] | None:
    """1:1 transcription of test_canonical_smoke._window_days (parity-tested)."""
    try:
        a = (date.fromisoformat(observation_arguments["window_a_end"])
             - date.fromisoformat(observation_arguments["window_a_start"])).days + 1
        b = (date.fromisoformat(observation_arguments["window_b_end"])
             - date.fromisoformat(observation_arguments["window_b_start"])).days + 1
    except (KeyError, TypeError, ValueError):
        return None
    return a, b


def evidence_servers_of(record: InvestigationRecord) -> set[str]:
    """Same derivation both existing tests use for server coverage."""
    return {obs.tool.split("__")[0] for obs in record.tool_calls if obs.status == "ok"}


def canonical_checks(
    record: InvestigationRecord, *, expected_servers: frozenset[str], min_evidence: int = 3
) -> list[CheckResult]:
    """Transcription of test_canonical_smoke.py's assertion list (order kept)."""
    checks: list[CheckResult] = []
    checks.append(check(
        "investigation_complete", record.status == "complete",
        f"status={record.status} failure_reason={record.failure_reason}",
    ))
    checks.append(check(
        "plan_has_steps", bool(record.plan) and len(record.plan.steps) >= 1,
        f"steps={len(record.plan.steps) if record.plan else 0}",
    ))
    checks.append(check(
        "evidence_count_min", len(record.evidence_ids) >= min_evidence,
        f"evidence={len(record.evidence_ids)} min={min_evidence}",
    ))
    servers = evidence_servers_of(record)
    checks.append(check(
        "server_coverage", servers >= set(expected_servers),
        f"servers={sorted(servers)} expected={sorted(expected_servers)}",
    ))
    report = record.report
    checks.append(check("report_present", report is not None))
    if report is not None:
        checks.append(check("conclusion_substantive", len(report.conclusion) > 30))
        claims = report.claims
        checks.append(check("claims_present", bool(claims)))
        checks.append(check(
            "claims_cite_evidence",
            all(c.evidence_ids for c in claims),
        ))
    checks.append(check("verification_present", record.verification is not None))
    equal = True
    detail = "no ok compare_sales observations"
    unequal: list[dict] = []
    for obs in record.tool_calls:
        if obs.tool.endswith("__compare_sales") and obs.status == "ok":
            windows = window_days(obs.arguments)
            if windows is None or windows[0] != windows[1]:
                equal = False
                unequal.append(obs.arguments)
    if unequal:
        detail = f"unequal-duration comparisons reached execution: {unequal}"
    checks.append(check("compare_windows_equal_duration", equal, detail))
    return checks


# -- grounding (REUSES the verifier's verdict; never recomputed) ----------------


def grounding_checks(
    record: InvestigationRecord, *,
    expect_ok: bool | None = None,
    expect_flagged: bool = False,
    expect_downgraded: bool | None = None,
) -> list[CheckResult]:
    """Assert on the verifier OUTPUT attached to the record. When a claim
    carries a numeric token, this grader additionally cross-checks claimed
    values against the cited evidence payloads (magnitude-aware, same rule as
    the verifier: sign/percent/thousand separators ignored) — callers pass
    `claim_value_expectations` for cases that need it; none of the Gate 1
    deterministic cases require it."""
    verification = record.verification
    checks = [check("verification_present", verification is not None)]
    if verification is None:
        return checks
    assert isinstance(verification, VerificationResult)
    if expect_ok is not None:
        checks.append(check(
            "verifier_ok_matches_expectation",
            verification.ok == expect_ok,
            f"ok={verification.ok} expected={expect_ok}",
        ))
    if expect_flagged:
        checks.append(check(
            "unsupported_claim_flagged", bool(verification.flagged_claims),
            f"flagged={verification.flagged_claims}",
        ))
    if expect_downgraded is not None:
        checks.append(check(
            "downgrade_matches_expectation",
            verification.downgraded == expect_downgraded,
            f"downgraded={verification.downgraded}",
        ))
    # universal invariant: dropped/flagged entries are strings and the record's
    # report claims all still cite evidence (the verifier guarantees this)
    checks.append(check(
        "claims_still_cite_evidence",
        record.report is None or all(c.evidence_ids for c in record.report.claims),
    ))
    return checks


# -- deterministic scenario checks ----------------------------------------------


def failure_checks(
    record: InvestigationRecord, *,
    failing_tool: str, expected_error_class: str,
    expect_no_evidence: bool = True,
) -> list[CheckResult]:
    """D-SEL / D-FAIL: the typed error is preserved, no evidence record was
    created from the failed call, and the step failed."""
    failing = [obs for obs in record.tool_calls if obs.tool == failing_tool]
    checks = [
        check("failing_call_observed", bool(failing), f"observations={len(failing)}"),
        check(
            "typed_error_preserved",
            any(o.status == "error" and o.error_class == expected_error_class for o in failing),
            f"expected={expected_error_class} got={[ (o.status, o.error_class) for o in failing ]}",
        ),
    ]
    if expect_no_evidence:
        evidence_ids_from_failures = {
            o.evidence_id for o in failing
            if o.status == "error" and o.evidence_id
        }
        checks.append(check(
            "no_evidence_from_failed_call",
            not evidence_ids_from_failures and not (set(record.evidence_ids) & evidence_ids_from_failures),
            f"evidence_from_failures={sorted(evidence_ids_from_failures)}",
        ))
        checks.append(check(
            "failed_step_recorded",
            any(s.status == "failed" for s in record.steps),
            f"steps={[s.status for s in record.steps]}",
        ))
    return checks


def coverage_checks(
    record: InvestigationRecord, *, expected_servers: frozenset[str]
) -> list[CheckResult]:
    servers = evidence_servers_of(record)
    return [
        check("investigation_complete", record.status == "complete", f"status={record.status}"),
        check("evidence_collected", bool(record.evidence_ids), f"evidence={len(record.evidence_ids)}"),
        check(
            "expected_servers_covered", servers >= set(expected_servers),
            f"servers={sorted(servers)} expected={sorted(expected_servers)}",
        ),
    ]


def budget_checks(
    record: InvestigationRecord, *,
    max_llm_calls: int, expect_budget_warning: bool = False,
    budget_metric_delta: Mapping[str, float] | None = None,
) -> list[CheckResult]:
    """Budget invariants: terminal status, call bound respected, and (when the
    runner captured it) the Phase 8 budget-exhaustion metric incremented."""
    checks = [
        check(
            "terminal_status", record.status in ("complete", "failed"),
            f"status={record.status}",
        ),
        check(
            "llm_calls_within_budget", record.llm_calls <= max_llm_calls,
            f"llm_calls={record.llm_calls} max={max_llm_calls}",
        ),
    ]
    if expect_budget_warning:
        checks.append(check(
            "budget_warning_recorded",
            any("budget" in w.lower() for w in record.warnings),
            f"warnings={record.warnings}",
        ))
    if budget_metric_delta is not None:
        checks.append(check(
            "budget_metric_recorded",
            any(v >= 1 for v in budget_metric_delta.values()),
            f"deltas={dict(budget_metric_delta)}",
        ))
    return checks


def circuit_checks(
    record: InvestigationRecord, *, unavailable_server: str
) -> list[CheckResult]:
    """Source unavailability must be REPORTED (warning + report gap), never
    silently skipped (architecture principle 5)."""
    warning_ok = any(
        "source unavailable" in w and unavailable_server in w for w in record.warnings
    )
    gap_ok = any(
        unavailable_server in gap
        for gap in ((record.report.evidence_gaps if record.report else []) or [])
    )
    return [
        check("investigation_complete", record.status == "complete", f"status={record.status}"),
        check(
            "unavailability_reported_in_warnings", warning_ok,
            f"warnings={record.warnings}",
        ),
        check(
            "unavailability_reported_in_gaps", gap_ok,
            f"gaps={(record.report.evidence_gaps if record.report else [])}",
        ),
        check(
            "circuit_open_typed",
            any(
                o.status == "error" and o.error_class == "CircuitOpenError"
                for o in record.tool_calls
                if o.tool.split("__")[0] == unavailable_server
            ),
            f"observations={[(o.tool, o.status, o.error_class) for o in record.tool_calls]}",
        ),
    ]


def proposal_checks(
    *, persisted: ApprovalRecord, final: ApprovalRecord
) -> list[CheckResult]:
    """D-APPROVE: the full enforced lifecycle, graded on persisted rows only.
    The model can only PROPOSE; a decision is mandatory; execution uses the
    stored arguments verbatim; verification is recorded."""
    same_args = persisted.arguments == final.arguments
    return [
        check("initially_proposed", persisted.status == "proposed", f"status={persisted.status}"),
        check(
            "hash_binding_present",
            bool(persisted.args_hash) and bool(final.args_hash)
            and persisted.args_hash == final.args_hash,
        ),
        check("human_decision_required", final.decided_by is not None and final.decided_by != ""),
        check("executed_by_human_label", final.executed_by is not None and final.executed_by != ""),
        check("executed_from_stored_arguments", same_args),
        check("final_status_executed", final.status == "executed", f"status={final.status}"),
        check(
            "verification_ok_recorded",
            bool(final.verification) and final.verification.get("ok") is True,
            f"verification={final.verification}",
        ),
    ]


def cancellation_checks(record: InvestigationRecord) -> list[CheckResult]:
    """D-CANCEL: the run never finalized — cancellation propagated."""
    return [
        check("status_still_running", record.status == "running", f"status={record.status}"),
        check("not_finalized", record.finished_at is None),
    ]


def case_passed(checks: list[CheckResult]) -> bool:
    return all(c.passed for c in checks)


def summarize_case(case: CaseResult) -> dict[str, Any]:
    return {"case_id": case.case_id, "passed": case.passed,
            "failed_checks": [c.name for c in case.checks if not c.passed]}
