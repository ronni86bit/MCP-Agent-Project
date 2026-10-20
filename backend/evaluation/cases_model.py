"""Model-dependent evaluation cases (Phase 9 Gate 3).

M-SEL — the existing six-probe selection suite: rows come verbatim from
`backend/agent/smoke.py::run_selection_probes` and are graded with the exact
four predicates of `tests/evaluation/test_model_smoke.py` (via the Gate 1
graders, threshold included). One result row for the case, with per-probe
checks.

M-CANON — the canonical investigation over the real workflow: the existing
`test_canonical_smoke.py` assertion set (Gate 1 `canonical_checks`) PLUS the
approved fixture-grounded numeric cross-check, which mirrors the thresholds of
`tests/integration/test_storyline.py` (decline delta_pct <= -20; worst channel
`mobile`) applied to the evidence the run actually collected. Existing tests
are not modified.

Every case wraps its LLM in `RecordingLLM` (timing/usage only), keeps action
mode disabled, and classifies its own failure with the conservative
classifier. Real write actions are impossible: action mode is off and no
approval path is exercised.
"""

from __future__ import annotations

import asyncio
import time

from backend.agent.state import InvestigationRecord
from backend.config import Settings
from backend.evaluation import graders
from backend.evaluation.failure_classification import (
    INFRA_ERROR_CLASSES,
    FailureEvidence,
    classify_failure,
)
from backend.evaluation.recording import RecordingLLM
from backend.evaluation.schema import CaseResult, CheckResult, check

QUESTION = "Why did today's sales drop?"
CANONICAL_SERVERS = frozenset({"postgres-mcp", "monitoring-mcp", "github-mcp"})

# Mirrored 1:1 from tests/integration/test_storyline.py
# (test_canonical_question_is_answerable_through_mcp asserts delta_pct <= -20).
CANONICAL_DECLINE_THRESHOLD_PCT = -20.0


def _budget_exhausted(record: InvestigationRecord) -> bool:
    return any("budget" in w.lower() for w in record.warnings)


def fixture_truth_checks(record: InvestigationRecord) -> list[CheckResult]:
    """Fixture-grounded numeric cross-check (the M-CANON addition). Compares
    the compare_sales evidence the run collected against the seeded scenario's
    known decline, using thresholds mirrored from the existing storyline test.
    Claim-level numeric grounding stays the verifier's job; this checks the
    collected EVIDENCE against fixture truth."""
    compares = [e.summary for e in record.evidence if e.tool_name == "compare_sales"]
    checks: list[CheckResult] = []

    deltas = [s["delta_pct"] for s in compares if s.get("delta_pct") is not None]
    if deltas:
        checks.append(check(
            "fixture_decline_present",
            min(deltas) <= CANONICAL_DECLINE_THRESHOLD_PCT,
            f"delta_pct values={deltas} threshold<={CANONICAL_DECLINE_THRESHOLD_PCT} "
            "(mirrors tests/integration/test_storyline.py)",
        ))
    else:
        checks.append(check(
            "fixture_decline_present", False,
            "no comparable decline evidence collected for the canonical question",
        ))

    entries = [b for s in compares for b in (s.get("breakdown") or [])
               if b.get("delta_pct") is not None]
    if entries:
        worst = min(entries, key=lambda b: b["delta_pct"])
        checks.append(check(
            "worst_channel_is_mobile",
            worst.get("group") == "mobile",
            f"worst channel={worst.get('group')!r} delta_pct={worst.get('delta_pct')} "
            "(fixture truth: mobile is the affected channel)",
        ))
    else:
        checks.append(check(
            "worst_channel_is_mobile", True,
            "no channel breakdown collected (advisory pass; fixture_decline_present "
            "still applies)",
        ))
    return checks


