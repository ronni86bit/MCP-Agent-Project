"""Phase 9 Gate 4: scenario-variant loading, isolation, and contracts.

Pure unit tests — no PostgreSQL, no subprocesses, no model. Covers: variant
parsing, canonical invariance (fixture-truth hashes captured before the Gate 4
changes), default behavior with the override unset, no stale caching across
variants, environment restore after success/exception/cancellation, the
explicit-path-wins rule, and the anti-causation grading contracts against
synthetic records.
"""

import asyncio
import hashlib
import json
import os
import uuid

import pytest

from backend.agent.models import (
    Claim,
    Confidence,
    InvestigationReport,
    VerificationResult,
)
from backend.agent.state import InvestigationRecord
from backend.evaluation import cases_variants
from backend.evaluation.scenario_harness import (
    VARIANT_ENV_VAR,
    scoped_variant_env,
    variant_path,
)
from backend.mcp.fixtures.loader import DEFAULT_SCENARIO_PATH, load_scenario
from backend.mcp.fixtures.monitoring_seed import build_monitoring_fixtures
from backend.mcp.fixtures.sales_seed import build_sales_rows

# fixture-truth hashes captured BEFORE the Gate 4 schema/generator changes —
# the canonical scenario's generated output must remain byte-identical
PRE_CHANGE_MONITORING_HASH = "d28ce6964a466539d925963b88f4063dd006fb93a040dee4cc3d40af0d369ce0"
PRE_CHANGE_SALES_HASH = "931af7814939a808b08667b4b19db321acc66a91afa4ffd247f8601af7720149"

_VARIANTS = ("deployment_without_incident", "incident_without_deployment", "no_anomaly")


def _hash(obj) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, default=str).encode()).hexdigest()


# -- requirement 1: variants parse and carry their intended shape -----------------


def test_each_variant_parses_with_its_intended_structure():
    v1 = load_scenario(variant_path("deployment_without_incident"))
    assert v1.name == "checkout_deployment_without_incident_v1"
    assert v1.deployment is not None                    # the correlation target exists
    assert v1.monitoring.alert is None and v1.monitoring.incident is None
    assert v1.impact.channel_multipliers["mobile"] == [1.0, 1.0, 1.0]

    v2 = load_scenario(variant_path("incident_without_deployment"))
    assert v2.name == "checkout_incident_without_deployment_v1"
    assert v2.deployment is None                        # the anti-causation target
    assert v2.monitoring.alert.id == "ALT-2071" and v2.monitoring.incident.id == "INC-1042"
    assert v2.impact.channel_multipliers["mobile"] == [0.50, 0.42, 0.38]

    v3 = load_scenario(variant_path("no_anomaly"))
    assert v3.name == "checkout_no_anomaly_v1"
    assert v3.deployment is None
    assert v3.monitoring.alert is None and v3.monitoring.incident is None
    assert v3.impact.channel_multipliers["mobile"] == [1.0, 1.0, 1.0]

    for variant in _VARIANTS:
        spec = load_scenario(variant_path(variant))
        fixtures = build_monitoring_fixtures(spec)
        story_alerts = [a["id"] for a in fixtures.alerts if a["id"] != "ALT-1044"]
        story_incidents = [i["id"] for i in fixtures.incidents if i["id"] != "INC-0987"]
        assert (story_alerts == ["ALT-2071"]) == (spec.monitoring.alert is not None)
        assert (story_incidents == ["INC-1042"]) == (spec.monitoring.incident is not None)


def test_variant_precondition_checks_satisfy_their_contracts():
    from backend.evaluation.scenario_harness import VariantContext

    for variant in _VARIANTS:
        ctx = VariantContext(variant=variant, spec=load_scenario(variant_path(variant)),
                             spec_path=variant_path(variant), eval_dsn="unused", db_name="unused")
        checks = cases_variants.fixture_precondition_checks(ctx)
        assert all(c.passed for c in checks), (variant, [c.model_dump() for c in checks])


# -- requirement 2: the canonical scenario is unchanged ---------------------------


