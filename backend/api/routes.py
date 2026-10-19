"""Phase 3+ endpoints: health + investigations (poll for results) + Phase 7
approval endpoints (proposal lifecycle; SSE streaming is a later-phase
addition). All approval decisions and executions route through the
ApprovalService — routes never touch approval persistence directly.

Phase 8 Gate C adds root-level `/health` (liveness, dependency-free) and
`/ready` (readiness, 30s cached dependency probe); `GET /api/health` keeps
its existing Phase 3 response contract unchanged."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse

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
