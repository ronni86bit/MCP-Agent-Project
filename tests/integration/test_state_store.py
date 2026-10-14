"""Phase 5 state persistence: schema init, record round-trips, plan versions,
evidence stability, interrupted recovery, restart retrieval.

Uses a dedicated TEST database (created fresh) so production state is never
touched; skips when PostgreSQL is unavailable.
"""

import asyncio
from datetime import datetime, timezone

import asyncpg
import pytest

from backend.agent.models import (
    Claim,
    Confidence,
    InvestigationReport,
    Plan,
    PlanStep,
    PlanVersionInfo,
    StepRecord,
    ToolCallObservation,
    Understanding,
    VerificationResult,
)
from backend.agent.persistence.store import InvestigationStore
from backend.agent.state import InvestigationRecord
from backend.config import get_settings
from tests.conftest import ADMIN_DSN

TEST_DB = "ops_agent_state_test"


def _state_dsn() -> str:
    return get_settings().ops_admin_dsn or "postgresql://ops_admin:ops_admin_dev@localhost:5433/ops_agent"


async def _recreate_test_database(admin_dsn: str) -> str:
    admin = await asyncpg.connect(admin_dsn)
    try:
        await admin.execute(f"DROP DATABASE IF EXISTS {TEST_DB} WITH (FORCE)")
        await admin.execute(f"CREATE DATABASE {TEST_DB}")
    finally:
        await admin.close()
    return admin_dsn.rsplit("/", 1)[0] + f"/{TEST_DB}"


@pytest.fixture(scope="module")
def state_dsn(admin_dsn: str) -> str:
    return asyncio.run(_recreate_test_database(admin_dsn))


@pytest.fixture
def fresh_store(state_dsn: str) -> InvestigationStore:
    return InvestigationStore(dsn=state_dsn)


def _record(investigation_id: str = "inv-state-1") -> InvestigationRecord:
    return InvestigationRecord(
        investigation_id=investigation_id,
        request_id="req-state-1",
        question="Why did today's sales drop?",
        status="running",
        stage="plan",
        understanding=Understanding(
            goal="explain the drop", time_frame="2026-10-05",
            focus_areas=["revenue"], success_criteria="grounded answer",
        ),
        created_at=datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc),
        updated_at=datetime(2026, 10, 5, 12, 5, tzinfo=timezone.utc),
    )


async def test_schema_initialization_is_versioned_and_idempotent(fresh_store: InvestigationStore):
    version = await fresh_store.ensure_schema()
    assert version >= 1
    version_again = await fresh_store.ensure_schema()
    assert version_again == version
    await fresh_store.close()


async def test_record_roundtrip_preserves_all_entities(state_dsn: str, fresh_store: InvestigationStore):
    await fresh_store.ensure_schema()
    record = _record()
    record.plan_version = 1
    plan = Plan(
        goal="investigate the drop",
        steps=[PlanStep(id=1, action="daily sales", evidence_needed="totals", status="completed")],
        expected_evidence=["daily totals"],
        completion_criteria="done",
    )
    record.plan = plan
    record.plan_history = [PlanVersionInfo(version=1, plan=plan)]
    record.steps = [StepRecord(index=1, plan_version=1, plan_step_id=1, action="daily sales", status="completed", evidence_ids=["ev-0001"])]
    record.tool_calls = [
        ToolCallObservation(
            step=1, tool="postgres-mcp__get_daily_sales",
            arguments={"start_date": "2026-10-05", "end_date": "2026-10-05"},
            status="ok", duration_ms=12.5, evidence_id="ev-0001",
        )
    ]
    record.evidence_ids = ["ev-0001"]
    record.report = InvestigationReport(
        conclusion="Revenue fell, consistent with evidence.",
        claims=[Claim(text="Revenue fell.", evidence_ids=["ev-0001"])],
        confidence=Confidence.medium,
        confidence_rationale="single source",
    )
    record.verification = VerificationResult(ok=True, grounding_checked=True)
    await fresh_store.persist_record(record)
    await fresh_store.close()

    reopened = InvestigationStore(dsn=state_dsn)
    try:
        loaded = await reopened.load_investigation(record.investigation_id)
    finally:
        await reopened.close()

    assert loaded is not None
    assert loaded.question == record.question
    assert loaded.status == "running" and loaded.stage == "plan"
    assert loaded.understanding is not None and loaded.understanding.goal == "explain the drop"
    assert loaded.plan_version == 1
    assert loaded.plan is not None and loaded.plan.steps[0].action == "daily sales"
    assert loaded.plan.steps[0].status == "completed"
    assert loaded.steps[0].status == "completed" and loaded.steps[0].evidence_ids == ["ev-0001"]
    assert loaded.tool_calls[0].tool == "postgres-mcp__get_daily_sales"
    assert loaded.tool_calls[0].arguments["start_date"] == "2026-10-05"
    assert loaded.report is not None and loaded.report.conclusion.startswith("Revenue fell")
    assert loaded.verification is not None and loaded.verification.grounding_checked
    assert loaded.evidence == []  # none added in this record


