"""Phase 3+ endpoints: health + investigations (poll for results) + Phase 7
approval endpoints (proposal lifecycle; SSE streaming is a later-phase
addition). All approval decisions and executions route through the
ApprovalService — routes never touch approval persistence directly.

Phase 8 Gate C adds root-level `/health` (liveness, dependency-free) and
`/ready` (readiness, 30s cached dependency probe); `GET /api/health` keeps
its existing Phase 3 response contract unchanged.

Phase 10 Gate 2 adds `GET /api/investigations/{id}/events` (SSE): a periodic
read of the LIVE in-memory record — not a push hook from the loop — emitting
a `status` event whenever the status/stage/evidence snapshot changes, a
`terminal` event on completion, and keepalive comments while idle."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse

from backend.agent.service import AgentService
from backend.api.deps import get_service
from backend.api.schemas import (
    ApprovalDecisionRequest,
    ApprovalExecuteRequest,
    InvestigationCreated,
    InvestigationRequest,
    approval_out,
)
from backend.reliability import CircuitOpenError
from backend.security.approval_actions import WRITABLE_ACTIONS
from backend.security.approval_service import (
    ApprovalError,
    ApprovalExpiredError,
    ApprovalInvalidIdentityError,
    ApprovalNotFoundError,
    ApprovalPrecheckFailedError,
    ApprovalTamperedError,
    ApprovalUncertainError,
)

router = APIRouter(prefix="/api")

# Root-level operational endpoints (Phase 8 Gate C). /api/health above is the
# Phase 3 contract and is intentionally untouched by these.
root_router = APIRouter()


@root_router.get("/health")
async def liveness() -> dict:
    """Liveness: the process can serve requests.

    Static by design — it probes nothing (no LLM, PostgreSQL, or MCP), so a
    slow or hung dependency can never make this endpoint slow or hang."""
    return {"status": "ok"}


@root_router.get("/ready")
async def readiness(service: AgentService = Depends(get_service)) -> JSONResponse:
    """Readiness: required dependencies are available.

    Required: LLM provider + every configured MCP server. The Postgres state
    store is informational only (startup() degrades to in-memory operation).
    Probes are cached for 30s with single-flight, so concurrent callers share
    one probe instead of stampeding the dependencies."""
    ready, checks, from_cache = await service.readiness()
    return JSONResponse(
        status_code=200 if ready else 503,
        content={"status": "ready" if ready else "not_ready", "cached": from_cache, "checks": checks},
    )

_VALID_APPROVAL_STATUSES = (
    "proposed", "approved", "rejected", "expired", "superseded",
    "executing", "executed", "failed", "uncertain",
)


def _approval_http_error(exc: ApprovalError) -> HTTPException:
    """Map typed approval failures onto HTTP statuses."""
    if isinstance(exc, ApprovalNotFoundError):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, ApprovalInvalidIdentityError):
        return HTTPException(status_code=422, detail=str(exc))
    if isinstance(exc, (ApprovalExpiredError, ApprovalTamperedError, ApprovalPrecheckFailedError)):
        return HTTPException(status_code=409, detail=str(exc))
    return HTTPException(status_code=409, detail=str(exc))  # conflicts and unknown failures


@router.get("/health")
async def health(service: AgentService = Depends(get_service)) -> dict:
    return await service.health()


@router.post("/investigations", status_code=201)
async def create_investigation(
    body: InvestigationRequest, service: AgentService = Depends(get_service)
) -> InvestigationCreated:
    record = service.start_investigation(body.question.strip())
    return InvestigationCreated(
        investigation_id=record.investigation_id, request_id=record.request_id, status=record.status
    )


@router.get("/investigations/{investigation_id}")
async def get_investigation(
    investigation_id: str, service: AgentService = Depends(get_service)
) -> dict:
    record = await service.get_investigation(investigation_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"unknown investigation {investigation_id!r}")
    return record.dict_for_api()


# -- SSE: investigation event stream (Phase 10 Gate 2) --------------------------

SSE_POLL_SECONDS = 1.0
SSE_KEEPALIVE_SECONDS = 15.0
SSE_MAX_STREAM_SECONDS = 1800.0
_TERMINAL_STATUSES = ("complete", "failed", "interrupted")
_SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "X-Accel-Buffering": "no",  # nginx: never buffer this stream
    "Connection": "keep-alive",
}


def _sse_frame(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _sse_snapshot(record) -> dict:
    """Operator-safe snapshot: identifiers and lifecycle only — no evidence
    payloads, prompts, tool arguments, or internal diagnostics."""
    return {
        "investigation_id": record.investigation_id,
        "status": record.status,
        "stage": record.stage,
        "evidence_count": len(record.evidence_ids),
    }


async def _sse_follow(record) -> AsyncIterator[str]:
    """Yield SSE frames by PERIODICALLY READING the live record (poll, not a
    loop push-hook): transitions between polls coalesce into the next emitted
    snapshot, so the stream is not guaranteed to show every internal
    transition. Ends with a `terminal` event when the record reaches a
    terminal status. Client disconnects cancel the generator at the sleep;
    the `finally` is the stream cleanup point."""
    last_snapshot: dict | None = None
    last_emit = time.monotonic()
    deadline = last_emit + SSE_MAX_STREAM_SECONDS
    try:
        while True:
            snapshot = _sse_snapshot(record)
            if snapshot != last_snapshot:
                yield _sse_frame("status", snapshot)
                last_snapshot = snapshot
                last_emit = time.monotonic()
            if snapshot["status"] in _TERMINAL_STATUSES:
                yield _sse_frame("terminal", snapshot)
                return
            if time.monotonic() - last_emit >= SSE_KEEPALIVE_SECONDS:
                yield ": keepalive\n\n"  # SSE comment frame: ignored by clients
                last_emit = time.monotonic()
            if time.monotonic() >= deadline:
                yield _sse_frame("terminal", {**snapshot, "status": "stream_timeout"})
                return
            await asyncio.sleep(SSE_POLL_SECONDS)
    finally:
        # stream cleanup: nothing is held beyond the record reference; the
        # generator close (client disconnect / cancellation) lands here.
        pass


@router.get("/investigations/{investigation_id}/events")
async def investigation_events(
    investigation_id: str, service: AgentService = Depends(get_service)
) -> StreamingResponse:
    record = await service.get_investigation(investigation_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"unknown investigation {investigation_id!r}")
    # only in-process records can advance; anything loaded from the store is
    # already static (the boot sweep marks restart-orphaned runs interrupted)
    live = service.registry.get(investigation_id) is not None

    async def stream():
        if not live or record.status in _TERMINAL_STATUSES:
            snapshot = _sse_snapshot(record)
            yield _sse_frame("status", snapshot)
            yield _sse_frame("terminal", snapshot)
            return
        async for frame in _sse_follow(record):
            yield frame

    return StreamingResponse(stream(), media_type="text/event-stream", headers=_SSE_HEADERS)


@router.get("/approvals")
async def list_approvals(
    status: str | None = None, service: AgentService = Depends(get_service)
) -> dict:
    if status is not None and status not in _VALID_APPROVAL_STATUSES:
        raise HTTPException(
            status_code=422,
            detail=f"invalid status {status!r}; valid: {', '.join(_VALID_APPROVAL_STATUSES)}",
        )
    approvals = await service.list_approvals(status)
    return {"count": len(approvals), "approvals": [approval_out(a) for a in approvals]}


@router.get("/approvals/{approval_id}")
async def get_approval(
    approval_id: str, service: AgentService = Depends(get_service)
) -> dict:
    record = await service.get_approval(approval_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"unknown approval {approval_id!r}")
    return approval_out(record)


@router.post("/approvals/{approval_id}/decision")
async def decide_approval(
    approval_id: str,
    body: ApprovalDecisionRequest,
    service: AgentService = Depends(get_service),
) -> dict:
    try:
        record = await service.decide_approval(
            approval_id, body.decision, body.decided_by, body.comment
        )
    except ApprovalNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ApprovalInvalidIdentityError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except ApprovalExpiredError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ApprovalError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return approval_out(record)


@router.post("/approvals/{approval_id}/execute")
async def execute_approval(
    approval_id: str,
    body: ApprovalExecuteRequest,
    service: AgentService = Depends(get_service),
) -> dict:
    try:
        record = await service.execute_approval(approval_id, body.executed_by)
    except ApprovalNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ApprovalInvalidIdentityError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except ApprovalExpiredError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ApprovalTamperedError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ApprovalPrecheckFailedError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except (ApprovalError, CircuitOpenError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return approval_out(record)
