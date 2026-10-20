"""Evaluation-only LLM recorder (Phase 9 Gate 3).

Wraps any `LLMClient` and records per-call wall time and the token counts the
adapter already parsed onto `LLMResponse` — nothing else. It never stores or
logs prompts, tool definitions, completions, credentials, or payloads; only
durations, token counts, method names, and exception CLASS names.

Delegation contract: responses are returned unchanged (same objects); provider
exceptions and cancellation propagate unchanged after being recorded.
Unavailable usage stays `None` — it is never reported as zero.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from pydantic import BaseModel

from backend.agent.llm.adapter import LLMClient, LLMMessage, LLMResponse


@dataclass(frozen=True)
class CallRecord:
    """One delegated LLM call. Deliberately contains no prompt/completion text."""

    method: str
    ok: bool
    elapsed_ms: float
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    error_class: str | None = None


@dataclass
class RecordingLLM(LLMClient):
    """`LLMClient` decorator that records timing/usage. Treat the recorded
    list as diagnostics only — delegation behavior is untouched."""

    inner: LLMClient
    records: list[CallRecord] = field(default_factory=list)

    @property
    def model_name(self) -> str:  # type: ignore[override]
        return self.inner.model_name

    def _record(self, method: str, started: float, *, response: LLMResponse | None = None,
                error: BaseException | None = None) -> None:
        try:
            self.records.append(CallRecord(
                method=method,
                ok=error is None,
                elapsed_ms=round((time.perf_counter() - started) * 1000, 1),
                prompt_tokens=response.prompt_tokens if response is not None else None,
                completion_tokens=response.completion_tokens if response is not None else None,
                error_class=type(error).__name__ if error is not None else None,
            ))
        except Exception:  # recording must never alter delegation behavior
            pass

    async def check_provider(self) -> None:
        started = time.perf_counter()
        try:
            await self.inner.check_provider()
        except BaseException as exc:
            self._record("check_provider", started, error=exc)
            raise
        self._record("check_provider", started)

    async def chat_with_tools(
        self, messages: list[LLMMessage], tools: list[dict], *, max_tokens: int | None = None
    ) -> LLMResponse:
        started = time.perf_counter()
        try:
            response = await self.inner.chat_with_tools(messages, tools, max_tokens=max_tokens)
        except BaseException as exc:
            self._record("chat_with_tools", started, error=exc)
            raise
        self._record("chat_with_tools", started, response=response)
        return response  # unchanged object

    async def complete_structured(
        self, messages: list[LLMMessage], schema_model: type[BaseModel], *, max_tokens: int | None = None
    ) -> tuple[BaseModel, LLMResponse]:
        started = time.perf_counter()
        try:
            model, response = await self.inner.complete_structured(
                messages, schema_model, max_tokens=max_tokens)
        except BaseException as exc:
            self._record("complete_structured", started, error=exc)
            raise
        self._record("complete_structured", started, response=response)
        return model, response  # unchanged objects

    # -- aggregated diagnostics ---------------------------------------------

    def totals(self) -> dict:
        """Aggregate usage. Tokens are summed over calls that reported them;
        when NO call reported usage the value stays None (never zero), and
        partially-reported usage is visible via `usage_missing_calls`."""
        completed = [r for r in self.records if r.ok]
        prompt = [r.prompt_tokens for r in completed if r.prompt_tokens is not None]
        completion = [r.completion_tokens for r in completed if r.completion_tokens is not None]
        usage_missing = sum(1 for r in completed if r.prompt_tokens is None
                            and r.completion_tokens is None)
        return {
            "calls": len(self.records),
            "llm_ms": round(sum(r.elapsed_ms for r in self.records), 1),
            "prompt_tokens": sum(prompt) if prompt else None,
            "completion_tokens": sum(completion) if completion else None,
            "usage_missing_calls": usage_missing,
        }
