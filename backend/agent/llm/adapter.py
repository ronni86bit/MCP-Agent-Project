"""Provider-agnostic LLM boundary.

`LLMClient` is the interface the whole runtime depends on; `OpenAICompatLLM` is
the one implementation that knows about a concrete provider (any
OpenAI-compatible endpoint — Ollama first, per architecture). No other module
may import a provider SDK.

Structured output uses strict-JSON prompting plus Pydantic validation with
exactly one repair retry — no provider-specific structured-output features are
assumed.
"""

from __future__ import annotations

import json
import re
from abc import ABC, abstractmethod
from collections.abc import Callable, Awaitable
from typing import Any, Literal

import structlog
from openai import APIConnectionError, APITimeoutError, AsyncOpenAI, BadRequestError, NotFoundError
from openai import APIStatusError
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from backend.reliability import retry_async

log = structlog.get_logger(__name__)

_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)


class LLMError(Exception):
    """Base class for provider-facing failures."""


class ProviderUnavailableError(LLMError):
    """The provider endpoint cannot be reached."""


class ProviderTimeoutError(LLMError):
    """The provider did not answer within the configured timeout."""


class ModelNotAvailableError(LLMError):
    """The configured model is not installed on the provider."""


class ProviderResponseError(LLMError):
    """The provider rejected the request or returned an unusable response."""


class MalformedOutputError(LLMError):
    """The model could not produce schema-valid JSON after the repair retry."""


# transient provider failures worth one bounded retry (never auth/model errors)
_TRANSIENT = (ProviderUnavailableError, ProviderTimeoutError)


class LLMMessage(BaseModel):
    role: Literal["system", "user", "assistant", "tool"]
    content: str | None = None
    tool_call_id: str | None = None
    name: str | None = None


class LLMToolCall(BaseModel):
    id: str = ""
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    raw_arguments: str | None = None  # set when the JSON did not parse

    model_config = ConfigDict(extra="forbid")


class LLMResponse(BaseModel):
    content: str = ""
    tool_calls: list[LLMToolCall] = Field(default_factory=list)
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    model: str = ""


def strip_think(text: str) -> str:
    """Remove qwen-style <think> blocks from message content."""
    return _THINK_RE.sub("", text or "").strip()


def extract_json_object(text: str) -> Any:
    """Parse the first JSON object found in `text`; raise ValueError otherwise."""
    cleaned = strip_think(text)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(cleaned[start : end + 1])
        except json.JSONDecodeError:
            pass
    raise ValueError("no parseable JSON object in model output")


class LLMClient(ABC):
    """Interface the agent runtime depends on. Implementations: the adapter
    below (real providers) and test doubles."""

    model_name: str

    @abstractmethod
    async def check_provider(self) -> None:
        """Raise a typed LLMError when the provider or model is unavailable."""

    @abstractmethod
    async def chat_with_tools(
        self, messages: list[LLMMessage], tools: list[dict], *, max_tokens: int | None = None
    ) -> LLMResponse:
        """One chat turn with native tool definitions available."""

    @abstractmethod
    async def complete_structured(
        self, messages: list[LLMMessage], schema_model: type[BaseModel], *, max_tokens: int | None = None
    ) -> tuple[BaseModel, LLMResponse]:
        """Return a schema-validated model instance (one repair retry on
        malformed output; raises MalformedOutputError after the retry)."""

    def _retry_transient(self, coro_factory: Callable[[], Awaitable[Any]], purpose: str) -> Any:
        """Bounded retry for transient provider failures only."""
        return retry_async(
            coro_factory,
            attempts=1 + self._retry_attempts,
            base_delay_seconds=self._retry_backoff_seconds,
            retry_on=_TRANSIENT,
            on_retry=lambda attempt, delay, exc: log.warning(
                "llm_transient_retry",
                purpose=purpose,
                attempt=attempt,
                delay_seconds=round(delay, 2),
                error=str(exc)[:160],
            ),
        )


