"""InvestigationStore: write-through persistence for investigation records.

Every entity of an investigation lives in its own table (investigations,
plans per version, steps, tool_calls, evidence, reports) so retrieval
reassembles the full record after a backend restart. `persist_record` is an
idempotent snapshot sync invoked by the loop at each transition; state
transitions are additionally recorded in the append-only
`investigation_events` log. Persistence failures never crash an investigation
(the loop degrades to in-memory operation and logs a warning).

Recovery is conservative: the startup sweep marks still-running
investigations as `interrupted` and tool calls recorded as `running` as
`unknown` — uncertain external operations are never re-executed blindly.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Any

import asyncpg
import structlog

from backend.agent.evidence import Evidence
from backend.agent.models import (
    ApprovalRecord,
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
from backend.agent.persistence.schema import SCHEMA_NAME, ensure_schema
from backend.agent.state import InvestigationRecord
from backend.config import get_settings

log = structlog.get_logger(__name__)

DEFAULT_ADMIN_DSN = "postgresql://ops_admin:ops_admin_dev@localhost:5433/ops_agent"


def _dt(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _json(value: Any) -> str:
    return json.dumps(value, default=str)


_APPROVAL_COLUMNS = """approval_id, investigation_id, tool, arguments, status, proposal,
    args_hash, proposal_version, expires_at, created_at, decided_by, decision_comment,
    decided_at, executed_by, dispatched_at, executed_at, execution_result, verification"""


def _approval_from_row(row) -> ApprovalRecord:
    return ApprovalRecord(
        approval_id=row["approval_id"],
        investigation_id=row["investigation_id"],
        tool=row["tool"],
        arguments=json.loads(row["arguments"]),
        status=row["status"],
        proposal=json.loads(row["proposal"]),
        args_hash=row["args_hash"],
        proposal_version=row["proposal_version"] or 1,
        expires_at=row["expires_at"],
        created_at=row["created_at"],
        decided_by=row["decided_by"],
        decision_comment=row["decision_comment"],
        decided_at=row["decided_at"],
        executed_by=row["executed_by"],
        dispatched_at=row["dispatched_at"],
        executed_at=row["executed_at"],
        execution_result=json.loads(row["execution_result"]),
        verification=json.loads(row["verification"]) if row["verification"] is not None else None,
    )


async def _record_event(conn, investigation_id: str, event: str, payload: dict) -> None:
    """Insert an audit event inside the caller's transaction."""
    await conn.execute(
        f"INSERT INTO {SCHEMA_NAME}.investigation_events "
        "(investigation_id, event, payload) VALUES ($1,$2,$3)",
        investigation_id,
        event,
        _json(payload or {}),
    )




class ApprovalTransitionError(Exception):
    """An approval transition was requested from a state that forbids it.

    Kept for API-layer mapping in later gates; the store itself signals lost
    races by returning None."""


