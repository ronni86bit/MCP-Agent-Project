"""Phase 9 Gate 4: deterministic variant harness integration.

PostgreSQL-gated (like the rest of the integration suite). These tests run the
REAL variant harness — spawned monitoring/github/postgres MCP subprocesses, a
real seeded throwaway database — and prove:

- all three variants are cross-store coherent before any model would run;
- a deliberately induced mismatch is DETECTED and, at case level, classified
  as infrastructure — the model is never invoked on a mismatched setup;
- cleanup drops exactly the tracked database and nothing else, and ownership
  verification refuses to drop a database the case does not own.

GitHub runs against the local mock (existing GITHUB_API_BASE_URL support) so
the round-trips are hermetic; no model is involved anywhere in this file.
"""

import sys
import uuid
from pathlib import Path

import asyncpg
import pytest

from backend.evaluation import cases_variants
from backend.evaluation.scenario_harness import (
    _admin_dsn,
    _drop_eval_db,
    assert_cross_store_coherence,
    variant_environment,
)
from tests.support.github_mock import build_github_mock


@pytest.fixture(scope="module")
def github_mock():
    server = build_github_mock()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


def _reachable() -> bool:
    import asyncio

    async def ping() -> bool:
        try:
            conn = await asyncpg.connect(_admin_dsn(), timeout=3)
            await conn.close()
            return True
        except Exception:
            return False

    return asyncio.run(ping())


pytestmark = pytest.mark.skipif(not _reachable(), reason="PostgreSQL unavailable")


# -- requirement 6: all three stores serve the same variant ------------------------


@pytest.mark.parametrize(
    "variant",
    ["deployment_without_incident", "incident_without_deployment", "no_anomaly"],
)
async def test_cross_store_coherence_holds_for_each_variant(variant, github_mock):
    async with variant_environment(variant) as ctx:
        coherent, failures = await assert_cross_store_coherence(
            ctx, github_base_url=github_mock)
    assert coherent, failures


# -- requirement 9: setup/coherence failures are infra, never reasoning ------------


async def test_induced_coherence_mismatch_is_detected(github_mock):
    """Serve the healthy V1 scenario while holding V2 expectations: the
    spec/served divergence must be caught as a coherence failure."""
    import dataclasses

    from backend.mcp.fixtures.loader import load_scenario
    from backend.evaluation.scenario_harness import variant_path

    async with variant_environment("deployment_without_incident") as env_ctx:
        v2_spec = load_scenario(variant_path("incident_without_deployment"))
        # expectations say V2 (alert+incident+decline), the servers serve V1
        ctx = dataclasses.replace(env_ctx, variant="incident_without_deployment",
                                  spec=v2_spec)
        coherent, failures = await assert_cross_store_coherence(
            ctx, github_base_url=github_mock)
    assert coherent is False
    assert any("presence" in f or "delta_pct" in f for f in failures)


async def test_coherence_failure_short_circuits_before_any_model_call(
    monkeypatch, github_mock
):
    """When coherence fails, the case returns an `infra` row immediately —
    the LLM is never invoked (llm=None would explode if it were)."""
    async def _failing_coherence(ctx, *, github_base_url=None):
        return False, ["injected coherence mismatch"]

    monkeypatch.setattr(cases_variants, "assert_cross_store_coherence",
                        _failing_coherence)
    case = await cases_variants.case_variant(
        "incident_without_deployment", llm=None,
        settings=cases_variants.CANONICAL_MODEL_SETTINGS,
    )
    assert case.failure_layer == "infra"
    assert case.checks[0].name == "cross_store_coherence"
    assert case.checks[0].passed is False


# -- requirements 5/7: exact-database ownership and cleanup ------------------------


async def test_cleanup_drops_only_the_tracked_database():
    admin_dsn = _admin_dsn()

    async def _create(name: str) -> None:
        admin = await asyncpg.connect(admin_dsn, timeout=10)
        try:
            await admin.execute(f"CREATE DATABASE {name}")
        finally:
            await admin.close()

    async def _exists(name: str) -> bool:
        admin = await asyncpg.connect(admin_dsn, timeout=10)
        try:
            return bool(await admin.fetchval(
                "SELECT 1 FROM pg_database WHERE datname = $1", name))
        finally:
            await admin.close()

    bystander = f"ops_agent_eval_bystander_{uuid.uuid4().hex[:6]}"
    await _create(bystander)
    try:
        async with variant_environment("no_anomaly") as ctx:
            tracked = ctx.db_name
            assert await _exists(tracked)
            assert await _exists(bystander)  # coexisting eval-named database
        assert not await _exists(tracked)      # the tracked one is gone...
        assert await _exists(bystander)        # ...and ONLY the tracked one
    finally:
        admin = await asyncpg.connect(admin_dsn, timeout=10)
        try:
            await admin.execute(f"DROP DATABASE {bystander} WITH (FORCE)")
        finally:
            await admin.close()


async def test_ownership_verification_refuses_to_drop_an_unowned_database():
    admin_dsn = _admin_dsn()
    # eval DSN resolves to 'ops_agent', but the tracked name is fabricated:
    # the guard must raise INSTEAD of dropping whatever the DSN points at
    with pytest.raises(RuntimeError, match="ownership verification failed"):
        await _drop_eval_db(admin_dsn, "ops_agent_eval_never_created",
                            admin_dsn)  # resolves to the canonical ops_agent db
    # the canonical database was not touched
    conn = await asyncpg.connect(admin_dsn, timeout=10)
    try:
        assert await conn.fetchval("SELECT current_database()") == "ops_agent"
    finally:
        await conn.close()


# -- runner wiring: variant cases live only in the anti-causation suite ------------


def test_variant_cases_are_only_in_the_anti_causation_suite():
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    import scripts.run_benchmark as runner

    deterministic_ids = [s.case_id for s in runner.build_case_specs()]
    assert not any(cid.startswith("V:") for cid in deterministic_ids)
    anti_ids = [s.case_id for s in runner.build_anti_causation_specs(_scripted_stub())]
    assert anti_ids == [
        "V:deployment_without_incident",
        "V:incident_without_deployment",
        "V:no_anomaly",
    ]


def _scripted_stub():
    from tests.scripted import ScriptedLLM
    from backend.agent.models import InvestigationReport, Plan, PlanStep, Understanding

    return ScriptedLLM(
        understanding=Understanding(goal="explain the drop", time_frame="2026-10-05",
                                    focus_areas=["checkout"],
                                    success_criteria="grounded answer"),
        plan=Plan(goal="investigate",
                  steps=[PlanStep(id=1, action="step 1", evidence_needed="e")],
                  expected_evidence=["e"], completion_criteria="done"),
        selections=[],
        report=InvestigationReport(conclusion="c"),
    )
