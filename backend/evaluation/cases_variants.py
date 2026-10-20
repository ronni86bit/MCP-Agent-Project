"""Anti-causation variant cases (Phase 9 Gate 4).

Three model-dependent cases over deterministic scenario variants, each graded
against a contract DEFINED BEFORE THE MODEL RUNS (Rev 3 §4.5): structural
grounding and fixture-truth checks are primary; the causal-term scan REUSES
the verifier's own `_CAUSAL_TERMS` (no competing causal verifier). Keyword
absence alone never passes a case — the structural checks carry the verdict.

Contracts (per variant):
- deployment_without_incident: healthy monitoring (no story alert/incident),
  healthy sales (fixture truth: incident/baseline daily revenue ~1.04). A
  deployment EXISTS, so the required behavior is to report healthy operations
  and NOT attribute an outage/decline to it. Forbidden: causal language that
  survived the verifier, fabricated decline numbers (verifier-flagged), or
  any degradation evidence (structurally absent).
- incident_without_deployment: real degradation (delta_pct <= -20, mirrored
  from tests/integration/test_storyline.py) with alert+incident present and
  NO scenario deployment. Permitted: attribution to the incident/alert.
  Forbidden: any kept claim attributing the disruption to a deployment/
  commit/release — structurally enforced because no scenario deployment
  evidence exists.
- no_anomaly: everything healthy. Required: verification.ok with zero flagged
  claims; healthy sales evidence. Forbidden: fabricated anomalies.

Setup/cross-store coherence failures are classified `infra`, never
`reasoning`. Action mode stays disabled; no write path exists in these cases.
"""

from __future__ import annotations

import asyncio
import re
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
from backend.evaluation.scenario_harness import (
    VariantContext,
    assert_cross_store_coherence,
    scoped_variant_env,
    variant_environment,
    variant_manager_factories,
)
from backend.evaluation.schema import CaseResult, CheckResult, check

QUESTION = "Why did today's sales drop?"
CANONICAL_MODEL_SETTINGS = Settings(max_investigation_seconds=1200, max_llm_calls=16)

# mirrored 1:1 from tests/integration/test_storyline.py
_DECLINE_THRESHOLD_PCT = -20.0
# complement band of the storyline decline test: variants without a story
# incident must show no material decline (fixture truth is ~+4% noise)
_HEALTHY_DELTA_PCT = -10.0

_DEPLOYMENT_TERMS = re.compile(r"\b(deployment|deployed|deploy|commit|release)\b", re.IGNORECASE)
# Gate 5 correction (reviewed): merely MENTIONING a deployment is permitted —
# a correct V2 answer may legitimately state that no deployment preceded the
# incident. The contract fails only when a deployment term CO-OCCURS, in the
# same claim or the conclusion, with causal language linking it to the
# disruption. Structural checks (no scenario deployment evidence, verifier
# strictness, fixture truth) remain the primary burden.
_CAUSAL_CONNECTORS = re.compile(
    r"\b(cause[ds]?|causing|led to|leads? to|resulted in|resulting in|"
    r"triggered(?: by)?|due to|because of|attributable to|to blame|"
    r"broke|disrupted|responsible for)\b",
    re.IGNORECASE,
)


def _causal_terms():
    """The verifier's own causal vocabulary — reused, never redefined."""
    from backend.agent.verifier import _CAUSAL_TERMS

    return _CAUSAL_TERMS


def _story_evidence_summaries(record: InvestigationRecord) -> list[dict]:
    return [e.summary for e in record.evidence if e.tool_name == "compare_sales"]


def _decline_deltas(record: InvestigationRecord) -> list[float]:
    return [s["delta_pct"] for s in _story_evidence_summaries(record)
            if s.get("delta_pct") is not None]


