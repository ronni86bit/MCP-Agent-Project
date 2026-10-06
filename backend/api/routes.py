"""Phase 3 endpoints: health + investigation create/get (poll for results;
SSE streaming is a later-phase addition)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from backend.agent.service import AgentService
from backend.api.deps import get_service
from backend.api.schemas import InvestigationCreated, InvestigationRequest

router = APIRouter(prefix="/api")


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
    record = service.registry.get(investigation_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"unknown investigation {investigation_id!r}")
    return record.dict_for_api()
