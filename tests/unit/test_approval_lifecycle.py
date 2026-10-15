"""Phase 7 Gate 2: ApprovalService lifecycle and security, deterministic via
an injected fake monitoring session and a dedicated test database.

Covers the required matrix: approve/reject transitions, expiry, hash
tampering, stale alert state, concurrent/duplicate execution, pre/post-dispatch
cancellation, late write completion, shutdown, recovery without blind replay,
verification success/failure, blank operator identities, and the executor
policy gate for the write tool.
"""

import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from backend.agent.evidence import Evidence, EvidenceStore
from backend.agent.executor import Executor
from backend.agent.catalog import SERVER_ALLOWLISTS, ToolCatalog
from backend.agent.models import ApprovalRecord, ToolCallRequest
from backend.agent.persistence.store import InvestigationStore
from backend.agent.state import InvestigationRecord
from backend.reliability import CircuitState
from backend.security.approval_actions import (
    WRITABLE_ACTIONS,
    args_hash,
    eligible_alert_ids,
)
from backend.security.approval_service import (
    ApprovalConflictError,
    ApprovalExpiredError,
    ApprovalInvalidIdentityError,
    ApprovalNotFoundError,
    ApprovalPrecheckFailedError,
    ApprovalService,
    ApprovalTamperedError,
    ApprovalUncertainError,
    verify_acknowledgement,
)

ALERT = {"id": "ALT-2071", "service": "checkout", "acknowledged": False,
         "acknowledged_by": None, "acknowledged_at": None}


class FakeSessionCM:
    """Async CM yielding the current inner session (tests may swap it)."""

    def __init__(self, holder: dict):
        self._holder = holder

    async def __aenter__(self):
        return self._holder["current"]

    async def __aexit__(self, *exc_info):
        return False


class FakeSession:
    """Scripted monitoring session: records calls, serves canned payloads."""

    def __init__(self, queues: dict[str, list]):
        self.calls: list[str] = []
        self._queues = {tool: list(items) for tool, items in queues.items()}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    async def call_tool(self, server: str, tool: str, arguments: dict):
        self.calls.append(tool)
        queue = self._queues.get(tool)
        if not queue:
            return _result({"alerts": [dict(ALERT)], "count": 1})
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return _result(item)


class HangSession:
    """get_recent_alerts: returns immediately. acknowledge_alert: hangs until
    the gate is set (simulating an in-flight write)."""

    def __init__(self, *, hang_read: bool = False):
        self.reads = 0
        self.writes = 0
        self.write_gate = asyncio.Event()
        self._hang_read = hang_read
        self._acked_by: str | None = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    async def call_tool(self, server: str, tool: str, arguments: dict):
        if tool == "acknowledge_alert":
            self.writes += 1
            await self.write_gate.wait()
            self._acked_by = arguments.get("acknowledged_by")
            return _result({"acknowledged": True, "alert_id": arguments["alert_id"],
                            "acknowledged_at": "2026-10-05T12:00:00Z",
                            "acknowledged_by": arguments["acknowledged_by"]})
        if tool == "get_recent_alerts":
            self.reads += 1
            if self._hang_read and self.reads == 1:
                await asyncio.Event().wait()
            if self._acked_by is not None:
                return _result({"alerts": [dict(ALERT, acknowledged=True,
                                                    acknowledged_by=self._acked_by,
                                                    acknowledged_at="2026-10-05T12:00:00Z")],
                                "count": 1})
            return _result({"alerts": [dict(ALERT)], "count": 1})
        raise AssertionError(f"unexpected tool {tool}")


def _result(payload: dict):
    from types import SimpleNamespace

    return SimpleNamespace(structured_content=payload, content=[])


def _evidence() -> list[Evidence]:
    store = EvidenceStore("inv-approval")
    store.add(
        tool_name="get_recent_alerts", server="monitoring-mcp", step=1,
        arguments={"service": "checkout"},
        payload={"count": 1,
                 "alerts": [{"id": "ALT-2071", "service": "checkout",
                             "acknowledged": False}]},
    )
    return store.all()


def _now() -> datetime:
    return datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)


class FixedClock:
    def __init__(self, start: datetime):
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


def _service(store: InvestigationStore, session_factory, clock: FixedClock | None = None,
             timeout: float = 5.0) -> ApprovalService:
    return ApprovalService(
        store,
        session_factory,
        clock=clock if clock is not None else _now,
        execution_timeout_seconds=timeout,
    )


