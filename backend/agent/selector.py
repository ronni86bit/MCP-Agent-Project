"""SELECT phase helpers: build the tool-selection prompt and parse the choice.

The model selects tools exclusively through native tool calling; there is no
path from model output to arbitrary code or SQL.
"""

from __future__ import annotations

from backend.agent.llm.adapter import LLMMessage, LLMResponse
from backend.agent.models import Plan, ToolCallRequest


def _compact_plan(plan: Plan) -> str:
    """Steps with their named tools — compact but unambiguous."""
    steps = "; ".join(
        f"{step.id}) {step.action}" + (f" [{step.tool}]" if step.tool else "")
        for step in plan.steps
    )
    return f"goal: {plan.goal} | steps: {steps}"


def selection_messages(
    *,
    question: str,
    plan: Plan,
    digest: str,
    step_action: str,
    step_evidence_needed: str,
    dataset_context: str,
    repair_feedback: str | None = None,
    recent_failures: list[str] | None = None,
) -> list[LLMMessage]:
    system = (
        "You are a careful operations investigation agent. "
        f"Dataset context: {dataset_context} "
        "Rules: call exactly ONE tool per turn — the one that best advances the CURRENT step. "
        "Dates are ISO YYYY-MM-DD. Use filter values (product/region/channel) only when they "
        "appear in the evidence or the question; otherwise omit the filter. "
        "Never invent tools: choose only from the provided tool definitions. "
        "When comparing two windows, they must span the SAME number of days. "
        "When the plan is complete, or the step cannot progress, reply with a short message and NO tool call."
    )
    parts = [
        f"Question: {question}",
        f"Plan: {_compact_plan(plan)}",
        "Evidence so far:",
        digest or "(none)",
    ]
    if recent_failures:
        parts.append("Recent failed attempts (do NOT repeat them; fix what they complain about):")
        parts.extend(f"- {failure}" for failure in recent_failures)
    parts.append(f"CURRENT STEP: {step_action} (evidence needed: {step_evidence_needed})")
    if repair_feedback:
        parts.append(f"Your previous selection was rejected: {repair_feedback} Make one corrected tool call.")
    parts.append("Respond now with one tool call, or a short completion message.")
    return [
        LLMMessage(role="system", content=system),
        LLMMessage(role="user", content="\n".join(parts)),
    ]


def parse_selection(response: LLMResponse) -> ToolCallRequest | None:
    """Return the requested tool call, or None when the model signalled completion."""
    if not response.tool_calls:
        return None
    call = response.tool_calls[0]
    if call.raw_arguments is not None:
        # Unparseable JSON arguments: surface as a request with the raw payload
        # so validation produces a repairable error instead of silently passing.
        return ToolCallRequest(name=call.name, arguments={"_raw": call.raw_arguments})
    return ToolCallRequest(name=call.name, arguments=call.arguments)
