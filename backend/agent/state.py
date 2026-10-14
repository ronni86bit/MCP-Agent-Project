"""In-memory investigation record + registry, persisted by the state store.

The record is the API contract and the loop's working state; every mutation
is checkpointed to PostgreSQL (agent_state schema) by the loop, so retrieval
survives backend restarts. Statuses: running | complete | failed | interrupted.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field

from backend.agent.evidence import Evidence
from backend.agent.models import (
    InvestigationReport,
    Plan,
    PlanVersionInfo,
    StepRecord,
    ToolCallObservation,
    Understanding,
    VerificationResult,
    _utcnow,
)


class InvestigationRecord(BaseModel):
    investigation_id: str
    request_id: str
    question: str
    status: str = "running"  # running | complete | failed | interrupted
    stage: str = "created"
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)
    finished_at: datetime | None = None
    states_visited: list[str] = Field(default_factory=list)
    understanding: Understanding | None = None
    plan: Plan | None = None
    plan_version: int = 0  # 0 until the first plan is saved
    plan_history: list[PlanVersionInfo] = Field(default_factory=list)
    steps: list[StepRecord] = Field(default_factory=list)
    tool_calls: list[ToolCallObservation] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    report: InvestigationReport | None = None
    verification: VerificationResult | None = None
    failure_reason: str | None = None
    warnings: list[str] = Field(default_factory=list)
    llm_calls: int = 0

    def dict_for_api(self) -> dict[str, Any]:
        return self.model_dump(mode="json")

    def touch(self) -> None:
        self.updated_at = _utcnow()


class InvestigationRegistry:
    """Live records for investigations running in THIS process."""

    def __init__(self) -> None:
        self._items: dict[str, InvestigationRecord] = {}

    def create(self, question: str) -> InvestigationRecord:
        record = InvestigationRecord(
            investigation_id=f"inv-{uuid.uuid4().hex[:12]}",
            request_id=f"req-{uuid.uuid4().hex[:12]}",
            question=question,
        )
        self._items[record.investigation_id] = record
        return record

    def get(self, investigation_id: str) -> InvestigationRecord | None:
        return self._items.get(investigation_id)
