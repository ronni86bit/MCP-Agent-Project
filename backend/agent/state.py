"""In-memory investigation state (Phase 3 scope).

Persistence of investigation state belongs to the Planning & State phase; this
registry only needs to serve live API reads and the loop's record updates.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field

from backend.agent.models import (
    InvestigationReport,
    Plan,
    StepRecord,
    ToolCallObservation,
    Understanding,
    VerificationResult,
)


class InvestigationRecord(BaseModel):
    investigation_id: str
    request_id: str
    question: str
    status: str = "running"  # running | complete | failed
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    finished_at: datetime | None = None
    states_visited: list[str] = Field(default_factory=list)
    understanding: Understanding | None = None
    plan: Plan | None = None
    steps: list[StepRecord] = Field(default_factory=list)
    tool_calls: list[ToolCallObservation] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    report: InvestigationReport | None = None
    verification: VerificationResult | None = None
    failure_reason: str | None = None
    warnings: list[str] = Field(default_factory=list)
    llm_calls: int = 0

    def dict_for_api(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


class InvestigationRegistry:
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