async def case_msel(*, llm, managers, settings: Settings | None = None) -> CaseResult:
    """M-SEL: the existing six-probe suite, row source and predicates
    unchanged; one result row with per-probe checks and usage totals."""
    from backend.agent.smoke import DEFAULT_PROBES, run_selection_probes

    recorder = RecordingLLM(llm)
    started = time.perf_counter()
    harness_error: BaseException | None = None
    rows: list[dict] = []
    try:
        rows = await run_selection_probes(recorder, managers, DEFAULT_PROBES)
    except Exception as exc:  # manager/harness-level failure, not a probe result
        harness_error = exc
    latency_ms = (time.perf_counter() - started) * 1000

    checks: list[CheckResult] = []
    for i, row in enumerate(rows):
        for c in graders.selection_checks(row):
            checks.append(c.model_copy(update={"name": f"probe_{i}_{c.name}"}))

    errors = [row.get("error") for row in rows if row.get("error")]
    case_passed = bool(checks) and all(c.passed for c in checks)
    # infrastructure health: verified only when every probe ran without an
    # invocation error; with probe errors the layer is deliberately not
    # established (conservative — could be a dependency failure)
    if harness_error is None and len(rows) == len(DEFAULT_PROBES) and not errors:
        healthy: bool | None = True
    else:
        healthy = None
    evidence = FailureEvidence(
        case_passed=case_passed,
        terminal=harness_error is None,
        infra_verified_healthy=healthy,
        harness_error_class=type(harness_error).__name__ if harness_error else None,
        detail={"probes_completed": len(rows), "probe_errors": len(errors)},
    )
    verdict = classify_failure(evidence)
    return CaseResult(
        case_id="M-SEL",
        suite="model",
        kind="model",
        observed={"probes": rows, "totals": recorder.totals()},
        checks=checks,
        error_class=type(harness_error).__name__ if harness_error else None,
        latency_ms=round(latency_ms, 1),
        failure_layer=verdict.layer,
        failure_diagnostics=verdict.diagnostics,
    )


async def case_mcanon(
    *, llm, settings: Settings | None = None,
    manager_factory=None,  # None = real stdio factories (real-model runs)
) -> CaseResult:
    """M-CANON: the canonical investigation over the real workflow."""
    from backend.agent.service import AgentService

    settings = settings or Settings(max_investigation_seconds=1200, max_llm_calls=16)
    recorder = RecordingLLM(llm)
    if manager_factory is not None:
        service = AgentService(settings, llm=recorder, manager_factory=manager_factory)
    else:
        service = AgentService(settings, llm=recorder)
    started = time.perf_counter()
    harness_error: BaseException | None = None
    record: InvestigationRecord | None = None
    try:
        record = service.start_investigation(QUESTION)
        deadline = time.monotonic() + settings.max_investigation_seconds + 60
        while record.status == "running" and time.monotonic() < deadline:
            await asyncio.sleep(2)
    except Exception as exc:
        harness_error = exc
    finally:
        await service.shutdown()
    latency_ms = (time.perf_counter() - started) * 1000

    checks: list[CheckResult] = [check(
        "action_mode_disabled", settings.action_mode_enabled is False,
        "benchmark runs never enable the write path",
    )]
    terminal = False
    if record is not None and harness_error is None:
        checks.extend(graders.canonical_checks(record, expected_servers=CANONICAL_SERVERS))
        checks.extend(fixture_truth_checks(record))
        terminal = record.status in ("complete", "failed")
    case_passed = bool(checks) and all(c.passed for c in checks)

    infra_classes: tuple[str, ...] = ()
    if record is not None:
        infra_classes = tuple({
            o.error_class for o in record.tool_calls if o.error_class
        } & INFRA_ERROR_CLASSES)
    provider_errors = [r.error_class for r in recorder.records if not r.ok and r.error_class]
    rate_limit = any(
        "rate limit" in (o.error or "").lower()
        for o in (record.tool_calls if record is not None else []) if o.error
    )
    healthy: bool | None = None
    if terminal and not infra_classes and not provider_errors and not rate_limit:
        healthy = True

    evidence = FailureEvidence(
        case_passed=case_passed,
        terminal=terminal,
        budget_exhausted=_budget_exhausted(record) if record is not None else False,
        infra_error_classes=infra_classes,
        rate_limit_signature_seen=rate_limit,
        infra_verified_healthy=healthy,
        harness_error_class=type(harness_error).__name__ if harness_error else None,
        detail={"failed_checks": [c.name for c in checks if not c.passed]},
    )
    verdict = classify_failure(evidence)
    return CaseResult(
        case_id="M-CANON",
        suite="model",
        kind="model",
        observed={
            "status": record.status if record is not None else None,
            "evidence_servers": sorted(graders.evidence_servers_of(record))
            if record is not None else [],
            "totals": recorder.totals(),
            "failure_reason": record.failure_reason if record is not None else None,
        },
        checks=checks,
        error_class=type(harness_error).__name__ if harness_error else None,
        latency_ms=round(latency_ms, 1),
        failure_layer=verdict.layer,
        failure_diagnostics=verdict.diagnostics,
    )
