"""Request/response payloads for the Phase 3+ API."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from backend.agent.models import ApprovalRecord


class InvestigationRequest(BaseModel):
    question: str = Field(min_length=5, max_length=1000)


class InvestigationCreated(BaseModel):
    investigation_id: str
    request_id: str
    status: str


class ApprovalDecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: Literal["approved", "rejected"]
    decided_by: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=80)]
    comment: Annotated[str | None, StringConstraints(strip_whitespace=True, max_length=300)] = None


class ApprovalExecuteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    executed_by: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=80)]


def approval_out(record: ApprovalRecord) -> dict:
    """Approval wire format: status, execution result, verification, and
    uncertainty details are exposed separately — `executed` is NOT treated as
    verified success."""
    data = record.model_dump(mode="json")
    data["executed"] = record.status == "executed"
    data["verification_ok"] = (
        record.verification.get("ok") if record.verification is not None else None
    )
    return data
