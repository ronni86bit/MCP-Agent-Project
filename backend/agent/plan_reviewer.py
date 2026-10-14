"""Post-step plan review: bounded plan revision when evidence changes direction.

The reviewer runs after each executed step (budget-permitting) and may either
continue the current plan or propose a full revised plan. Revisions are
bounded by MAX_PLAN_REVISIONS, keep every bound of the original plan, and
never touch already-collected evidence — plan history is preserved for
auditability.
"""

from __future__ import annotations

from backend.agent.llm.adapter import LLMClient, LLMMessage
from backend.agent.models import Plan, PlanReviewDecision
from backend.config import Settings


async def review_plan(
    llm: LLMClient, question: str, plan: Plan, evidence_digest: str, settings: Settings
) -> PlanReviewDecision:
    system = (
        "You review an investigation plan after each executed step. Decide whether to "
        "'continue' the remaining plan or 'revise' it. Revise ONLY when the evidence so far "
        "makes the remaining steps pointless or a clearly better direction exists — do not "
        "revise out of caution. A revised plan keeps the same bounds: "
        f"at most {settings.max_plan_steps} steps, exactly one tool call per step, and any "
        "compared windows must span the same number of days."
    )
    user = (
        f"Question: {question}\n\nCurrent plan: {_compact_plan(plan)}\n\n"
        f"Evidence so far:\n{evidence_digest}\n\n"
        "Return JSON: action (\"continue\"|\"revise\"), revision_reason (string, under 40 words), and — "
        "only when action is \"revise\" — a full replacement plan (goal, steps with id/action/"
        "evidence_needed, expected_evidence, completion_criteria). Revise only when clearly needed; "
        "a revised plan has at most 3 steps."
    )
    decision, _ = await llm.complete_structured(
        [LLMMessage(role="system", content=system), LLMMessage(role="user", content=user)],
        PlanReviewDecision,
        max_tokens=900,
    )
    return decision


def _compact_plan(plan: Plan) -> str:
    steps = "; ".join(f"{step.id}) {step.action}" for step in plan.steps)
    return f"goal: {plan.goal} | steps: {steps}"
