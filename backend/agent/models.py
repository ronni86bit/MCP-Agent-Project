"""Typed models crossing agent-runtime boundaries (LLM output, reports, records).

Step status taxonomy: pending | running | completed | failed | skipped.
Tool-call status: running | ok | error | unknown (unknown = interrupted before
completion; never re-executed blindly). Investigation status:
running | complete | failed | interrupted.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal

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
    tool: str = Field(default="", max_length=160)  # qualified tool name the step will call
    status: Literal["pending", "running", "completed", "failed", "skipped"] = "pending"
    depends_on: list[int] = Field(default_factory=list, max_length=6)


class Plan(BaseModel):
    """PLAN phase output: a bounded, revisable investigation plan."""

    goal: str = Field(min_length=3, max_length=500)
    steps: list[PlanStep] = Field(min_length=1)
    expected_evidence: list[str] = Field(default_factory=list, max_length=10)
    completion_criteria: str = Field(min_length=1, max_length=500)


class PlanReviewDecision(BaseModel):
    """Post-step review output: continue, or revise the remaining plan."""

    action: Literal["continue", "revise"] = "continue"
    # generous cap: local models ramble; the loop truncates at consumption points
    revision_reason: str = Field(default="", max_length=1200)
    plan: Plan | None = None


class ToolCallRequest(BaseModel):
    """A tool the model selected, with arguments awaiting validation."""

    name: str = Field(min_length=1, max_length=160)
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
    """Deterministic verifier output over a synthesized report.

    grounding_checked=True means the deterministic value/entity checks ran
    (id existence is always checked). This is NOT general semantic grounding.
    """

    ok: bool = True
    dropped_claims: list[str] = Field(default_factory=list)
    flagged_claims: list[str] = Field(default_factory=list)
    downgraded: bool = False
    grounding_checked: bool = False
    notes: list[str] = Field(default_factory=list)


class StepRecord(BaseModel):
    index: int
    plan_version: int = 1
    plan_step_id: int | None = None
    action: str = ""
    status: Literal["pending", "running", "completed", "failed", "skipped"] = "pending"
    evidence_ids: list[str] = Field(default_factory=list)


class ToolCallObservation(BaseModel):
    step: int
    tool: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    attempt: int = 1  # 1 = first selection, 2 = repair/reselection
    status: Literal["running", "ok", "error", "unknown"] = "running"
    error_class: str | None = None
    error: str | None = None
    duration_ms: float = 0.0
    evidence_id: str | None = None
    at: datetime = Field(default_factory=_utcnow)


class PlanVersionInfo(BaseModel):
    version: int
    plan: Plan
    revision_reason: str = ""
    superseded: bool = False


class ApprovalRecord(BaseModel):
    """One human-gated write action (Phase 7: `acknowledge_alert` only).

    Status lifecycle:
    proposed -> approved -> executing -> executed -> (verification recorded)
                                           -> failed
                                           -> uncertain -> (recovery) executed/failed
    proposed -> rejected | expired | superseded
    """

    approval_id: str
    investigation_id: str
    tool: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    status: Literal[
        "proposed",
        "approved",
        "rejected",
        "expired",
        "superseded",
        "executing",
        "executed",
        "failed",
        "uncertain",
    ] = "proposed"
    proposal: dict[str, Any] = Field(default_factory=dict)  # full ActionProposal payload (rationale, evidence_ids)
    args_hash: str | None = None
    proposal_version: int = 1
    expires_at: datetime | None = None
    created_at: datetime = Field(default_factory=_utcnow)
    decided_by: str | None = None
    decision_comment: str | None = None
    decided_at: datetime | None = None
    executed_by: str | None = None
    dispatched_at: datetime | None = None
    executed_at: datetime | None = None
    execution_result: dict[str, Any] = Field(default_factory=dict)
    verification: dict[str, Any] | None = None