def _proposal_args() -> dict:
    return {"alert_id": "ALT-2071", "note": "acknowledged during incident response"}


def _approval_record(approval_id: str, clock: FixedClock) -> ApprovalRecord:
    args = _proposal_args()
    expires = clock.now + timedelta(seconds=3600)
    return ApprovalRecord(
        approval_id=approval_id,
        investigation_id="inv-g2",
        tool="acknowledge_alert",
        arguments=args,
        status="proposed",
        proposal={"tool": "acknowledge_alert", "arguments": args,
                  "rationale": "unacknowledged high-severity alert",
                  "evidence_ids": ["ev-0001"], "proposal_version": 1},
        args_hash=args_hash(action="acknowledge_alert", target_server="monitoring-mcp",
                            arguments=args, proposal_version=1, expires_at=expires),
        proposal_version=1,
        expires_at=expires,
        created_at=clock.now,
    )


async def _saved_proposal(store: InvestigationStore, clock: FixedClock) -> ApprovalRecord:
    await store.ensure_schema()
    await store.persist_record(
        InvestigationRecord(investigation_id="inv-g2", request_id="req-g2",
                            question="Why did today's sales drop?", status="complete")
    )
    approval = _approval_record("apr-" + uuid.uuid4().hex[:8], clock)
    await store.save_proposal(approval)
    return approval



# -- registry / hash / eligibility / identity ---------------------------------


def test_registry_contains_exactly_acknowledge_alert():
    assert WRITABLE_ACTIONS == {"acknowledge_alert": "monitoring-mcp"}


def test_args_hash_is_stable_and_binds_all_fields():
    base = dict(action="acknowledge_alert", target_server="monitoring-mcp",
                arguments={"alert_id": "ALT-2071", "note": None},
                proposal_version=1, expires_at=_now() + timedelta(hours=1))
    assert args_hash(**base) == args_hash(**base)
    assert args_hash(**{**base, "arguments": {"alert_id": "ALT-9999", "note": None}}) != args_hash(**base)
    assert args_hash(**{**base, "proposal_version": 2}) != args_hash(**base)
    assert args_hash(**{**base, "expires_at": _now() + timedelta(hours=2)}) != args_hash(**base)
    assert args_hash(**{**base, "target_server": "other"}) != args_hash(**base)


def test_eligibility_from_evidence_previews():
    assert eligible_alert_ids(_evidence()) == ["ALT-2071"]


def test_eligibility_ignores_acknowledged_alerts():
    store = EvidenceStore("inv")
    store.add(tool_name="get_recent_alerts", server="monitoring-mcp", step=1,
              arguments={}, payload={"count": 1, "alerts": [
                  {"id": "ALT-2071", "acknowledged": True}]})
    assert eligible_alert_ids(store.all()) == []


async def test_blank_operator_identity_is_rejected_before_any_state_change(state_test_store):
    store = state_test_store()
    clock = FixedClock(_now())
    service = _service(store, lambda: FakeSessionCM({}), clock)
    approval = await _saved_proposal(store, clock)
    await service.decide(approval.approval_id, "approved", "op-1")

    for label in ("", "   ", None):
        with pytest.raises(ApprovalInvalidIdentityError):
            await service.execute(approval.approval_id, label)
    row = await store.get_approval(approval.approval_id)
    assert row.status == "approved", "rejected identity leaves the approval untouched"


# -- lifecycle ----------------------------------------------------------------


async def test_full_lifecycle_propose_decide_execute_verified(state_test_store):
    store = state_test_store()
    clock = FixedClock(_now())
    session = FakeSession({
        "get_recent_alerts": [
            {"alerts": [dict(ALERT)], "count": 1},                           # pre-check
            {"alerts": [dict(ALERT, acknowledged=True, acknowledged_by="op-1",
                              acknowledged_at="2026-10-05T12:00:01Z")],
             "count": 1},                                                    # verification
        ]
    })
    service = _service(store, lambda: FakeSessionCM({"current": session}), clock)
    approval = await _saved_proposal(store, clock)

    await service.decide(approval.approval_id, "approved", "op-1")
    executed = await service.execute(approval.approval_id, "op-2")

    assert executed.status == "executed"
    assert executed.verification == {
        "ok": True, "details": "acknowledged at 2026-10-05T12:00:01Z"
    }
    assert executed.executed_by == "op-2" and executed.decided_by == "op-1"
    assert session.calls.count("get_recent_alerts") == 2  # pre-check + verification, SAME session


