"""UNDERSTAND and PLAN phases: structured LLM calls with bounded outputs."""

from __future__ import annotations

import structlog
import yaml
from pydantic import ValidationError

from backend.agent.llm.adapter import LLMClient, LLMMessage
from backend.agent.models import Plan, Understanding
from backend.config import Settings
from backend.mcp.fixtures.loader import ScenarioSpec, load_scenario

log = structlog.get_logger(__name__)


def dataset_context() -> str:
    """Data-bound context for prompts, derived from the scenario spec (single
    source of truth) — never from wall-clock assumptions."""
    try:
        spec = load_scenario()
    except (OSError, yaml.YAMLError, ValidationError) as exc:
        log.warning("dataset_context_unavailable", error=str(exc))
        return "(dataset context unavailable; do not assume specific dates)"
    return _describe(spec)


def _describe(spec: ScenarioSpec) -> str:
    return (
        f"Dataset: daily sales (orders/units/revenue) by product, region, and channel, "
        f"covering {spec.start_date} through {spec.end_date} (inclusive). "
        f"'today' refers to {spec.end_date} unless the question names another date. "
        f"A known-healthy baseline window is {spec.baseline.start_date}..{spec.baseline.end_date}."
    )


async def understand(llm: LLMClient, question: str, context: str) -> Understanding:
    messages = [
        LLMMessage(role="system", content="You extract the intent of an operations question about a sales database. Be concise and literal."),
        LLMMessage(role="user", content=f"Question: {question}\n\nDataset context: {context}\n\nIdentify the goal, time_frame, focus_areas (up to 3), and success_criteria."),
    ]
    understanding, _ = await llm.complete_structured(messages, Understanding, max_tokens=400)
    return understanding


async def make_plan(
    llm: LLMClient,
    question: str,
    understanding: Understanding,
    tool_names: list[str],
    settings: Settings,
) -> Plan:
    system = (
        "You plan small investigations over a demo operations stack. "
        f"You may only use these tools: {', '.join(tool_names)}. "
        "The tools come from three systems: postgres-mcp (business/sales data), monitoring-mcp "
        "(service health: error rates, alerts, incidents), and github-mcp (commits, deployments, "
        "issues). For 'why did X happen' questions, plan steps that check every relevant system — "
        "a business decline usually needs the sales numbers AND service health AND recent "
        "software changes before a confident answer is possible. "
        f"Produce at most {settings.max_plan_steps} steps; prefer the fewest steps that answer the "
        "question — each step is exactly one tool call, and redundant repeats waste the budget. "
        "When a step compares two windows, they must span the same number of days. "
        "Steps are executed in order; later steps may use earlier evidence."
    )
    user = (
        f"Question: {question}\n\nUnderstanding: {understanding.model_dump_json()}\n\n"
        f"Dataset context: {dataset_context()}\n\n"
        "Return a plan: goal, steps (each with id, action, evidence_needed), "
        "expected_evidence, completion_criteria."
    )
    plan, _ = await llm.complete_structured(
        [LLMMessage(role="system", content=system), LLMMessage(role="user", content=user)],
        Plan,
        max_tokens=700,
    )
    if len(plan.steps) > settings.max_plan_steps:
        log.warning("plan_too_long", steps=len(plan.steps), retrying_with_limit=settings.max_plan_steps)
        plan, _ = await llm.complete_structured(
            [
                LLMMessage(role="system", content=system),
                LLMMessage(role="user", content=user),
                LLMMessage(role="assistant", content=plan.model_dump_json()),
                LLMMessage(role="user", content=f"Too many steps. Return at most {settings.max_plan_steps} steps."),
            ],
            Plan,
            max_tokens=700,
        )
        if len(plan.steps) > settings.max_plan_steps:
            plan = plan.model_copy(update={"steps": plan.steps[: settings.max_plan_steps]})
    return plan
