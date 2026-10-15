"""Gate 1 persistence tests: v1→v2 migration, approval persistence, conditional
transitions, races, sweep idempotency/terminal-state safety.

Deterministic — dedicated test database, no subprocesses, no LLM.
"""

import asyncio
from datetime import datetime, timedelta, timezone

import asyncpg
import pytest

from backend.agent.models import ApprovalRecord
from backend.agent.state import InvestigationRecord
from backend.agent.persistence.store import InvestigationStore
from backend.config import get_settings
from tests.conftest import ADMIN_DSN

TEST_DB = "ops_agent_approvals_test"


def _state_dsn() -> str:
    return (get_settings().ops_admin_dsn
            or "postgresql://ops_admin:ops_admin_dev@localhost:5433/ops_agent").rsplit("/", 1)[0] + f"/{TEST_DB}"


@pytest.fixture(scope="module")
def state_dsn(admin_dsn: str) -> str:
    async def recreate():
        admin = await asyncpg.connect(admin_dsn)
        try:
            await admin.execute(f"DROP DATABASE IF EXISTS {TEST_DB} WITH (FORCE)")
            await admin.execute(f"CREATE DATABASE {TEST_DB}")
        finally:
            await admin.close()

    asyncio.run(recreate())
    return _state_dsn()


@pytest.fixture
def store(state_dsn: str) -> InvestigationStore:
    return InvestigationStore(dsn=state_dsn)


def _approval(approval_id: str, investigation_id: str, *, status: str = "proposed",
              with_hash: str | None = "abc123", expires_in_days: int | None = 30) -> ApprovalRecord:
    return ApprovalRecord(
        approval_id=approval_id,
        investigation_id=investigation_id,
        tool="acknowledge_alert",
        arguments={"alert_id": "ALT-2071", "note": "acknowledged during investigation"},
        status=status,
        proposal={
            "tool": "acknowledge_alert",
            "arguments": {"alert_id": "ALT-2071", "note": "acknowledged during investigation"},
            "rationale": "unacknowledged high-severity alert observed",
            "evidence_ids": ["ev-0001"],
            "proposal_version": 1,
        },
        args_hash=with_hash,
        proposal_version=1,
        expires_at=(datetime(2026, 10, 5, tzinfo=timezone.utc)
                    + timedelta(days=expires_in_days)) if expires_in_days else None,
        created_at=datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc),
    )


async def _seed_investigation(store: InvestigationStore, investigation_id: str) -> None:
    await store.persist_record(
        InvestigationRecord(
            investigation_id=investigation_id,
            request_id=f"req-{investigation_id}",
            question="Why did today's sales drop?",
        )
    )


async def _create_v1_database(state_dsn: str) -> None:
    """Rebuild the test database with ONLY the v1 schema + one v1 row."""
    from backend.agent.persistence.schema import MIGRATIONS, SCHEMA_NAME

    admin_dsn = _state_dsn().rsplit("/", 1)[0] + "/postgres"
    admin = await asyncpg.connect(admin_dsn)
    try:
        await admin.execute(f"DROP DATABASE IF EXISTS {TEST_DB} WITH (FORCE)")
        await admin.execute(f"CREATE DATABASE {TEST_DB}")
    finally:
        await admin.close()

    conn = await asyncpg.connect(state_dsn)
    try:
        await conn.execute(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA_NAME}")
        await conn.execute(
            f"CREATE TABLE IF NOT EXISTS {SCHEMA_NAME}.schema_migrations "
            "(version INTEGER PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
        )
        for statement in MIGRATIONS[1]:
            await conn.execute(statement)
        await conn.execute(f"INSERT INTO {SCHEMA_NAME}.schema_migrations (version) VALUES (1)")
        await conn.execute(
            f"INSERT INTO {SCHEMA_NAME}.investigations "
            "(investigation_id, request_id, question, status, stage, created_at, updated_at) "
            "VALUES ('inv-v1', 'req-v1', 'Why did sales drop?', 'complete', 'complete', now(), now())"
        )
        await conn.execute(
            f"INSERT INTO {SCHEMA_NAME}.approvals "
            "(approval_id, investigation_id, tool, arguments, status, created_at) "
            "VALUES ('apr-v1', 'inv-v1', 'acknowledge_alert', "
            "'{\"alert_id\": \"ALT-2071\"}'::jsonb, 'proposed', now())"
        )
    finally:
        await conn.close()


async def test_v2_migration_preserves_v1_data(state_dsn: str):
    """A v1 database (schema v1 recorded, one v1 approval row) migrates to v2
    with the row intact and the new columns defaulted."""
    await _create_v1_database(state_dsn)
    store = InvestigationStore(dsn=state_dsn)
    version = await store.ensure_schema()
    await store.close()
    assert version == 2

    conn = await asyncpg.connect(state_dsn)
    try:
        row = await conn.fetchrow(
            "SELECT approval_id, tool, status, args_hash, proposal_version, proposal "
            "FROM agent_state.approvals WHERE approval_id = 'apr-v1'"
        )
        versions = [r["version"] for r in await conn.fetch(
            "SELECT version FROM agent_state.schema_migrations ORDER BY version"
        )]
    finally:
        await conn.close()
    assert row is not None, "v1 row must survive the migration"
    assert row["status"] == "proposed"
    assert row["args_hash"] is None and row["proposal_version"] is None  # new columns defaulted
    import json

    assert json.loads(row["proposal"]) == {}
    assert versions == [1, 2], "migration order recorded"