class OpenAICompatLLM(LLMClient):
    """Adapter for any OpenAI-compatible chat endpoint (Ollama default)."""

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        timeout_seconds: float = 120.0,
        temperature: float = 0.1,
        api_key: str = "ollama",  # placeholder; local providers do not check it
        http_client: Any | None = None,  # injectable transport seam (tests)
        retry_attempts: int = 1,
        retry_backoff_seconds: float = 2.0,
    ):
        self.model_name = model
        self._temperature = temperature
        self._retry_attempts = retry_attempts
        self._retry_backoff_seconds = retry_backoff_seconds
        client_kwargs: dict[str, Any] = dict(
            base_url=base_url, api_key=api_key, timeout=timeout_seconds, max_retries=0
        )
        if http_client is not None:
            client_kwargs["http_client"] = http_client
        self._client = AsyncOpenAI(**client_kwargs)

    async def check_provider(self) -> None:
        try:
            models = await self._client.models.list()
        except APITimeoutError as exc:
            raise ProviderTimeoutError(f"LLM provider did not answer in time: {exc}") from exc
        except APIConnectionError as exc:
            raise ProviderUnavailableError(
                f"cannot reach LLM provider: {exc}. Is Ollama running at the configured host?"
            ) from exc
        available = sorted(model.id for model in models.data)
        if self.model_name not in available:
            raise ModelNotAvailableError(
                f"model {self.model_name!r} is not installed on the provider. "
                f"Run: ollama pull {self.model_name}. Installed: {available}"
            )

    async def chat_with_tools(
        self, messages: list[LLMMessage], tools: list[dict], *, max_tokens: int | None = None
    ) -> LLMResponse:
        response = await self._retry_transient(
            lambda: self._create(
                [self._to_openai(message) for message in messages], tools=tools, max_tokens=max_tokens
            ),
            purpose="chat_with_tools",
        )
        choice = response.choices[0].message
        tool_calls = [
            LLMToolCall(
                id=call.id or "",
                name=call.function.name,
                **self._arguments(call.function.arguments),
            )
            for call in choice.tool_calls or []
        ]
        return LLMResponse(
            content=strip_think(choice.content or ""),
            tool_calls=tool_calls,
            prompt_tokens=getattr(response.usage, "prompt_tokens", None),
            completion_tokens=getattr(response.usage, "completion_tokens", None),
            model=response.model or self.model_name,
        )

    async def complete_structured(
        self, messages: list[LLMMessage], schema_model: type[BaseModel], *, max_tokens: int | None = None
    ) -> tuple[BaseModel, LLMResponse]:
        schema = json.dumps(schema_model.model_json_schema())
        instruction = LLMMessage(
            role="system",
            content=(
                "You must respond with a single JSON object and nothing else. "
                f"The object must conform to this JSON Schema:\n{schema}"
            ),
        )
        convo = [instruction, *messages]
        last_error = "unknown"
        for attempt in (1, 2):
            response = await self._retry_transient(
                lambda: self._create(
                    [self._to_openai(message) for message in convo],
                    tools=None,
                    max_tokens=max_tokens,
                    json_mode=True,
                ),
                purpose=f"structured:{schema_model.__name__}",
            )
            content = strip_think(response.choices[0].message.content or "")
            llm_response = LLMResponse(
                content=content,
                prompt_tokens=getattr(response.usage, "prompt_tokens", None),
                completion_tokens=getattr(response.usage, "completion_tokens", None),
                model=response.model or self.model_name,
            )
            try:
                parsed = extract_json_object(content)
                return schema_model.model_validate(parsed), llm_response
            except (ValueError, ValidationError) as exc:
                last_error = self._validation_reason(exc)
                log.warning("llm_structured_repair", attempt=attempt, error=last_error)
                convo = [
                    *convo,
                    LLMMessage(role="assistant", content=content),
                    LLMMessage(
                        role="user",
                        content=(
                            f"Your JSON was invalid: {last_error}. "
                            "Respond again with the corrected JSON object only."
                        ),
                    ),
                ]
        raise MalformedOutputError(
            f"model did not produce schema-valid JSON for {schema_model.__name__} after repair: {last_error}"
        )

    # -- internals ---------------------------------------------------------

    def _to_openai(self, message: LLMMessage) -> dict:
        payload: dict[str, Any] = {"role": message.role, "content": message.content}
        if message.tool_call_id is not None:
            payload["tool_call_id"] = message.tool_call_id
        if message.name is not None:
            payload["name"] = message.name
        return payload

    @staticmethod
    def _arguments(raw: str) -> dict:
        try:
            return {"arguments": json.loads(raw or "{}")}
        except json.JSONDecodeError:
            return {"arguments": {}, "raw_arguments": raw}

    async def _create(
        self,
        openai_messages: list[dict],
        *,
        tools: list[dict] | None,
        max_tokens: int | None,
        json_mode: bool = False,
    ):
        request: dict[str, Any] = {
            "model": self.model_name,
            "messages": openai_messages,
            "temperature": self._temperature,
        }
        if tools:
            request["tools"] = tools
            request["tool_choice"] = "auto"
        if max_tokens is not None:
            request["max_tokens"] = max_tokens
        if json_mode:
            request["response_format"] = {"type": "json_object"}
        try:
            return await self._client.chat.completions.create(**request)
        except APITimeoutError as exc:
            raise ProviderTimeoutError(f"LLM call timed out: {exc}") from exc
        except APIConnectionError as exc:
            raise ProviderUnavailableError(
                f"cannot reach LLM provider: {exc}. Is Ollama running at the configured host?"
            ) from exc
        except NotFoundError as exc:
            raise ModelNotAvailableError(
                f"model {self.model_name!r} not found on the provider: {exc}. "
                f"Run: ollama pull {self.model_name}"
            ) from exc
        except BadRequestError as exc:
            message = str(exc)
            if json_mode and "response_format" in message:
                # Provider without json_object support: retry without it.
                request.pop("response_format")
                try:
                    return await self._client.chat.completions.create(**request)
                except (APIConnectionError, APITimeoutError, NotFoundError, BadRequestError, APIStatusError) as retry_exc:
                    raise self._map_unexpected(retry_exc) from retry_exc
            raise ProviderResponseError(f"provider rejected the request: {message}") from exc
        except APIStatusError as exc:
            raise self._map_unexpected(exc) from exc

    @staticmethod
    def _map_unexpected(exc: APIStatusError) -> LLMError:
        if isinstance(exc, NotFoundError):
            return ModelNotAvailableError(f"model not found on the provider: {exc}")
        return ProviderResponseError(f"provider error (status {exc.status_code}): {exc}")

    @staticmethod
    def _validation_reason(exc: ValueError | ValidationError) -> str:
        if isinstance(exc, ValidationError):
            return "; ".join(
                f"{'.'.join(str(part) for part in err['loc'])}: {err['msg']}" for err in exc.errors()[:5]
            )
        return str(exc)