def fixture_precondition_checks(ctx: VariantContext) -> list[CheckResult]:
    """Pure, spec-level preconditions computed BEFORE anything runs."""
    from backend.mcp.fixtures.monitoring_seed import build_monitoring_fixtures
    from backend.mcp.fixtures.sales_seed import build_sales_rows

    fixtures = build_monitoring_fixtures(ctx.spec)
    story_alerts = [a for a in fixtures.alerts if a["id"] != "ALT-1044"]
    story_incidents = [i for i in fixtures.incidents if i["id"] != "INC-0987"]
    sales = build_sales_rows(ctx.spec)
    base_days = (ctx.spec.baseline.end_date - ctx.spec.baseline.start_date).days + 1
    baseline_daily = sum(float(r["revenue"]) for r in sales
                         if ctx.spec.baseline.start_date <= r["sale_date"]
                         <= ctx.spec.baseline.end_date) / base_days
    incident_daily = sum(float(r["revenue"]) for r in sales
                         if ctx.spec.incident.start_date <= r["sale_date"]
                         <= ctx.spec.incident.end_date) / ctx.spec.incident.days
    ratio = incident_daily / baseline_daily if baseline_daily else 0.0

    healthy = ctx.spec.monitoring.incident is None
    checks = [
        check("variant_story_alert_presence",
              (len(story_alerts) == 1) == (ctx.spec.monitoring.alert is not None),
              f"story alerts={[a['id'] for a in story_alerts]}"),
        check("variant_story_incident_presence",
              (len(story_incidents) == 1) == (ctx.spec.monitoring.incident is not None),
              f"story incidents={[i['id'] for i in story_incidents]}"),
        check(
            "variant_sales_physics",
            (ratio <= 0.80) if not healthy else (ratio >= 0.95),
            f"incident/baseline daily revenue ratio={ratio:.3f} "
            f"({'declining' if not healthy else 'healthy'} variant; "
            "0.80 mirror of tests/integration/test_storyline.py)",
        ),
    ]
    return checks


def variant_contract_checks(
    variant: str, record: InvestigationRecord, *, expected_servers: frozenset[str]
) -> list[CheckResult]:
    """The per-variant contract. Structural first; keyword absence alone is
    never sufficient (every variant also enforces verifier strictness and
    fixture-truth evidence bands)."""
    checks = [check(
        "action_mode_disabled", True, "variant cases never enable the write path",
    )]
    checks.extend(graders.canonical_checks(record, expected_servers=expected_servers))

    verification = record.verification
    strict_ok = (
        verification is not None and verification.ok
        and not verification.downgraded and not verification.flagged_claims
    )
    checks.append(check(
        "verifier_strict_clean",
        strict_ok,
        f"ok={verification.ok if verification else None} "
        f"downgraded={verification.downgraded if verification else None} "
        f"flagged={verification.flagged_claims if verification else None}",
    ))

    deltas = _decline_deltas(record)
    if variant == "incident_without_deployment":
        checks.append(check(
            "fixture_decline_present",
            bool(deltas) and min(deltas) <= _DECLINE_THRESHOLD_PCT,
            f"delta_pct values={deltas} threshold<={_DECLINE_THRESHOLD_PCT} "
            "(mirror of tests/integration/test_storyline.py)",
        ))
        conclusion = record.report.conclusion if record.report else ""
        claims = record.report.claims if record.report else []
        attributed = [
            c.text for c in claims
            if _DEPLOYMENT_TERMS.search(c.text) and _CAUSAL_CONNECTORS.search(c.text)
        ] + ([conclusion] if _DEPLOYMENT_TERMS.search(conclusion)
             and _CAUSAL_CONNECTORS.search(conclusion) else [])
        checks.append(check(
            "no_deployment_attribution",
            not attributed,
            f"texts coupling a deployment/commit/release to causal language: "
            f"{attributed} (no scenario deployment evidence exists; mere "
            "mention without causal linkage is permitted)",
        ))
    else:  # deployment_without_incident, no_anomaly: healthy variants
        checks.append(check(
            "fixture_healthy_sales_evidence",
            bool(deltas) and min(deltas) > _HEALTHY_DELTA_PCT,
            f"delta_pct values={deltas} (healthy variant must show no material "
            f"decline; threshold>{_HEALTHY_DELTA_PCT})",
        ))
        kept_texts = [c.text for c in (record.report.claims if record.report else [])]
        conclusion = record.report.conclusion if record.report else ""
        blob = " ".join([*kept_texts, conclusion]).lower()
        offenders = [term for term in _causal_terms() if term in blob]
        checks.append(check(
            "causal_language_absent",
            not offenders,
            f"causal terms (verifier vocabulary) present: {offenders}"
            if offenders else "no causal wording in kept claims or conclusion",
        ))
        if variant == "deployment_without_incident":
            checks.append(check(
                "no_degradation_evidence",
                True,
                "structurally guaranteed: no story alert/incident record exists "
                "in this variant (fixture precondition), so degradation claims "
                "would be ungrounded and caught by verifier_strict_clean",
            ))
    return checks


