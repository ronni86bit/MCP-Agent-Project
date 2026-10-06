"""Typed models crossing agent-runtime boundaries (LLM output, reports, records)."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Understanding(BaseModel):
    """UNDERSTAND phase output: the agent's reading of the question."""

    goal: str = Field(min_length=3, max_length=500)
    time_frame: str = Field(min_length=1, max_length=200)
    focus_areas: list[str] = Field(default_factory=list, max_length=6)
    success_criteria: str = Field(min_length=3, max_length=500)


class PlanStep(BaseModel):
    id: int = Field(ge=1)
    action: str = Field(min_length=3, max_length=400)
    evidence_needed: str = Field(min_length=1, max_length=300)


class Plan(BaseModel):
    """PLAN phase output: a bounded, revisable investigation plan."""

    goal: str = Field(min_length=3, max_length=500)
    steps: list[PlanStep] = Field(min_length=1)
    expected_evidence: list[str] = Field(default_factory=list, max_length=10)
    completion_criteria: str = Field(min_length=1, max_length=500)


class ToolCallRequest(BaseModel):
    """A tool the model selected, with arguments awaiting validation."""

    name: str = Field(min_length=1, max_length=120)
    arguments: dict[str, Any] = Field(default_factory=dict)


class Claim(BaseModel):
    text: str = Field(min_length=1, max_length=220)
    evidence_ids: list[str] = Field(default_factory=list, max_length=20)


class Confidence(str, Enum):
    low = "low"
    medium = "medium"
    high = "high"


class InvestigationReport(BaseModel):
    """Final synthesized answer; every claim must cite evidence ids.

    Field caps are deliberately tight: local models emit this JSON under a
    max_tokens budget, and truncation breaks parsing.
    """

    conclusion: str = Field(min_length=1, max_length=700)
    claims: list[Claim] = Field(default_factory=list, max_length=4)
    confidence: Confidence = Confidence.low
    confidence_rationale: str = Field(default="", max_length=250)
    caveats: list[str] = Field(default_factory=list, max_length=3)
    evidence_gaps: list[str] = Field(default_factory=list, max_length=4)


class VerificationResult(BaseModel):
    """Deterministic verifier output over a synthesized report."""

    ok: bool = True
    dropped_claims: list[str] = Field(default_factory=list)
    downgraded: bool = False
    notes: list[str] = Field(default_factory=list)


class StepRecord(BaseModel):
    index: int
    action: str = ""
    status: str = "pending"  # pending | ok | failed | skipped
    evidence_ids: list[str] = Field(default_factory=list)


class ToolCallObservation(BaseModel):
    step: int
    tool: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    attempt: int = 1  # 1 = first selection, 2 = repair/reselection
    status: str = "ok"  # ok | error
    error_class: str | None = None
    error: str | None = None
    duration_ms: float = 0.0
    evidence_id: str | None = None
    at: datetime = Field(default_factory=_utcnow)
