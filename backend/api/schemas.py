"""Request/response payloads for the Phase 3 API."""

from __future__ import annotations

from pydantic import BaseModel, Field


class InvestigationRequest(BaseModel):
    question: str = Field(min_length=5, max_length=1000)


class InvestigationCreated(BaseModel):
    investigation_id: str
    request_id: str
    status: str