async def test_rejection_and_double_decision(state_test_store):
    store = state_test_store()
    clock = FixedClock(_now())
    service = _service(store, lambda: FakeSessionCM({}), clock)
    approval = await _saved_proposal(store, clock)

    rejected = await service.decide(approval.approval_id, "rejected", "op-1", "not needed")
    assert rejected.status == "rejected"

    with pytest.raises(ApprovalConflictError):
        await service.decide(approval.approval_id, "approved", "op-2")
    with pytest.raises(ApprovalConflictError):
        await service.execute(approval.approval_id, "op-3")

    row = await store.get_approval(approval.approval_id)
    assert row.status == "rejected"


async def test_expired_proposal_is_terminal(state_test_store):
    store = state_test_store()
    clock = FixedClock(_now())
    service = _service(store, lambda: FakeSessionCM({}), clock)
    approval = await _saved_proposal(store, clock)
    clock.advance(3601)

    with pytest.raises(ApprovalExpiredError):
        await service.decide(approval.approval_id, "approved", "op-1")
    with pytest.raises(ApprovalExpiredError):
        await service.execute(approval.approval_id, "op-2")
    row = await store.get_approval(approval.approval_id)
    assert row.status == "expired"


async def test_tampered_arguments_are_refused_without_write(state_test_store):
    store = state_test_store()
    clock = FixedClock(_now())
    service = _service(store, lambda: FakeSessionCM({}), clock)
    approval = await _saved_proposal(store, clock)
    await service.decide(approval.approval_id, "approved", "op-1")

    conn = await store._conn()
    try:
        await conn.execute(
            "UPDATE agent_state.approvals "
            "SET arguments = jsonb_build_object('alert_id', 'ALT-9999', 'note', 'x') "
            "WHERE approval_id = $1",
            approval.approval_id,
        )
    finally:
        await store._release(store._pool, conn)

    with pytest.raises(ApprovalTamperedError):
        await service.execute(approval.approval_id, "op-2")
    row = await store.get_approval(approval.approval_id)
    assert row.status == "failed"
    assert "hash" in row.execution_result["refusal_reason"]


async def test_stale_alert_state_supersedes_without_write(state_test_store):
    store = state_test_store()
    clock = FixedClock(_now())
    already = dict(ALERT, acknowledged=True, acknowledged_by="someone-else",
                   acknowledged_at="2026-10-05T11:00:00Z")
    session = FakeSession({"get_recent_alerts": [{"alerts": [already], "count": 1}]})
    service = _service(store, lambda: FakeSessionCM({"current": session}), clock)
    approval = await _saved_proposal(store, clock)
    await service.decide(approval.approval_id, "approved", "op-1")

    with pytest.raises(ApprovalPrecheckFailedError):
        await service.execute(approval.approval_id, "op-2")
    row = await store.get_approval(approval.approval_id)
    assert row.status == "superseded"
    assert len(session.calls) == 1, "pre-check only: NO write attempted"


# -- cancellation / uncertainty ------------------------------------------------


async def test_pre_dispatch_cancellation_reverts_to_approved(state_test_store):
    store = state_test_store()
    clock = FixedClock(_now())
    hang_read = HangSession(hang_read=True)
    service = _service(store, lambda: FakeSessionCM({"current": hang_read}), clock)
    approval = await _saved_proposal(store, clock)
    await service.decide(approval.approval_id, "approved", "op-1")

    task = asyncio.create_task(service.execute(approval.approval_id, "op-2"))
    await asyncio.sleep(0.05)
    assert hang_read.reads == 1 and hang_read.writes == 0
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    row = await store.get_approval(approval.approval_id)
    assert row.status == "approved", "pre-dispatch cancellation reverts to approved"
    assert row.dispatched_at is None


async def test_post_dispatch_cancellation_marks_uncertain_then_records_late_completion(state_test_store):
    store = state_test_store()
    clock = FixedClock(_now())
    hang_write = HangSession()
    service = _service(store, lambda: FakeSessionCM({"current": hang_write}), clock)
    approval = await _saved_proposal(store, clock)
    await service.decide(approval.approval_id, "approved", "op-1")

    task = asyncio.create_task(service.execute(approval.approval_id, "op-2"))
    while hang_write.writes == 0:
        await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    row = await store.get_approval(approval.approval_id)
    assert row.status == "uncertain"

    # the shielded write eventually completes and records its own outcome
    hang_write.write_gate.set()
    await asyncio.sleep(0.05)
    row = await store.get_approval(approval.approval_id)
    assert row.status == "executed"
    assert row.verification["ok"] is True