async def _events(dsn: str, investigation_id: str) -> list[dict]:
    conn = await asyncpg.connect(dsn)
    try:
        rows = await conn.fetch(
            "SELECT event, payload FROM agent_state.investigation_events "
            "WHERE investigation_id = $1 ORDER BY event_id",
            investigation_id,
        )
        import json

        return [{"event": r["event"], "payload": json.loads(r["payload"])} for r in rows]
    finally:
        await conn.close()


async def test_proposal_persistence_roundtrip_with_event(state_dsn: str, store: InvestigationStore):
    await store.ensure_schema()
    approval = _approval("apr-rt-1", "inv-rt-1")
    await store.persist_record(
        InvestigationRecord(investigation_id="inv-rt-1", request_id="r", question="q")
    )
    await store.save_proposal(approval)

    loaded = await store.get_approval("apr-rt-1")
    assert loaded is not None
    assert loaded.status == "proposed"
    assert loaded.arguments == {"alert_id": "ALT-2071", "note": "acknowledged during investigation"}
    assert loaded.args_hash == "abc123"
    assert loaded.proposal["rationale"].startswith("unacknowledged")
    assert loaded.proposal_version == 1

    events = await _events(state_dsn, "inv-rt-1")
    assert any(e["event"] == "proposal_created" for e in events)


async def test_decide_approval_conditional_transitions(state_dsn: str, store: InvestigationStore):
    await store.ensure_schema()
    inv = "inv-decide"
    await _seed_investigation(store, inv)

    approval = _approval("apr-dec-1", inv)
    await store.save_proposal(approval)

    # invalid decision value rejected outright
    with pytest.raises(ValueError):
        await store.decide_approval("apr-dec-1", "maybe", "op")
    # blank operator label rejected
    with pytest.raises(ValueError):
        await store.decide_approval("apr-dec-1", "approved", "   ")

    decided = await store.decide_approval("apr-dec-1", "approved", "operator-1", comment="ok")
    assert decided is not None and decided.status == "approved"
    assert decided.decided_by == "operator-1" and decided.decided_at is not None

    # second decision from `approved` -> lost race / invalid transition
    assert await store.decide_approval("apr-dec-1", "rejected", "operator-2") is None

    # unknown id -> None
    assert await store.decide_approval("apr-nope", "approved", "op") is None

    events = await _events(state_dsn, inv)
    decided_events = [e for e in events if e["event"] == "proposal_decided"]
    assert len(decided_events) == 1  # exactly one decision event, atomic with the transition
    assert decided_events[0]["payload"]["decided_by"] == "operator-1"


async def test_execution_lifecycle_transitions_and_races(store: InvestigationStore):
    await store.ensure_schema()
    inv = "inv-exec"
    await _seed_investigation(store, inv)
    await store.save_proposal(_approval("apr-exec-1", inv))

    # cannot execute a merely proposed action
    assert await store.begin_execution("apr-exec-1", "op-1") is None

    assert await store.decide_approval("apr-exec-1", "approved", "op-1") is not None
    executing = await store.begin_execution("apr-exec-1", "op-2")
    assert executing is not None and executing.status == "executing"
    assert executing.executed_by == "op-2"

    # concurrent second executor loses the race
    assert await store.begin_execution("apr-exec-1", "op-3") is None

    await store.mark_dispatched("apr-exec-1")
    finished = await store.finish_execution(
        "apr-exec-1", "executed",
        {"write": "acknowledge_alert", "alert_acknowledged": True},
        {"ok": True, "checked_at": "2026-10-05T13:00:00Z"},
    )
    assert finished is not None and finished.status == "executed"
    assert finished.verification == {"ok": True, "checked_at": "2026-10-05T13:00:00Z"}

    # terminal: no further transitions
    assert await store.begin_execution("apr-exec-1", "op-4") is None
    assert await store.finish_execution("apr-exec-1", "failed", {"r": 1}) is None

    with pytest.raises(ValueError):
        await store.finish_execution("apr-exec-1", "exploded", {})


async def test_revert_to_approved_only_before_dispatch(store: InvestigationStore):
    await store.ensure_schema()
    inv = "inv-revert"
    await _seed_investigation(store, inv)
    await store.save_proposal(_approval("apr-rev-1", inv))
    await store.decide_approval("apr-rev-1", "approved", "op-1")

    assert await store.begin_execution("apr-rev-1", "op-2") is not None
    reverted = await store.revert_to_approved("apr-rev-1")
    assert reverted is not None and reverted.status == "approved"

    # re-approval/execution works after a pre-dispatch revert
    assert await store.begin_execution("apr-rev-1", "op-2") is not None

    await store.mark_dispatched("apr-rev-1")
    assert await store.revert_to_approved("apr-rev-1") is None, \
        "post-dispatch revert is forbidden: outcome is uncertain, not 'approved'"
    # park the row in a defined state: post-dispatch means uncertain, never
    # silently back to approved
    await store.mark_uncertain("apr-rev-1", "test teardown: ended post-dispatch")


