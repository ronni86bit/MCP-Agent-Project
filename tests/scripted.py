"""Deterministic LLM test double implementing the adapter interface.

Selections are queued LLMToolCall objects; structured outputs are returned by
schema type. `MalformedLLM` and `RaisingLLM` cover failure paths.
"""

from __future__ import annotations

from pydantic import BaseModel

from backend.agent.llm.adapter import LLMClient, LLMMessage, LLMResponse
from backend.agent.models import InvestigationReport, Plan, PlanStep, Understanding


class ScriptedLLM(LLMClient):
    model_name = "scripted"

    def __init__(
        self,
        understanding: Understanding,
        plan: Plan,
        selections: list,
        report: InvestigationReport,
    ):
        self.understanding = understanding
        self.plan = plan
        self.selections = list(selections)
        self.report = report
        self.select_calls = 0
        self.last_messages: list[LLMMessage] | None = None
        self.last_tools: list[dict] | None = None

    async def check_provider(self) -> None:
        return None

    async def complete_structured(
        self, messages: list[LLMMessage], schema_model: type[BaseModel], *, max_tokens: int | None = None
    ) -> tuple[BaseModel, LLMResponse]:
        if schema_model is Understanding:
            return self.understanding, LLMResponse(model=self.model_name)
        if schema_model is Plan:
            return self.plan, LLMResponse(model=self.model_name)
        if schema_model is InvestigationReport:
            return self.report, LLMResponse(model=self.model_name)
        raise AssertionError(f"unexpected schema model {schema_model.__name__}")

    async def chat_with_tools(
        self, messages: list[LLMMessage], tools: list[dict], *, max_tokens: int | None = None
    ) -> LLMResponse:
        self.last_messages = messages
        self.last_tools = tools
        self.select_calls += 1
        if not self.selections:
            return LLMResponse(content="plan complete")
        selection = self.selections.pop(0)
        if selection is None:
            return LLMResponse(content="plan complete")
        return LLMResponse(content="", tool_calls=[selection])


class MalformedLLM(ScriptedLLM):
    """Structured calls fail even after the adapter's repair retry."""

    async def complete_structured(self, messages, schema_model, *, max_tokens=None):
        if schema_model is Understanding or schema_model is Plan:
            return await super().complete_structured(messages, schema_model, max_tokens=max_tokens)
        from backend.agent.llm.adapter import MalformedOutputError

        raise MalformedOutputError("scripted malformed output")


class RaisingLLM(ScriptedLLM):
    """LLM calls raise immediately (e.g. provider unavailable)."""

    def __init__(self, error: Exception):
        super().__init__(
            understanding=Understanding(
                goal="explain the question", time_frame="today", success_criteria="grounded answer"
            ),
            plan=Plan(
                goal="investigate",
                steps=[PlanStep(id=1, action="placeholder step", evidence_needed="evidence")],
                completion_criteria="criteria met",
            ),
            selections=[],
            report=InvestigationReport(conclusion="conclusion"),
        )
        self.error = error

    async def complete_structured(self, messages, schema_model, *, max_tokens=None):
        raise self.error