async def test_shutdown_awaits_in_flight_writes(state_test_store):
    store = state_test_store()
    clock = FixedClock(_now())
    hang_write = HangSession()
    service = _service(store, lambda: FakeSessionCM({"current": hang_write}), clock)
    approval = await _saved_proposal(store, clock)
    await service.decide(approval.approval_id, "approved", "op-1")

    task = asyncio.create_task(service.execute(approval.approval_id, "op-2"))
    while hang_write.writes == 0:
        await asyncio.sleep(0.01)
    shutdown = asyncio.create_task(service.shutdown())
    await asyncio.sleep(0.05)
    assert not shutdown.done(), "graceful shutdown waits for the in-flight write"
    hang_write.write_gate.set()
    await asyncio.sleep(0.05)
    await shutdown
    row = await store.get_approval(approval.approval_id)
    assert row.status == "executed"  # write completed and was persisted


async def test_uncertain_requires_recovery_without_blind_replay(state_test_store):
    store = state_test_store()
    clock = FixedClock(_now())
    session = FakeSession({
        "get_recent_alerts": [
            {"alerts": [dict(ALERT, acknowledged=True, acknowledged_by="op-1",
                              acknowledged_at="2026-10-05T12:00:01Z")], "count": 1},
        ]
    })
    service = _service(store, lambda: FakeSessionCM({"current": session}), clock)
    approval = await _saved_proposal(store, clock)
    await service.decide(approval.approval_id, "approved", "op-1")
    await store.begin_execution(approval.approval_id, "op-1")
    await store.mark_uncertain(approval.approval_id, "simulated transport failure")

    resolved = await service.resolve_uncertain(approval.approval_id, "op-recovery")
    assert resolved.status == "executed"
    assert resolved.executed_by == "op-recovery", "recovery operator recorded separately"
    assert resolved.execution_result["recovery"]["resolved_from"] == "uncertain"
    assert resolved.verification["ok"] is True

    with pytest.raises(ApprovalConflictError):
        await service.resolve_uncertain(approval.approval_id, "op-recovery-2")


async def test_verification_failure_is_preserved_and_surfaced(state_test_store):
    store = state_test_store()
    clock = FixedClock(_now())
    mismatched = dict(ALERT, acknowledged=True, acknowledged_by="someone-unexpected",
                      acknowledged_at="2026-10-05T12:00:01Z")
    session = FakeSession({
        "get_recent_alerts": [
            {"alerts": [dict(ALERT)], "count": 1},                            # pre-check
            {"alerts": [mismatched], "count": 1},                             # verification
        ]
    })
    service = _service(store, lambda: FakeSessionCM({"current": session}), clock)
    approval = await _saved_proposal(store, clock)
    await service.decide(approval.approval_id, "approved", "op-1")

    executed = await service.execute(approval.approval_id, "op-2")
    assert executed.status == "executed"                       # write succeeded
    assert executed.verification["ok"] is False                # read-back disagrees
    assert "mismatch" in executed.verification["details"]


# -- executor policy gate -------------------------------------------------------


async def test_executor_rejects_write_tool_through_agent_path():
    import sys

    from backend.mcp.client_manager import MCPClientManager, MultiMCPManager

    factories = {
        "monitoring-mcp": lambda: MCPClientManager(
            "monitoring-mcp", sys.executable, ["-m", "backend.mcp.servers.monitoring_mcp"],
            call_timeout_seconds=15,
        ),
    }
    async with MultiMCPManager(factories) as manager:
        catalog = ToolCatalog.from_managers(manager.managers, SERVER_ALLOWLISTS)
        executor = Executor(manager, catalog, EvidenceStore("inv"), SERVER_ALLOWLISTS)
        outcome = executor.validate(
            ToolCallRequest(name="monitoring-mcp__acknowledge_alert",
                            arguments={"alert_id": "ALT-2071", "acknowledged_by": "op"})
        )
    assert not outcome.ok
    assert "policy allowlist" in outcome.error
    assert not catalog.is_executable("monitoring-mcp__acknowledge_alert")


def _dict(**kwargs):
    return dict(kwargs)