async def test_uncertain_state_and_recovery(store: InvestigationStore):
    await store.ensure_schema()
    inv = "inv-unc"
    await _seed_investigation(store, inv)
    await store.save_proposal(_approval("apr-unc-1", inv))
    await store.decide_approval("apr-unc-1", "approved", "op-1")
    await store.begin_execution("apr-unc-1", "op-1")
    await store.mark_dispatched("apr-unc-1")

    marked = await store.mark_uncertain("apr-unc-1", "transport failure after dispatch")
    assert marked is not None and marked.status == "uncertain"
    assert marked.execution_result["uncertain_reason"] == "transport failure after dispatch"
    assert marked.executed_by == "op-1", "original operator preserved"

    # blank recovery operator rejected
    with pytest.raises(ValueError):
        await store.resolve_uncertain("apr-unc-1", "executed", "   ", {})

    resolved = await store.resolve_uncertain(
        "apr-unc-1", "executed", "op-recovery",
        {"recovery": {"executed_by": "op-recovery"}}, {"ok": True},
    )
    assert resolved is not None and resolved.status == "executed"
    assert resolved.executed_by == "op-recovery"
    assert resolved.execution_result["recovery"]["executed_by"] == "op-recovery"
    assert resolved.execution_result["uncertain_reason"] == "transport failure after dispatch"

    assert await store.resolve_uncertain("apr-unc-1", "failed", "op-r2", {}) is None


async def test_sweep_marks_only_executing_approvals_uncertain(state_dsn: str, store: InvestigationStore):
    await store.ensure_schema()
    inv = "inv-sweep"
    await _seed_investigation(store, inv)
    rows = {
        "apr-sw-exec": ("executing", None),
        "apr-sw-approved": ("approved", None),
        "apr-sw-rejected": ("rejected", None),
        "apr-sw-executed": ("executed", None),
        "apr-sw-proposed": ("proposed", None),
    }
    # build rows with the exact target statuses (bypassing transitions for
    # terminal ones by writing directly through save_proposal + transitions)
    for approval_id, (status, _) in rows.items():
        await store.save_proposal(_approval(approval_id, inv))
        if status == "approved":
            await store.decide_approval(approval_id, "approved", "op")
        elif status == "rejected":
            await store.decide_approval(approval_id, "rejected", "op")
        elif status == "executed":
            await store.decide_approval(approval_id, "approved", "op")
            await store.begin_execution(approval_id, "op")
            await store.finish_execution(approval_id, "executed", {})
        elif status == "executing":
            await store.decide_approval(approval_id, "approved", "op")
            await store.begin_execution(approval_id, "op")

    count = await store.mark_executing_approvals_uncertain()
    assert count == 1

    loaded = {a.approval_id: a.status for a in await store.list_approvals()}
    assert loaded["apr-sw-exec"] == "uncertain"
    assert loaded["apr-sw-approved"] == "approved"
    assert loaded["apr-sw-rejected"] == "rejected"
    assert loaded["apr-sw-executed"] == "executed"
    assert loaded["apr-sw-proposed"] == "proposed"

    # idempotent: second sweep resolves nothing
    assert await store.mark_executing_approvals_uncertain() == 0
    assert loaded["apr-sw-exec"] == "uncertain"

    events = await _events(state_dsn, inv)
    interrupted = [e for e in events if e["event"] == "approval_interrupted"]
    assert len(interrupted) == 1
    assert interrupted[0]["payload"]["approval_id"] == "apr-sw-exec"


async def test_full_investigation_sweep_also_resolves_executing_approvals(store: InvestigationStore):
    """The Phase 5 mark_interrupted sweep (investigations) now also resolves
    orphaned executing approvals — one restart, one sweep, everything safe."""
    await store.ensure_schema()
    inv = "inv-sweep-full"
    await _seed_investigation(store, inv)
    from backend.agent.models import StepRecord, ToolCallObservation

    record = InvestigationRecord(
        investigation_id=inv, request_id="r", question="q", status="running"
    )
    record.steps = [StepRecord(index=1, action="mid-flight", status="running")]
    record.tool_calls = [ToolCallObservation(
        step=1, tool="postgres-mcp__get_daily_sales", status="running")]
    await store.persist_record(record)

    approval = _approval("apr-sweep-full-1", inv)
    await store.save_proposal(approval)
    await store.decide_approval("apr-sweep-full-1", "approved", "op-1")
    await store.begin_execution("apr-sweep-full-1", "op-1")

    interrupted = await store.mark_interrupted()
    assert inv in interrupted

    approval_row = await store.get_approval("apr-sweep-full-1")
    assert approval_row is not None and approval_row.status == "uncertain"
    assert approval_row.execution_result["uncertain_reason"] == "backend restart"

    record_loaded = await store.load_investigation(inv)
    assert record_loaded is not None and record_loaded.status == "interrupted"