def test_canonical_scenario_remains_unchanged_and_generates_identical_output():
    spec = load_scenario()  # no env override active in the test process
    assert spec.name == "checkout_incident_v1"
    assert spec.deployment is not None
    assert spec.monitoring.alert.id == "ALT-2071"
    assert spec.monitoring.incident.id == "INC-1042"
    assert DEFAULT_SCENARIO_PATH.name == "scenario.yaml"

    assert _hash(build_monitoring_fixtures(spec).model_dump(mode="json")) \
        == PRE_CHANGE_MONITORING_HASH
    assert _hash(build_sales_rows(spec)) == PRE_CHANGE_SALES_HASH


# -- requirement 3: override unset preserves default behavior ----------------------


def test_override_unset_loads_the_canonical_scenario(monkeypatch):
    monkeypatch.delenv(VARIANT_ENV_VAR, raising=False)
    spec = load_scenario()
    assert spec.name == "checkout_incident_v1"
    assert spec.deployment is not None  # variants would fail this


# -- requirement 4: two variants in one process, no stale caching ------------------


def test_distinct_variant_paths_load_distinct_specs_in_one_process():
    v1 = load_scenario(variant_path("deployment_without_incident"))
    v2 = load_scenario(variant_path("incident_without_deployment"))
    v1_again = load_scenario(variant_path("deployment_without_incident"))
    assert v1.name != v2.name
    assert v1.deployment is not None and v2.deployment is None
    assert v1_again == v1


def test_env_override_selects_variant_and_explicit_path_wins(monkeypatch):
    monkeypatch.setenv(VARIANT_ENV_VAR, str(variant_path("no_anomaly")))
    assert load_scenario().name == "checkout_no_anomaly_v1"
    # an explicit path argument still wins over the environment
    assert load_scenario(variant_path("incident_without_deployment")).name == \
        "checkout_incident_without_deployment_v1"


# -- requirement 5: scoped environment restores after success/exception/cancel -----


def test_scoped_env_restores_after_success_and_exception():
    monkeypatch_free = VARIANT_ENV_VAR not in os.environ
    with scoped_variant_env("no_anomaly"):
        assert os.environ[VARIANT_ENV_VAR] == str(variant_path("no_anomaly"))
        with pytest.raises(RuntimeError):
            with scoped_variant_env("deployment_without_incident"):
                assert os.environ[VARIANT_ENV_VAR].endswith("deployment_without_incident.yaml")
                raise RuntimeError("boom")
        # inner exception restored the outer scope's value
        assert os.environ[VARIANT_ENV_VAR] == str(variant_path("no_anomaly"))
    assert (VARIANT_ENV_VAR in os.environ) == (not monkeypatch_free)


def test_scoped_env_restores_after_cancellation():
    async def _scenario():
        with scoped_variant_env("incident_without_deployment"):
            assert os.environ[VARIANT_ENV_VAR].endswith("incident_without_deployment.yaml")
            await asyncio.sleep(30)

    async def _cancel_it():
        task = asyncio.ensure_future(_scenario())
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(_cancel_it())
    assert VARIANT_ENV_VAR not in os.environ


# -- requirement 8 (contracts): structural grading on synthetic records ------------


def _record(*, claims, conclusion, verification, compare_delta_pct):
    """A canonical-checks-clean record: complete, planned, 3 evidence ids,
    3-server coverage, equal-window compare observation."""
    from backend.agent.models import Plan, PlanStep, ToolCallObservation
    from backend.agent.state import InvestigationRecord

    record = InvestigationRecord(
        investigation_id=f"inv-{uuid.uuid4().hex[:8]}",
        request_id=f"req-{uuid.uuid4().hex[:8]}",
        question="Why did today's sales drop?",
        status="complete",
        plan=Plan(goal="investigate",
                  steps=[PlanStep(id=1, action="compare", evidence_needed="deltas")],
                  expected_evidence=["deltas"], completion_criteria="done"),
        report=InvestigationReport(conclusion=conclusion, claims=claims,
                                   confidence=Confidence.medium,
                                   confidence_rationale="r"),
        verification=verification,
    )
    record.evidence_ids = ["ev-0001", "ev-0002", "ev-0003"]
    record.evidence = [_FakeEvidence(summary={"delta_pct": compare_delta_pct})]
    record.tool_calls = [
        ToolCallObservation(step=1, tool="postgres-mcp__compare_sales", status="ok",
                            arguments={"window_a_start": "2026-09-30",
                                       "window_a_end": "2026-10-02",
                                       "window_b_start": "2026-10-03",
                                       "window_b_end": "2026-10-05"},
                            evidence_id="ev-0001"),
        ToolCallObservation(step=2, tool="monitoring-mcp__get_error_rate", status="ok",
                            arguments={}, evidence_id="ev-0002"),
    ]
    return record


