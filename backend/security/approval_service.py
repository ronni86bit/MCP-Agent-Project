"""ApprovalService: the only path from a model proposal to an executed write.

Lifecycle (persisted per Gate 1):
    proposed --decide(approved|rejected)--> approved --execute--> executing
        --write ok--> executed (+ verification recorded)
        --server-side rejection--> failed
        --transport/timeout/cancellation after dispatch--> uncertain
    proposed/approved --TTL elapsed--> expired

Security properties:
- The model can only PROPOSE. Execution requires a persisted `approved` row
  whose `args_hash` still matches — the write always uses the stored
  arguments verbatim; execution requests carry no write arguments.
- `acknowledged_by` is derived from the persisted human decision
  (`decided_by`), never from model output.
- Cancellation before dispatch reverts `executing -> approved` (provably no
  write sent: `dispatched_at IS NULL`); after dispatch it marks `uncertain`.
  Uncertain outcomes are never blindly retried; resolution is read-back
  based and requires a fresh operator label.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable

import structlog
from pydantic import ValidationError

from backend.agent.models import ApprovalRecord
from backend.config import get_settings
from backend.mcp.errors import MCPClientError
from backend.reliability import CircuitOpenError
from backend.security.approval_actions import WRITABLE_ACTIONS, args_hash, eligible_alert_ids

log = structlog.get_logger(__name__)

_MAX_NOTE = 200


class ApprovalError(Exception):
    """Base class for approval lifecycle failures."""


class ApprovalNotFoundError(ApprovalError):
    pass


class ApprovalConflictError(ApprovalError):
    """Lost race or invalid current state for the requested transition."""


class ApprovalExpiredError(ApprovalError):
    pass


class ApprovalTamperedError(ApprovalError):
    """The persisted proposal no longer matches its binding hash."""


class ApprovalUnsupportedError(ApprovalError):
    """The proposed action is not in the write-action registry."""


class ApprovalInvalidIdentityError(ApprovalError):
    """A required operator label is missing or blank."""


class ApprovalPrecheckFailedError(ApprovalError):
    """The pre-dispatch read-back shows the target state changed."""


class ApprovalUncertainError(ApprovalError):
    """The write outcome could not be established (timeout/transport after
    dispatch). The approval row is `uncertain`; resolution is read-back
    based and requires a fresh operator label — never an automatic retry."""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _require_label(label: str | None, field: str) -> str:
    if not label or not label.strip():
        raise ApprovalInvalidIdentityError(f"{field} must be a non-blank operator label")
    return label.strip()


class ApprovalService:
    """Owns the approval lifecycle for registered write actions.

    `monitoring_session_factory` returns an async context manager yielding an
    object with `call_tool(server, tool, arguments)`; the default spawns a
    fresh monitoring-mcp stdio session. One session is used per execution so
    pre-check, write, and verification read-back observe the same in-memory
    fixture store.
    """

    def __init__(
        self,
        store,
        monitoring_session_factory: Callable[[], Any],
        *,
        clock: Callable[[], datetime] = _utcnow,
        execution_timeout_seconds: float | None = None,
    ):
        self.store = store
        self._session_factory = monitoring_session_factory
        self._clock = clock
        self.settings = get_settings()
        self._timeout = execution_timeout_seconds or float(
            getattr(self.settings, "approval_execution_timeout_seconds", 20.0)
        )
        self._write_tasks: set[asyncio.Task] = set()

    # -- proposal ------------------------------------------------------------

    def eligible_alert_ids(self, evidence_records: list[Any]) -> list[str]:
        return eligible_alert_ids(evidence_records)

    async def create_proposal(
        self,
        record: Any,
        evidence_records: list[Any],
        propose_llm_call: Callable[[], Awaitable[dict]],
    ) -> ApprovalRecord:
        """Validate eligibility deterministically, then ask the model to draft
        the proposal; the result is validated again before persistence.

        `propose_llm_call` returns a dict: {tool, arguments{alert_id, note},
        rationale, evidence_ids}. Raises ApprovalUnsupportedError /
        ApprovalPrecheckFailedError on invalid proposals (nothing persisted).
        """
        if record.plan is None:
            raise ApprovalUnsupportedError("investigation has no plan; refusing to propose")
        eligible = self.eligible_alert_ids(evidence_records)
        if not eligible:
            raise ApprovalPrecheckFailedError(
                "no eligible unacknowledged alert found in collected evidence"
            )

        raw = await propose_llm_call()
        tool = str(raw.get("tool", ""))
        if tool not in WRITABLE_ACTIONS:
            raise ApprovalUnsupportedError(
                f"proposed action {tool!r} is not a registered write action"
            )
        arguments = dict(raw.get("arguments") or {})
        alert_id = str(arguments.get("alert_id", ""))
        if alert_id not in eligible:
            raise ApprovalPrecheckFailedError(
                f"proposed alert {alert_id!r} is not an eligible unacknowledged alert "
                f"from the collected evidence; eligible: {eligible}"
            )
        note = arguments.get("note")
        if note is not None and len(str(note)) > _MAX_NOTE:
            raise ApprovalPrecheckFailedError(f"note exceeds {_MAX_NOTE} characters")

        now = self._clock()
        expires_at = now + timedelta(seconds=self.settings.approval_ttl_seconds)
        version = max((p.proposal_version for p in record.proposals), default=0) + 1
        args = {"alert_id": alert_id, "note": str(note) if note is not None else None}
        approval = ApprovalRecord(
            approval_id=f"apr-{uuid.uuid4().hex[:12]}",
            investigation_id=record.investigation_id,
            tool=tool,
            arguments=args,
            status="proposed",
            proposal={
                "tool": tool,
                "arguments": args,
                "rationale": str(raw.get("rationale", ""))[:400],
                "evidence_ids": list(raw.get("evidence_ids", [])),
                "proposal_version": version,
            },
            args_hash=args_hash(
                action=tool,
                target_server=WRITABLE_ACTIONS[tool],
                arguments=args,
                proposal_version=version,
                expires_at=expires_at,
            ),
            proposal_version=version,
            expires_at=expires_at,
            created_at=now,
        )
        await self.store.save_proposal(approval)
        log.info(
            "approval_proposed",
            approval_id=approval.approval_id,
            investigation_id=record.investigation_id,
            tool=tool,
            alert_id=alert_id,
            expires_at=expires_at.isoformat(),
        )
        return approval

    # -- decision ------------------------------------------------------------

    async def decide(
        self,
        approval_id: str,
        decision: str,
        decided_by: str,
        comment: str | None = None,
    ) -> ApprovalRecord:
        operator = _require_label(decided_by, "decided_by")
        if decision not in ("approved", "rejected"):
            raise ApprovalError(f"invalid decision {decision!r}")
        row = await self.store.get_approval(approval_id)
        if row is None:
            raise ApprovalNotFoundError(f"unknown approval {approval_id!r}")
        if row.status not in ("proposed",):
            raise ApprovalConflictError(f"approval {approval_id!r} is {row.status!r}, not 'proposed'")
        if row.expires_at is not None and self._clock() > row.expires_at.replace(tzinfo=row.expires_at.tzinfo):
            expired = await self.store.mark_expired(approval_id)
            raise ApprovalExpiredError(
                f"approval {approval_id!r} expired at {row.expires_at.isoformat()}"
                + ("" if expired is None else "; row marked expired")
            )
        decided = await self.store.decide_approval(
            approval_id, decision, operator, comment, at=self._clock()
        )
        if decided is None:
            raise ApprovalConflictError(f"approval {approval_id!r} was decided concurrently")
        log.info(
            "approval_decided",
            approval_id=approval_id, decision=decision, decided_by=operator,
        )
        return decided

    # -- execution -----------------------------------------------------------

    async def execute(self, approval_id: str, executed_by: str) -> ApprovalRecord:
        operator = _require_label(executed_by, "executed_by")
        row = await self.store.get_approval(approval_id)
        if row is None:
            raise ApprovalNotFoundError(f"unknown approval {approval_id!r}")

        now = self._clock()
        if row.expires_at is not None and now > row.expires_at.replace(tzinfo=row.expires_at.tzinfo):
            await self.store.mark_expired(approval_id)
            raise ApprovalExpiredError(f"approval {approval_id!r} expired; refusing to execute")

        recomputed = args_hash(
            action=row.tool,
            target_server=WRITABLE_ACTIONS.get(row.tool, row.tool),
            arguments=row.arguments,
            proposal_version=row.proposal_version,
            expires_at=row.expires_at.replace(tzinfo=row.expires_at.tzinfo) if row.expires_at else now,
        )
        if row.args_hash is None or row.args_hash != recomputed:
            await self.store.fail_approval(approval_id, "parameter hash mismatch (tampered?)")
            raise ApprovalTamperedError(
                f"approval {approval_id!r} failed its binding-hash check; refusing to execute"
            )

        begun = await self.store.begin_execution(approval_id, operator)
        if begun is None:
            raise ApprovalConflictError(
                f"approval {approval_id!r} is not in 'approved' state; refusing to execute"
            )

        target_server = WRITABLE_ACTIONS[row.tool]
        session = await self._session_factory().__aenter__()
        try:
            # pre-dispatch read-back: the target must still be unacknowledged.
            # A cancellation here is provably pre-dispatch (dispatched_at IS
            # NULL) and reverts the approval to `approved`.
            precheck = await asyncio.wait_for(
                session.call_tool(target_server, "get_recent_alerts",
                                  {"window_start": "2026-09-06",
                                   "window_end": "2026-10-05", "limit": 100}),
                timeout=self._timeout,
            )
            alerts = _alerts_of(precheck)
            target = next((a for a in alerts if a.get("id") == row.arguments.get("alert_id")), None)
            if target is None:
                await self.store.finish_execution(
                    approval_id, "failed",
                    {"reason": f"alert {row.arguments.get('alert_id')!r} not found at execution time"},
                )
                raise ApprovalPrecheckFailedError("proposed alert no longer exists")
            if target.get("acknowledged"):
                await self.store.mark_superseded(approval_id)
                raise ApprovalPrecheckFailedError(
                    f"alert {row.arguments.get('alert_id')!r} is already acknowledged"
                )

            # ---- dispatch boundary --------------------------------------
            await self.store.mark_dispatched(approval_id)
            write_args = {
                "alert_id": row.arguments["alert_id"],
                "acknowledged_by": row.decided_by or operator,
                "note": row.arguments.get("note"),
            }
            write_task = asyncio.create_task(
                self._dispatch_write(approval_id, session, target_server, write_args)
            )
            self._track(write_task)
            try:
                await asyncio.wait_for(asyncio.shield(write_task), timeout=self._timeout)
            except asyncio.CancelledError:
                # post-dispatch cancellation: outcome cannot be established;
                # the shielded task keeps running and records its own outcome
                await self.store.mark_uncertain(
                    approval_id, "execution cancelled after dispatch", at=self._clock()
                )
                raise
            except (asyncio.TimeoutError, MCPClientError, CircuitOpenError) as exc:
                await self.store.mark_uncertain(
                    approval_id, f"write outcome unknown: {str(exc)[:200]}", at=self._clock()
                )
                raise ApprovalUncertainError(
                    f"acknowledgement write outcome is unknown: {str(exc)[:160]}"
                ) from exc

            final = await write_task
            if final.get("status") == "failed":
                raise ApprovalError(
                    f"acknowledgement write failed: {str(final.get('reason', ''))[:200]}"
                )
            log.info(
                "approval_executed",
                approval_id=approval_id,
                tool=row.tool,
                verification_ok=(final.get("verification") or {}).get("ok"),
            )
            return await self.store.get_approval(approval_id)
        except asyncio.CancelledError:
            # pre-dispatch cancellation: provably no write sent (revert is
            # guarded by dispatched_at IS NULL) -> back to approved
            reverted = await self.store.revert_to_approved(approval_id)
            if reverted is None:
                await self.store.mark_uncertain(
                    approval_id, "execution cancelled after dispatch", at=self._clock()
                )
            raise
        except (MCPClientError, CircuitOpenError) as exc:
            await self.store.finish_execution(approval_id, "failed", {"reason": str(exc)[:300]})
            raise ApprovalError(f"execution failed: {str(exc)[:200]}") from exc
        finally:
            await session.__aexit__(None, None, None)

    async def _dispatch_write(
        self, approval_id: str, session: Any, target_server: str, write_args: dict
    ) -> dict:
        """Tracked write-task body: performs the write and records the outcome
        (executed + verification, or failed) even when the awaiting handler is
        cancelled. Never raises: every exception is observed and recorded."""
        try:
            result = await session.call_tool(target_server, "acknowledge_alert", write_args)
        except asyncio.CancelledError:
            raise  # process shutdown: the startup sweep marks the row uncertain
        except MCPClientError as exc:
            await self.store.finish_execution(approval_id, "failed", {"reason": str(exc)[:300]})
            return {"status": "failed", "reason": str(exc)[:200]}
        if getattr(result, "is_error", False):
            reason = _text_of(result)[:300]
            await self.store.finish_execution(approval_id, "failed", {"reason": reason})
            return {"status": "failed", "reason": reason}

        alerts_after = await asyncio.wait_for(
            session.call_tool(target_server, "get_recent_alerts",
                              {"window_start": "2026-09-06",
                               "window_end": "2026-10-05", "limit": 100}),
            timeout=self._timeout,
        )
        verification = verify_acknowledgement(
            _alerts_of(alerts_after), write_args["alert_id"], write_args["acknowledged_by"]
        )
        await self.store.finish_execution(
            approval_id, "executed", {"write_args": write_args}, verification
        )
        log.info("approval_write_recorded", approval_id=approval_id,
                 verification_ok=verification["ok"])
        return {"status": "executed", "verification": verification}

    async def shutdown(self) -> None:
        """Graceful shutdown: in-flight approval writes finish and persist
        their outcome before anything is cancelled."""
        if self._write_tasks:
            await asyncio.gather(*self._write_tasks, return_exceptions=True)

    # -- uncertain recovery ----------------------------------------------------

    async def resolve_uncertain(self, approval_id: str, recovery_executed_by: str) -> ApprovalRecord:
        operator = _require_label(recovery_executed_by, "executed_by")
        row = await self.store.get_approval(approval_id)
        if row is None:
            raise ApprovalNotFoundError(f"unknown approval {approval_id!r}")
        if row.status != "uncertain":
            raise ApprovalConflictError(
                f"approval {approval_id!r} is {row.status!r}, not 'uncertain'"
            )
        session = await self._session_factory().__aenter__()
        try:
            alerts = await asyncio.wait_for(
                session.call_tool(WRITABLE_ACTIONS[row.tool], "get_recent_alerts",
                                  {"window_start": "2026-09-06",
                                   "window_end": "2026-10-05", "limit": 100}),
                timeout=self._timeout,
            )
            target = next(
                (a for a in _alerts_of(alerts) if a.get("id") == row.arguments.get("alert_id")),
                None,
            )
            if target is not None and target.get("acknowledged"):
                resolved = await self.store.resolve_uncertain(
                    approval_id, "executed", operator,
                    {"recovery": {"executed_by": operator, "resolved_from": "uncertain"}},
                    {"ok": True, "details": "acknowledgement confirmed by read-back"},
                )
            else:
                resolved = await self.store.resolve_uncertain(
                    approval_id, "failed", operator,
                    {"recovery": {"executed_by": operator, "resolved_from": "uncertain"},
                     "reason": "read-back shows the alert was never acknowledged"},
                    None,
                )
            log.info("uncertainty_resolved", approval_id=approval_id,
                     status=resolved.status if resolved else None)
            return resolved
        finally:
            await session.__aexit__(None, None, None)

    # -- task tracking ---------------------------------------------------------

    def _track(self, task: asyncio.Task) -> None:
        self._write_tasks.add(task)
        task.add_done_callback(self._write_tasks.discard)

    async def shutdown(self) -> None:
        """Graceful shutdown: let in-flight approval writes finish and persist
        before anything is cancelled."""
        if self._write_tasks:
            await asyncio.gather(*self._write_tasks, return_exceptions=True)


def _alerts_of(result: Any) -> list[dict]:
    structured = getattr(result, "structured_content", None)
    if structured is None:
        import json

        try:
            structured = json.loads("".join(
                getattr(block, "text", "") for block in getattr(result, "content", []) or []
            ))
        except (json.JSONDecodeError, TypeError):
            structured = {}
    alerts = structured.get("alerts") if isinstance(structured, dict) else None
    return alerts or []


def _text_of(result: Any) -> str:
    return "".join(getattr(block, "text", "") for block in getattr(result, "content", []) or [])


def verify_acknowledgement(alerts: list[dict], alert_id: str, expected_by: str) -> dict:
    """Pure verification: the alert must exist, be acknowledged, and record the
    deciding operator."""
    target = next((a for a in alerts if a.get("id") == alert_id), None)
    if target is None:
        return {"ok": False, "details": f"alert {alert_id!r} not present in read-back"}
    if not target.get("acknowledged"):
        return {"ok": False, "details": f"alert {alert_id!r} is not acknowledged in read-back"}
    if target.get("acknowledged_by") != expected_by:
        return {
            "ok": False,
            "details": (f"acknowledged_by mismatch: expected {expected_by!r}, "
                        f"read back {target.get('acknowledged_by')!r}"),
        }
    return {"ok": True, "details": f"acknowledged at {target.get('acknowledged_at')}"}


def _payload_of(result: Any) -> dict:
    structured = getattr(result, "structured_content", None)
    return structured if isinstance(structured, dict) else {"raw": str(result)[:200]}