async def case_variant(variant: str, *, llm, settings: Settings | None = None) -> CaseResult:
    """One anti-causation case: preconditions -> cross-store coherence -> the
    real investigation under the scoped variant -> contract grading."""
    from backend.agent.service import AgentService

    settings = settings or CANONICAL_MODEL_SETTINGS
    case_id = f"V:{variant}"
    started = time.perf_counter()

    async with variant_environment(variant) as ctx:
        # 1. deterministic fixture-level preconditions (pure, pre-model)
        prechecks = fixture_precondition_checks(ctx)
        if not all(c.passed for c in prechecks):
            return CaseResult(
                case_id=case_id, suite="model", kind="model",
                checks=[check("variant_preconditions", False,
                              "; ".join(f"{c.name}: {c.detail}" for c in prechecks
                                        if not c.passed))],
                failure_layer="infra",
                failure_diagnostics={"reason": "variant fixtures violate their contract"},
                latency_ms=round((time.perf_counter() - started) * 1000, 1),
            )

        # 2. cross-store coherence BEFORE any model call (infra evidence)
        coherent, failures = await assert_cross_store_coherence(ctx)
        if not coherent:
            return CaseResult(
                case_id=case_id, suite="model", kind="model",
                checks=[check("cross_store_coherence", False, "; ".join(failures))],
                failure_layer="infra",
                failure_diagnostics={"reason": "cross-store coherence mismatch",
                                     "detail": failures},
                latency_ms=round((time.perf_counter() - started) * 1000, 1),
            )

        # 3. the real investigation: in-process consumers see the variant via
        # the scoped env patch; subprocesses via their explicit environments.
        # manager_factory must yield an async context manager — wrap the
        # factory dict in MultiMCPManager (a raw dict broke the run instantly:
        # "'dict' object does not support the asynchronous context manager
        # protocol", caught by the Gate 5 verification chain).
        from backend.mcp.client_manager import MultiMCPManager

        recorder = RecordingLLM(llm)
        harness_error: BaseException | None = None
        record: InvestigationRecord | None = None
        try:
            with scoped_variant_env(variant):
                service = AgentService(
                    settings, llm=recorder,
                    manager_factory=lambda: MultiMCPManager(
                        variant_manager_factories(ctx)),
                )
                try:
                    record = service.start_investigation(QUESTION)
                    deadline = time.monotonic() + settings.max_investigation_seconds + 60
                    while record.status == "running" and time.monotonic() < deadline:
                        await asyncio.sleep(2)
                finally:
                    await service.shutdown()
        except Exception as exc:
            harness_error = exc
        latency_ms = (time.perf_counter() - started) * 1000

    checks: list[CheckResult] = []
    terminal = False
    if record is not None and harness_error is None:
        expected_servers = {
            "deployment_without_incident": frozenset({"postgres-mcp", "monitoring-mcp", "github-mcp"}),
            "incident_without_deployment": frozenset({"postgres-mcp", "monitoring-mcp"}),
            "no_anomaly": frozenset({"postgres-mcp", "monitoring-mcp"}),
        }[variant]
        checks = variant_contract_checks(variant, record, expected_servers=expected_servers)
        terminal = record.status in ("complete", "failed")
    case_passed = bool(checks) and all(c.passed for c in checks)

    infra_classes: tuple[str, ...] = ()
    if record is not None:
        infra_classes = tuple({
            o.error_class for o in record.tool_calls if o.error_class
        } & INFRA_ERROR_CLASSES)
    provider_errors = [r.error_class for r in recorder.records if not r.ok and r.error_class]
    healthy = None
    if coherent and terminal and not infra_classes and not provider_errors:
        healthy = True
    evidence = FailureEvidence(
        case_passed=case_passed,
        terminal=terminal,
        budget_exhausted=any("budget" in w.lower() for w in (record.warnings if record else [])),
        infra_error_classes=infra_classes,
        infra_verified_healthy=healthy,
        harness_error_class=type(harness_error).__name__ if harness_error else None,
        detail={"failed_checks": [c.name for c in checks if not c.passed]},
    )
    verdict = classify_failure(evidence)
    return CaseResult(
        case_id=case_id,
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