class _FakeEvidence:
    def __init__(self, summary):
        self.evidence_id = "ev-0001"
        self.tool_name = "compare_sales"
        self.server = "postgres-mcp"
        self.summary = summary


def _clean_verification():
    return VerificationResult(ok=True, dropped_claims=[], flagged_claims=[],
                              downgraded=False, grounding_checked=True)


EXPECTED_SERVERS = frozenset({"postgres-mcp", "monitoring-mcp"})


def test_healthy_variant_accepts_grounded_conclusion_and_rejects_causal_attribution():
    healthy = _record(
        claims=[Claim(text="Revenue tracked the baseline in the control window.",
                      evidence_ids=["ev-0001"])],
        conclusion="Operations and sales were healthy throughout the window; "
                   "no anomaly is present in the collected evidence.",
        verification=_clean_verification(),
        compare_delta_pct=4.0,
    )
    checks = cases_variants.variant_contract_checks(
        "no_anomaly", healthy, expected_servers=EXPECTED_SERVERS)
    assert all(c.passed for c in checks), [c.model_dump() for c in checks if not c.passed]

    causal = _record(
        claims=[Claim(text="The deployment certainly caused an outage in the window.",
                      evidence_ids=["ev-0001"])],
        conclusion="The deployment proves an outage degraded sales.",
        verification=_clean_verification(),
        compare_delta_pct=4.0,
    )
    checks = cases_variants.variant_contract_checks(
        "deployment_without_incident", causal, expected_servers=EXPECTED_SERVERS)
    failed = {c.name for c in checks if not c.passed}
    assert "causal_language_absent" in failed       # prohibited attribution rejected
    assert not all(c.passed for c in checks)


def test_incident_variant_rejects_deployment_attribution_and_accepts_incident_attribution():
    attributed = _record(
        claims=[Claim(text="A deployment commit caused the revenue decline.",
                      evidence_ids=["ev-0001"])],
        conclusion="The decline followed a deployment commit.",
        verification=_clean_verification(),
        compare_delta_pct=-56.0,
    )
    checks = cases_variants.variant_contract_checks(
        "incident_without_deployment", attributed, expected_servers=EXPECTED_SERVERS)
    failed = {c.name for c in checks if not c.passed}
    assert "no_deployment_attribution" in failed
    assert not all(c.passed for c in checks)

    incident_ok = _record(
        claims=[Claim(text="Revenue fell 57% as the checkout incident opened.",
                      evidence_ids=["ev-0001"])],
        conclusion="Revenue declined during the checkout incident; the alert "
                   "and incident timeline align with the drop.",
        verification=_clean_verification(),
        compare_delta_pct=-56.0,
    )
    checks = cases_variants.variant_contract_checks(
        "incident_without_deployment", incident_ok, expected_servers=EXPECTED_SERVERS)
    assert all(c.passed for c in checks), [c.model_dump() for c in checks if not c.passed]


def test_no_anomaly_variant_rejects_fabricated_anomaly_via_verifier_strictness():
    fabricated = _record(
        claims=[Claim(text="Revenue fell exactly 57% in the window.",
                      evidence_ids=["ev-0001"])],
        conclusion="A sharp revenue decline occurred.",
        verification=VerificationResult(ok=False, flagged_claims=["57% not in evidence"],
                                        downgraded=False, grounding_checked=True),
        compare_delta_pct=4.0,  # healthy fixture truth contradicts the claim
    )
    checks = cases_variants.variant_contract_checks(
        "no_anomaly", fabricated, expected_servers=EXPECTED_SERVERS)
    failed = {c.name for c in checks if not c.passed}
    assert "verifier_strict_clean" in failed       # the verifier caught the fabrication
    # the EVIDENCE itself is healthy (delta +4.0), so the healthy-evidence check
    # correctly passes — the fabricated CLAIM is what fails the case
    assert "fixture_healthy_sales_evidence" not in failed
    assert not all(c.passed for c in checks)


# -- requirement 9 (wiring): invariants of the case-level classification ------------


def test_contracts_module_never_enables_the_write_path():
    assert cases_variants.CANONICAL_MODEL_SETTINGS.action_mode_enabled is False