class InvestigationStore:
    """AsyncPG-backed store; pool created lazily so construction never blocks."""

    def __init__(self, dsn: str | None = None):
        self._dsn = dsn or get_settings().ops_admin_dsn or DEFAULT_ADMIN_DSN
        self._pool: asyncpg.Pool | None = None
        self._ready = False

    async def _conn(self) -> asyncpg.Connection:
        if self._pool is None:
            self._pool = await asyncpg.create_pool(self._dsn, min_size=1, max_size=3, command_timeout=20)
        return await self._pool.acquire()

    @staticmethod
    async def _release(pool: asyncpg.Pool, conn: asyncpg.Connection) -> None:
        await pool.release(conn)

    async def ensure_schema(self) -> int:
        conn = await self._conn()
        try:
            version = await ensure_schema(conn)
            self._ready = True
            return version
        finally:
            await self._release(self._pool, conn)

    # -- write path ----------------------------------------------------------

    async def persist_record(self, record: InvestigationRecord) -> None:
        if not self._ready:
            await self.ensure_schema()
        conn = await self._conn()
        try:
            async with conn.transaction():
                await conn.execute(
                    f"""
                    INSERT INTO {SCHEMA_NAME}.investigations
                        (investigation_id, request_id, question, status, stage, understanding,
                         failure_reason, warnings, states_visited, llm_calls,
                         created_at, updated_at, finished_at)
                    VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13)
                    ON CONFLICT (investigation_id) DO UPDATE SET
                        status=EXCLUDED.status, stage=EXCLUDED.stage,
                        understanding=EXCLUDED.understanding,
                        failure_reason=EXCLUDED.failure_reason, warnings=EXCLUDED.warnings,
                        states_visited=EXCLUDED.states_visited, llm_calls=EXCLUDED.llm_calls,
                        updated_at=EXCLUDED.updated_at, finished_at=EXCLUDED.finished_at
                    """,
                    record.investigation_id, record.request_id, record.question,
                    record.status, record.stage,
                    _json(record.understanding.model_dump()) if record.understanding else None,
                    record.failure_reason, _json(record.warnings), _json(record.states_visited),
                    record.llm_calls, _dt(record.created_at), _dt(record.updated_at), _dt(record.finished_at),
                )
                for info in record.plan_history:
                    await conn.execute(
                        f"""
                        INSERT INTO {SCHEMA_NAME}.plans
                            (investigation_id, version, goal, steps, expected_evidence,
                             completion_criteria, revision_reason, superseded)
                        VALUES ($1,$2,$3,$4,$5,$6,$7,$8)
                        ON CONFLICT (investigation_id, version) DO UPDATE SET
                            goal=EXCLUDED.goal, steps=EXCLUDED.steps,
                            expected_evidence=EXCLUDED.expected_evidence,
                            completion_criteria=EXCLUDED.completion_criteria,
                            revision_reason=EXCLUDED.revision_reason,
                            superseded=EXCLUDED.superseded
                        """,
                        record.investigation_id, info.version, info.plan.goal,
                        _json([step.model_dump() for step in info.plan.steps]),
                        _json(info.plan.expected_evidence), info.plan.completion_criteria,
                        info.revision_reason, info.superseded,
                    )
                for step in record.steps:
                    await conn.execute(
                        f"""
                        INSERT INTO {SCHEMA_NAME}.steps
                            (investigation_id, step_index, plan_version, plan_step_id,
                             action, status, evidence_ids)
                        VALUES ($1,$2,$3,$4,$5,$6,$7)
                        ON CONFLICT (investigation_id, step_index) DO UPDATE SET
                            plan_version=EXCLUDED.plan_version, plan_step_id=EXCLUDED.plan_step_id,
                            action=EXCLUDED.action, status=EXCLUDED.status,
                            evidence_ids=EXCLUDED.evidence_ids
                        """,
                        record.investigation_id, step.index, step.plan_version,
                        step.plan_step_id, step.action, step.status, _json(step.evidence_ids),
                    )
                for index, obs in enumerate(record.tool_calls, start=1):
                    await conn.execute(
                        f"""
                        INSERT INTO {SCHEMA_NAME}.tool_calls
                            (investigation_id, call_index, step_index, server, tool, arguments,
                             attempt, status, error_class, error, duration_ms, evidence_id, at)
                        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13)
                        ON CONFLICT (investigation_id, call_index) DO UPDATE SET
                            step_index=EXCLUDED.step_index, server=EXCLUDED.server, tool=EXCLUDED.tool,
                            arguments=EXCLUDED.arguments, attempt=EXCLUDED.attempt,
                            status=EXCLUDED.status, error_class=EXCLUDED.error_class,
                            error=EXCLUDED.error, duration_ms=EXCLUDED.duration_ms,
                            evidence_id=EXCLUDED.evidence_id, at=EXCLUDED.at
                        """,
                        record.investigation_id, index, obs.step,
                        obs.tool.split("__", 1)[0], obs.tool.split("__", 1)[-1],
                        _json(obs.arguments), obs.attempt, obs.status,
                        obs.error_class, obs.error, obs.duration_ms, obs.evidence_id, _dt(obs.at),
                    )
                for item in record.evidence:
                    await conn.execute(
                        f"""
                        INSERT INTO {SCHEMA_NAME}.evidence
                            (investigation_id, evidence_id, server, tool_name, step,
                             arguments, summary, at)
                        VALUES ($1,$2,$3,$4,$5,$6,$7,$8)
                        ON CONFLICT (investigation_id, evidence_id) DO UPDATE SET
                            server=EXCLUDED.server, tool_name=EXCLUDED.tool_name, step=EXCLUDED.step,
                            arguments=EXCLUDED.arguments, summary=EXCLUDED.summary, at=EXCLUDED.at
                        """,
                        record.investigation_id, item.evidence_id, item.server, item.tool_name,
                        item.step, _json(item.arguments), _json(item.summary), _dt(item.at),
                    )
                if record.report is not None and record.verification is not None:
                    await conn.execute(
                        f"""
                        INSERT INTO {SCHEMA_NAME}.reports (investigation_id, report, verification)
                        VALUES ($1,$2,$3)
                        ON CONFLICT (investigation_id) DO UPDATE SET
                            report=EXCLUDED.report, verification=EXCLUDED.verification
                        """,
                        record.investigation_id,
                        _json(record.report.model_dump(mode="json")),
                        _json(record.verification.model_dump(mode="json")),
                    )
        finally:
            await self._release(self._pool, conn)

    async def append_event(self, investigation_id: str, event: str, payload: dict | None = None) -> None:
        if not self._ready:
            return
        conn = await self._conn()
        try:
            await conn.execute(
                f"INSERT INTO {SCHEMA_NAME}.investigation_events "
                "(investigation_id, event, payload) VALUES ($1,$2,$3)",
                investigation_id, event, _json(payload or {}),
            )
        finally:
            await self._release(self._pool, conn)

    # -- recovery --------------------------------------------------------------

    async def mark_interrupted(self) -> list[str]:
        """Startup sweep: resolve uncertainty conservatively after a restart.

        running investigations -> interrupted; tool calls stuck in `running`
        -> `unknown` (never re-executed blindly); steps mid-flight -> skipped.
        Returns the affected investigation ids.
        """
        if not self._ready:
            await self.ensure_schema()
        conn = await self._conn()
        try:
            async with conn.transaction():
                affected = [
                    row["investigation_id"]
                    for row in await conn.fetch(
                        f"SELECT investigation_id FROM {SCHEMA_NAME}.investigations WHERE status = 'running'"
                    )
                ]
                # orphaned in-flight approvals -> uncertain: transactional
                # events + update, idempotent, never touches terminal states
                await conn.execute(
                    f"""
                    INSERT INTO {SCHEMA_NAME}.investigation_events
                        (investigation_id, event, payload)
                    SELECT a.investigation_id, 'approval_interrupted',
                           jsonb_build_object('approval_id', a.approval_id,
                                              'reason', 'backend restart')
                    FROM {SCHEMA_NAME}.approvals a
                    WHERE a.status = 'executing'
                    """,
                )
                await conn.execute(
                    f"""
                    UPDATE {SCHEMA_NAME}.approvals
                    SET status = 'uncertain',
                        execution_result = COALESCE(execution_result, '{{}}'::jsonb)
                            || '{{"uncertain_reason": "backend restart"}}'::jsonb
                    WHERE status = 'executing'
                    """,
                )
                if not affected:
                    return []
                await conn.execute(
                    f"UPDATE {SCHEMA_NAME}.tool_calls SET status='unknown' "
                    f"WHERE status='running' AND investigation_id = ANY($1)",
                    affected,
                )
                await conn.execute(
                    f"UPDATE {SCHEMA_NAME}.steps SET status='skipped' "
                    f"WHERE status='running' AND investigation_id = ANY($1)",
                    affected,
                )
                await conn.execute(
                    f"""
                    UPDATE {SCHEMA_NAME}.investigations
                    SET status='interrupted', stage='interrupted',
                        failure_reason='backend restarted while the investigation was running',
                        finished_at=now(), updated_at=now()
                    WHERE status='running' AND investigation_id = ANY($1)
                    """,
                    affected,
                )
                for investigation_id in affected:
                    await conn.execute(
                        f"INSERT INTO {SCHEMA_NAME}.investigation_events "
                        "(investigation_id, event, payload) VALUES ($1,'interrupted',$2)",
                        investigation_id,
                        _json({"reason": "backend restart", "policy": "no blind re-execution"}),
                    )
            log.warning("interrupted_investigations_recovered", count=len(affected), ids=affected)
            return affected
        finally:
            await self._release(self._pool, conn)

    # -- read path -------------------------------------------------------------

    async def load_investigation(self, investigation_id: str) -> InvestigationRecord | None:
        if not self._ready:
            await self.ensure_schema()
        conn = await self._conn()
        try:
            row = await conn.fetchrow(
                f"SELECT * FROM {SCHEMA_NAME}.investigations WHERE investigation_id = $1",
                investigation_id,
            )
            if row is None:
                return None
            record = InvestigationRecord(
                investigation_id=row["investigation_id"],
                request_id=row["request_id"],
                question=row["question"],
                status=row["status"],
                stage=row["stage"],
                understanding=Understanding.model_validate(json.loads(row["understanding"]))
                if row["understanding"] else None,
                failure_reason=row["failure_reason"],
                warnings=json.loads(row["warnings"]),
                states_visited=json.loads(row["states_visited"]),
                llm_calls=row["llm_calls"],
                created_at=row["created_at"],
                updated_at=row["updated_at"],
                finished_at=row["finished_at"],
            )
            for prow in await conn.fetch(
                f"SELECT * FROM {SCHEMA_NAME}.plans WHERE investigation_id = $1 ORDER BY version",
                investigation_id,
            ):
                plan = Plan(
                    goal=prow["goal"],
                    steps=[PlanStep.model_validate(s) for s in json.loads(prow["steps"])],
                    expected_evidence=json.loads(prow["expected_evidence"]),
                    completion_criteria=prow["completion_criteria"],
                )
                record.plan_history.append(
                    PlanVersionInfo(
                        version=prow["version"], plan=plan,
                        revision_reason=prow["revision_reason"], superseded=prow["superseded"],
                    )
                )
                if not prow["superseded"]:
                    record.plan, record.plan_version = plan, prow["version"]
            for srow in await conn.fetch(
                f"SELECT * FROM {SCHEMA_NAME}.steps WHERE investigation_id = $1 ORDER BY step_index",
                investigation_id,
            ):
                record.steps.append(
                    StepRecord(
                        index=srow["step_index"], plan_version=srow["plan_version"],
                        plan_step_id=srow["plan_step_id"], action=srow["action"],
                        status=srow["status"], evidence_ids=json.loads(srow["evidence_ids"]),
                    )
                )
            for orow in await conn.fetch(
                f"SELECT * FROM {SCHEMA_NAME}.tool_calls WHERE investigation_id = $1 ORDER BY call_index",
                investigation_id,
            ):
                record.tool_calls.append(
                    ToolCallObservation(
                        step=orow["step_index"], tool=f"{orow['server']}__{orow['tool']}"
                        if orow["server"] and "__" not in orow["tool"] else orow["tool"],
                        arguments=json.loads(orow["arguments"]), attempt=orow["attempt"],
                        status=orow["status"], error_class=orow["error_class"], error=orow["error"],
                        duration_ms=orow["duration_ms"], evidence_id=orow["evidence_id"],
                        at=orow["at"],
                    )
                )
            for erow in await conn.fetch(
                f"SELECT * FROM {SCHEMA_NAME}.evidence WHERE investigation_id = $1 ORDER BY evidence_id",
                investigation_id,
            ):
                record.evidence.append(
                    Evidence(
                        evidence_id=erow["evidence_id"], investigation_id=erow["investigation_id"],
                        server=erow["server"], tool_name=erow["tool_name"], step=erow["step"],
                        arguments=json.loads(erow["arguments"]), summary=json.loads(erow["summary"]),
                        at=erow["at"],
                    )
                )
                record.evidence_ids.append(erow["evidence_id"])
            rrow = await conn.fetchrow(
                f"SELECT * FROM {SCHEMA_NAME}.reports WHERE investigation_id = $1", investigation_id
            )
            if rrow is not None:
                record.report = InvestigationReport.model_validate(json.loads(rrow["report"]))
                record.verification = VerificationResult.model_validate(json.loads(rrow["verification"]))
            return record
        finally:
            await self._release(self._pool, conn)

    async def close(self) -> None:
        if self._pool is not None:
            try:
                await asyncio.wait_for(self._pool.close(), timeout=10)
            except asyncio.TimeoutError:
                await self._pool.terminate()
            self._pool = None

    # -- approvals (Phase 7, Gate 1) ------------------------------------------
    #
    # Every state transition is a single conditional UPDATE executed in the
    # same transaction as its audit event: either both commit or neither does.
    # Lost races (another decision/execution won) return None — callers map
    # that to a typed conflict. Cancellation/uncertain paths never mark
    # success: `mark_uncertain` and `revert_to_approved` are the only exits
    # from `executing` besides a terminal result.

    async def save_proposal(self, approval: ApprovalRecord) -> None:
        if not self._ready:
            await self.ensure_schema()
        conn = await self._conn()
        try:
            async with conn.transaction():
                await conn.execute(
                    f"""
                    INSERT INTO {SCHEMA_NAME}.approvals
                        (approval_id, investigation_id, tool, arguments, status, proposal,
                         args_hash, proposal_version, expires_at, created_at)
                    VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
                    ON CONFLICT (approval_id) DO UPDATE SET
                        tool=EXCLUDED.tool, arguments=EXCLUDED.arguments, status=EXCLUDED.status,
                        proposal=EXCLUDED.proposal, args_hash=EXCLUDED.args_hash,
                        proposal_version=EXCLUDED.proposal_version, expires_at=EXCLUDED.expires_at
                    """,
                    approval.approval_id, approval.investigation_id, approval.tool,
                    _json(approval.arguments), approval.status, _json(approval.proposal),
                    approval.args_hash, approval.proposal_version, _dt(approval.expires_at),
                    _dt(approval.created_at),
                )
                await _record_event(
                    conn, approval.investigation_id, "proposal_created",
                    {
                        "approval_id": approval.approval_id,
                        "tool": approval.tool,
                        "arguments": approval.arguments,
                        "args_hash": approval.args_hash,
                        "expires_at": approval.expires_at.isoformat() if approval.expires_at else None,
                        "proposal_version": approval.proposal_version,
                    },
                )
        finally:
            await self._release(self._pool, conn)

    async def get_approval(self, approval_id: str) -> ApprovalRecord | None:
        if not self._ready:
            await self.ensure_schema()
        conn = await self._conn()
        try:
            row = await conn.fetchrow(
                f"SELECT {_APPROVAL_COLUMNS} FROM {SCHEMA_NAME}.approvals WHERE approval_id = $1",
                approval_id,
            )
            return _approval_from_row(row) if row is not None else None
        finally:
            await self._release(self._pool, conn)

    async def decide_approval(
        self,
        approval_id: str,
        decision: str,
        decided_by: str,
        comment: str | None = None,
        at: datetime | None = None,
    ) -> ApprovalRecord | None:
        """proposed -> approved | rejected. Returns None when the approval is
        not in `proposed` (lost race, already decided, or unknown id)."""
        if decision not in ("approved", "rejected"):
            raise ValueError(f"invalid decision {decision!r}; expected 'approved' or 'rejected'")
        if not decided_by or not decided_by.strip():
            raise ValueError("decided_by must be a non-blank operator label")
        if not self._ready:
            await self.ensure_schema()
        conn = await self._conn()
        try:
            async with conn.transaction():
                row = await conn.fetchrow(
                    f"""
                    UPDATE {SCHEMA_NAME}.approvals
                    SET status=$1, decided_by=$2, decision_comment=$3, decided_at=$4
                    WHERE approval_id=$5 AND status='proposed'
                    RETURNING {_APPROVAL_COLUMNS}
                    """,
                    decision, decided_by.strip(), comment,
                    _dt(at or datetime.now(timezone.utc)), approval_id,
                )
                if row is None:
                    return None
                await _record_event(
                    conn, row["investigation_id"], "proposal_decided",
                    {
                        "approval_id": approval_id, "decision": decision,
                        "decided_by": decided_by.strip(), "comment": comment,
                    },
                )
                return _approval_from_row(row)
        finally:
            await self._release(self._pool, conn)

    async def begin_execution(
        self, approval_id: str, executed_by: str, at: datetime | None = None
    ) -> ApprovalRecord | None:
        """approved -> executing. Returns None on a lost race (another executor
        already holds the transition) or when the approval is not approved."""
        if not executed_by or not executed_by.strip():
            raise ValueError("executed_by must be a non-blank operator label")
        if not self._ready:
            await self.ensure_schema()
        conn = await self._conn()
        try:
            async with conn.transaction():
                row = await conn.fetchrow(
                    f"""
                    UPDATE {SCHEMA_NAME}.approvals
                    SET status='executing', executed_by=$2, executed_at=$3
                    WHERE approval_id=$1 AND status='approved'
                    RETURNING {_APPROVAL_COLUMNS}
                    """,
                    approval_id, executed_by.strip(),
                    _dt(at or datetime.now(timezone.utc)),
                )
                if row is None:
                    return None
                await _record_event(
                    conn, row["investigation_id"], "execution_started",
                    {"approval_id": approval_id, "executed_by": executed_by.strip()},
                )
                return _approval_from_row(row)
        finally:
            await self._release(self._pool, conn)

    async def mark_dispatched(self, approval_id: str, at: datetime | None = None) -> None:
        """Record that the write request has been dispatched (this timestamp is
        what distinguishes pre-dispatch from post-dispatch cancellation)."""
        if not self._ready:
            await self.ensure_schema()
        conn = await self._conn()
        try:
            await conn.execute(
                f"UPDATE {SCHEMA_NAME}.approvals SET dispatched_at=$2 "
                "WHERE approval_id=$1 AND status='executing'",
                approval_id, _dt(at or datetime.now(timezone.utc)),
            )
        finally:
            await self._release(self._pool, conn)

    async def mark_uncertain(
        self, approval_id: str, reason: str, at: datetime | None = None
    ) -> ApprovalRecord | None:
        """executing -> uncertain: the write outcome could not be established.
        Never counted as success or availability failure; resolvable only by
        read-back recovery."""
        if not self._ready:
            await self.ensure_schema()
        conn = await self._conn()
        try:
            async with conn.transaction():
                row = await conn.fetchrow(
                    f"""
                    UPDATE {SCHEMA_NAME}.approvals
                    SET status='uncertain',
                        execution_result = COALESCE(execution_result, '{{}}'::jsonb) || $2::jsonb
                    WHERE approval_id=$1 AND status='executing'
                    RETURNING {_APPROVAL_COLUMNS}
                    """,
                    approval_id,
                    _json({"uncertain_reason": reason[:300],
                           "at": (at or datetime.now(timezone.utc)).isoformat()}),
                )
                if row is None:
                    return None
                await _record_event(
                    conn, row["investigation_id"], "execution_uncertain",
                    {"approval_id": approval_id, "reason": reason[:300]},
                )
                return _approval_from_row(row)
        finally:
            await self._release(self._pool, conn)

    async def revert_to_approved(
        self, approval_id: str, at: datetime | None = None
    ) -> ApprovalRecord | None:
        """executing -> approved: pre-dispatch cancellation; provably no write
        was sent (dispatched_at IS NULL guards the transition)."""
        if not self._ready:
            await self.ensure_schema()
        conn = await self._conn()
        try:
            async with conn.transaction():
                row = await conn.fetchrow(
                    f"""
                    UPDATE {SCHEMA_NAME}.approvals
                    SET status='approved', executed_by=NULL, executed_at=NULL, dispatched_at=NULL
                    WHERE approval_id=$1 AND status='executing' AND dispatched_at IS NULL
                    RETURNING {_APPROVAL_COLUMNS}
                    """,
                    approval_id,
                )
                if row is None:
                    return None
                await _record_event(
                    conn, row["investigation_id"], "execution_reverted",
                    {"approval_id": approval_id, "reason": "cancelled before dispatch"},
                )
                return _approval_from_row(row)
        finally:
            await self._release(self._pool, conn)

    async def finish_execution(
        self,
        approval_id: str,
        terminal_status: str,
        execution_result: dict,
        verification: dict | None = None,
        at: datetime | None = None,
    ) -> ApprovalRecord | None:
        """executing -> executed | failed (the write outcome was established)."""
        if terminal_status not in ("executed", "failed"):
            raise ValueError(f"invalid terminal status {terminal_status!r}")
        if not self._ready:
            await self.ensure_schema()
        conn = await self._conn()
        try:
            async with conn.transaction():
                row = await conn.fetchrow(
                    f"""
                    UPDATE {SCHEMA_NAME}.approvals
                    SET status=$2, executed_at=$3, execution_result=$4, verification=$5
                    WHERE approval_id=$1 AND status IN ('executing', 'uncertain')
                    RETURNING {_APPROVAL_COLUMNS}
                    """,
                    approval_id, terminal_status, _dt(at or datetime.now(timezone.utc)),
                    _json(execution_result),
                    _json(verification) if verification is not None else None,
                )
                if row is None:
                    return None
                await _record_event(
                    conn, row["investigation_id"], "execution_finished",
                    {"approval_id": approval_id, "status": terminal_status},
                )
                return _approval_from_row(row)
        finally:
            await self._release(self._pool, conn)

    async def resolve_uncertain(
        self,
        approval_id: str,
        terminal_status: str,
        recovery_executed_by: str,
        execution_result: dict,
        verification: dict | None = None,
        at: datetime | None = None,
    ) -> ApprovalRecord | None:
        """uncertain -> executed | failed via read-back recovery. The recovery
        operator is recorded separately inside execution_result.recovery; the
        original operator is preserved."""
        if terminal_status not in ("executed", "failed"):
            raise ValueError(f"invalid terminal status {terminal_status!r}")
        if not recovery_executed_by or not recovery_executed_by.strip():
            raise ValueError("recovery executed_by must be a non-blank operator label")
        if not self._ready:
            await self.ensure_schema()
        conn = await self._conn()
        try:
            async with conn.transaction():
                row = await conn.fetchrow(
                    f"""
                    UPDATE {SCHEMA_NAME}.approvals
                    SET status=$2,
                        executed_by=$3,
                        executed_at=$4,
                        execution_result = COALESCE(execution_result, '{{}}'::jsonb) || $5::jsonb,
                        verification=$6
                    WHERE approval_id=$1 AND status='uncertain'
                    RETURNING {_APPROVAL_COLUMNS}
                    """,
                    approval_id, terminal_status, recovery_executed_by.strip(),
                    _dt(at or datetime.now(timezone.utc)),
                    _json({"recovery": {"executed_by": recovery_executed_by.strip(),
                                        "resolved_from": "uncertain"}}),
                    _json(verification) if verification is not None else None,
                )
                if row is None:
                    return None
                await _record_event(
                    conn, row["investigation_id"], "uncertainty_resolved",
                    {"approval_id": approval_id, "status": terminal_status,
                     "recovery_executed_by": recovery_executed_by.strip()},
                )
                return _approval_from_row(row)
        finally:
            await self._release(self._pool, conn)

    async def fail_approval(
        self, approval_id: str, reason: str, at: datetime | None = None
    ) -> ApprovalRecord | None:
        """approved -> failed (refused before dispatch: tampered/expired/stale)
        or executing -> failed (server-side execution error)."""
        if not self._ready:
            await self.ensure_schema()
        conn = await self._conn()
        try:
            async with conn.transaction():
                row = await conn.fetchrow(
                    f"""
                    UPDATE {SCHEMA_NAME}.approvals
                    SET status='failed',
                        execution_result = COALESCE(execution_result, '{{}}'::jsonb) || $2::jsonb,
                        executed_at=COALESCE(executed_at, $3)
                    WHERE approval_id=$1 AND status IN ('approved', 'executing')
                    RETURNING {_APPROVAL_COLUMNS}
                    """,
                    approval_id,
                    _json({"refusal_reason": reason[:300]}),
                    _dt(at or datetime.now(timezone.utc)),
                )
                if row is None:
                    return None
                await _record_event(
                    conn, row["investigation_id"], "execution_refused",
                    {"approval_id": approval_id, "reason": reason[:300]},
                )
                return _approval_from_row(row)
        finally:
            await self._release(self._pool, conn)

    async def mark_expired(self, approval_id: str, at: datetime | None = None) -> ApprovalRecord | None:
        """proposed | approved -> expired (terminal)."""
        if not self._ready:
            await self.ensure_schema()
        conn = await self._conn()
        try:
            async with conn.transaction():
                row = await conn.fetchrow(
                    f"""
                    UPDATE {SCHEMA_NAME}.approvals
                    SET status='expired', decided_at=COALESCE(decided_at, $2)
                    WHERE approval_id=$1 AND status IN ('proposed', 'approved')
                    RETURNING {_APPROVAL_COLUMNS}
                    """,
                    approval_id, _dt(at or datetime.now(timezone.utc)),
                )
                if row is None:
                    return None
                await _record_event(
                    conn, row["investigation_id"], "approval_expired",
                    {"approval_id": approval_id},
                )
                return _approval_from_row(row)
        finally:
            await self._release(self._pool, conn)

    async def mark_superseded(
        self, approval_id: str, reason: str = "target already acknowledged", at: datetime | None = None
    ) -> ApprovalRecord | None:
        """executing -> superseded: the pre-dispatch read-back showed the write
        is no longer needed (no write sent)."""
        if not self._ready:
            await self.ensure_schema()
        conn = await self._conn()
        try:
            async with conn.transaction():
                row = await conn.fetchrow(
                    f"""
                    UPDATE {SCHEMA_NAME}.approvals
                    SET status='superseded',
                        execution_result = COALESCE(execution_result, '{{}}'::jsonb) || $2::jsonb
                    WHERE approval_id=$1 AND status='executing'
                    RETURNING {_APPROVAL_COLUMNS}
                    """,
                    approval_id,
                    _json({"superseded_reason": reason[:300]}),
                )
                if row is None:
                    return None
                await _record_event(
                    conn, row["investigation_id"], "approval_superseded",
                    {"approval_id": approval_id, "reason": reason[:300]},
                )
                return _approval_from_row(row)
        finally:
            await self._release(self._pool, conn)

    async def list_approvals(self, status: str | None = None) -> list[ApprovalRecord]:
        if not self._ready:
            await self.ensure_schema()
        conn = await self._conn()
        try:
            if status is None:
                rows = await conn.fetch(
                    f"SELECT {_APPROVAL_COLUMNS} FROM {SCHEMA_NAME}.approvals ORDER BY created_at"
                )
            else:
                rows = await conn.fetch(
                    f"SELECT {_APPROVAL_COLUMNS} FROM {SCHEMA_NAME}.approvals "
                    "WHERE status = $1 ORDER BY created_at",
                    status,
                )
            return [_approval_from_row(row) for row in rows]
        finally:
            await self._release(self._pool, conn)

    async def mark_executing_approvals_uncertain(self) -> int:
        """Startup-sweep helper: executing -> uncertain (idempotent, terminal-
        safe) with transactional events. Returns the number of rows resolved."""
        if not self._ready:
            await self.ensure_schema()
        conn = await self._conn()
        try:
            async with conn.transaction():
                events = await conn.fetch(
                    f"""
                    INSERT INTO {SCHEMA_NAME}.investigation_events
                        (investigation_id, event, payload)
                    SELECT a.investigation_id, 'approval_interrupted',
                           jsonb_build_object('approval_id', a.approval_id,
                                              'reason', 'backend restart')
                    FROM {SCHEMA_NAME}.approvals a
                    WHERE a.status = 'executing'
                    RETURNING investigation_id
                    """,
                )
                status = await conn.execute(
                    f"UPDATE {SCHEMA_NAME}.approvals SET status='uncertain' WHERE status='executing'"
                )
                count = int(status.split()[-1])
                log.info("executing_approvals_marked_uncertain", count=count, events=len(events))
                return count
        finally:
            await self._release(self._pool, conn)