async def test_evidence_persistence_with_stable_ids(state_dsn: str, fresh_store: InvestigationStore):
    from datetime import datetime

    await fresh_store.ensure_schema()
    record = _record("inv-state-ev")
    from backend.agent.evidence import Evidence

    item = Evidence(
        evidence_id="ev-0001", investigation_id=record.investigation_id,
        server="monitoring-mcp", tool_name="get_error_rate", step=1,
        arguments={"service": "checkout"}, summary={"window_avg_pct": 10.3},
        at=datetime(2026, 10, 5, 12, 1, tzinfo=timezone.utc),
    )
    record.evidence = [item]
    record.evidence_ids = ["ev-0001"]
    await fresh_store.persist_record(record)
    await fresh_store.persist_record(record)  # idempotent re-write
    await fresh_store.close()

    reopened = InvestigationStore(dsn=state_dsn)
    try:
        loaded = await reopened.load_investigation(record.investigation_id)
    finally:
        await reopened.close()
    assert loaded is not None and len(loaded.evidence) == 1, "no duplicates on re-persist"
    assert loaded.evidence[0].evidence_id == "ev-0001"
    assert loaded.evidence[0].server == "monitoring-mcp"
    assert loaded.evidence[0].summary["window_avg_pct"] == 10.3


async def test_plan_versions_preserve_history(state_dsn: str, fresh_store: InvestigationStore):
    await fresh_store.ensure_schema()
    record = _record("inv-state-plans")
    v1 = Plan(goal="investigate v1", steps=[PlanStep(id=1, action="first direction", evidence_needed="totals")], completion_criteria="done")
    record.plan_version = 1
    record.plan = v1
    record.plan_history = [PlanVersionInfo(version=1, plan=v1)]
    await fresh_store.persist_record(record)

    v2 = Plan(goal="investigate v2", steps=[PlanStep(id=1, action="changed direction", evidence_needed="errors")], completion_criteria="done")
    for info in record.plan_history:
        info.superseded = True
    record.plan_version = 2
    record.plan = v2
    record.plan_history.append(PlanVersionInfo(version=2, plan=v2, revision_reason="evidence changed direction"))
    await fresh_store.persist_record(record)
    await fresh_store.close()

    reopened = InvestigationStore(dsn=state_dsn)
    try:
        loaded = await reopened.load_investigation(record.investigation_id)
    finally:
        await reopened.close()
    assert loaded is not None
    assert loaded.plan_version == 2
    assert loaded.plan is not None and loaded.plan.steps[0].action == "changed direction"
    assert [info.version for info in loaded.plan_history] == [1, 2]
    assert loaded.plan_history[0].superseded is True
    assert loaded.plan_history[0].plan.steps[0].action == "first direction", "v1 content preserved"
    assert loaded.plan_history[1].superseded is False


async def test_interruption_sweep_resolves_uncertainty_without_erasing(state_dsn: str, fresh_store: InvestigationStore):
    await fresh_store.ensure_schema()
    record = _record("inv-state-int")
    record.status = "running"
    record.plan_version = 1
    plan = Plan(goal="investigate", steps=[PlanStep(id=1, action="daily sales", evidence_needed="totals")], completion_criteria="done")
    record.plan = plan
    record.plan_history = [PlanVersionInfo(version=1, plan=plan)]
    record.steps = [StepRecord(index=1, plan_version=1, plan_step_id=1, action="a", status="running")]
    record.tool_calls = [
        ToolCallObservation(step=1, tool="postgres-mcp__get_daily_sales", status="running")
    ]
    await fresh_store.persist_record(record)
    await fresh_store.close()

    sweeper = InvestigationStore(dsn=state_dsn)
    interrupted = await sweeper.mark_interrupted()
    await sweeper.close()
    assert record.investigation_id in interrupted

    reopened = InvestigationStore(dsn=state_dsn)
    try:
        loaded = await reopened.load_investigation(record.investigation_id)
    finally:
        await reopened.close()
    assert loaded is not None
    assert loaded.status == "interrupted"
    assert "backend restarted" in (loaded.failure_reason or "")
    assert loaded.tool_calls[0].status == "unknown", "uncertain calls are never marked executed"
    assert loaded.steps[0].status == "skipped"
    assert loaded.evidence_ids == [], "nothing was erased or fabricated"
    # sweep is idempotent
    sweeper2 = InvestigationStore(dsn=state_dsn)
    again = await sweeper2.mark_interrupted()
    await sweeper2.close()
    assert record.investigation_id not in again


async def test_complete_investigation_survives_restart(state_dsn: str, fresh_store: InvestigationStore):
    """Backend restart simulation: persist, drop the store, reload from disk."""
    await fresh_store.ensure_schema()
    record = _record("inv-state-done")
    record.status = "complete"
    record.plan_version = 1
    plan = Plan(goal="investigate", steps=[PlanStep(id=1, action="daily sales", evidence_needed="totals")], completion_criteria="done")
    record.plan = plan
    record.plan_history = [PlanVersionInfo(version=1, plan=plan)]
    record.report = InvestigationReport(
        conclusion="Final grounded conclusion.",
        claims=[Claim(text="Final claim.", evidence_ids=[])],
        confidence=Confidence.low,
    )
    record.verification = VerificationResult(ok=True, grounding_checked=True)
    await fresh_store.persist_record(record)
    await fresh_store.close()  # "restart"

    restarted = InvestigationStore(dsn=state_dsn)
    try:
        loaded = await restarted.load_investigation(record.investigation_id)
    finally:
        await restarted.close()
    assert loaded is not None
    assert loaded.status == "complete"
    assert loaded.report is not None and loaded.report.conclusion.startswith("Final grounded")
