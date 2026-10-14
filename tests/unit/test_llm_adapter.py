"""LLM adapter unit tests against an in-process mock transport.

The openai SDK (3.x) runs on httpx2, which respx does not cover — so these
tests inject an httpx2.MockTransport through the adapter's http_client seam.
"""

import json

import httpx2
import pytest

from backend.agent.llm.adapter import (
    MalformedOutputError,
    ModelNotAvailableError,
    OpenAICompatLLM,
    ProviderUnavailableError,
    LLMMessage,
    extract_json_object,
    strip_think,
)

BASE = "http://testserver/v1"
MODEL = "test-model"
CHAT_PATH = "/v1/chat/completions"
MODELS_PATH = "/v1/models"


def chat_payload(content="ok", tool_calls=None, usage=True):
    message: dict = {"role": "assistant", "content": content}
    if tool_calls is not None:
        message["tool_calls"] = tool_calls
    payload = {
        "id": "chatcmpl-1",
        "object": "chat.completion",
        "created": 0,
        "model": MODEL,
        "choices": [{"index": 0, "message": message, "finish_reason": "stop"}],
    }
    if usage:
        payload["usage"] = {"prompt_tokens": 11, "completion_tokens": 7}
    return payload


class MockProvider:
    """Scripted responses per request path, with call recording."""

    def __init__(self):
        self.calls: list[httpx2.Request] = []
        self._routes: dict[str, list] = {}

    def route(self, path: str, responses: list):
        self._routes[path] = list(responses)

    def _next(self, path: str):
        queue = self._routes.get(path)
        if not queue:
            raise AssertionError(f"unexpected request to {path}")
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def handler(self, request: httpx2.Request) -> httpx2.Response:
        self.calls.append(request)
        return self._next(request.url.path)

    def adapter(self) -> OpenAICompatLLM:
        client = httpx2.AsyncClient(transport=httpx2.MockTransport(self.handler), timeout=10)
        return OpenAICompatLLM(base_url=BASE, model=MODEL, http_client=client)


async def test_chat_with_tools_parses_calls_and_usage():
    provider = MockProvider()
    provider.route(
        CHAT_PATH,
        [
            httpx2.Response(
                200,
                json=chat_payload(
                    content="",
                    tool_calls=[
                        {
                            "id": "call-1",
                            "type": "function",
                            "function": {"name": "get_daily_sales", "arguments": '{"start_date": "2026-10-05"}'},
                        }
                    ],
                ),
            )
        ],
    )
    response = await provider.adapter().chat_with_tools(
        [LLMMessage(role="user", content="q")],
        [{"type": "function", "function": {"name": "get_daily_sales", "description": "", "parameters": {}}}],
    )
    assert response.tool_calls[0].name == "get_daily_sales"
    assert response.tool_calls[0].arguments == {"start_date": "2026-10-05"}
    assert response.prompt_tokens == 11 and response.completion_tokens == 7


async def test_unparseable_tool_arguments_are_preserved_for_repair():
    provider = MockProvider()
    provider.route(
        CHAT_PATH,
        [
            httpx2.Response(
                200,
                json=chat_payload(
                    content="",
                    tool_calls=[
                        {"id": "c", "type": "function", "function": {"name": "t", "arguments": "{not json"}}
                    ],
                ),
            )
        ],
    )
    response = await provider.adapter().chat_with_tools([LLMMessage(role="user", content="q")], [])
    assert response.tool_calls[0].raw_arguments == "{not json"
    assert response.tool_calls[0].arguments == {}


async def test_connection_refused_maps_to_provider_unavailable():
    provider = MockProvider()
    provider.route(CHAT_PATH, [httpx2.ConnectError("refused"), httpx2.ConnectError("refused")])
    with pytest.raises(ProviderUnavailableError):
        await provider.adapter().chat_with_tools([LLMMessage(role="user", content="q")], [])


async def test_unknown_model_maps_to_model_not_available():
    provider = MockProvider()
    provider.route(CHAT_PATH, [httpx2.Response(404, json={"error": {"message": "model not found"}})])
    with pytest.raises(ModelNotAvailableError):
        await provider.adapter().chat_with_tools([LLMMessage(role="user", content="q")], [])


async def test_check_provider_lists_missing_model():
    provider = MockProvider()
    provider.route(MODELS_PATH, [httpx2.Response(200, json={"object": "list", "data": [{"id": "other"}]})])
    with pytest.raises(ModelNotAvailableError):
        await provider.adapter().check_provider()


async def test_check_provider_unreachable():
    provider = MockProvider()
    provider.route(MODELS_PATH, [httpx2.ConnectError("refused")])
    with pytest.raises(ProviderUnavailableError):
        await provider.adapter().check_provider()


async def test_structured_output_parses_model():
    from backend.agent.models import Understanding

    provider = MockProvider()
    provider.route(
        CHAT_PATH,
        [httpx2.Response(200, json=chat_payload(content='{"goal": "explain the goal", "time_frame": "today", "success_criteria": "grounded answer"}'))],
    )
    result, response = await provider.adapter().complete_structured(
        [LLMMessage(role="user", content="x")], Understanding
    )
    assert result.goal == "explain the goal"
    assert response.completion_tokens == 7


async def test_structured_output_repairs_once():
    from backend.agent.models import Understanding

    provider = MockProvider()
    provider.route(
        CHAT_PATH,
        [
            httpx2.Response(200, json=chat_payload(content="<think>hmm</think> here: {not json")),
            httpx2.Response(
                200, json=chat_payload(content='{"goal": "fixed goal", "time_frame": "today", "success_criteria": "grounded answer"}')
            ),
        ],
    )
    result, _ = await provider.adapter().complete_structured(
        [LLMMessage(role="user", content="x")], Understanding
    )
    assert result.goal == "fixed goal"
    assert len(provider.calls) == 2
    # the repair turn carries the model's bad output back with the correction ask
    repair_message = json.loads(provider.calls[1].read().decode())["messages"][-1]
    assert "invalid" in repair_message["content"].lower()


async def test_structured_output_fails_after_repair():
    from backend.agent.models import Understanding

    provider = MockProvider()
    provider.route(
        CHAT_PATH,
        [
            httpx2.Response(200, json=chat_payload(content="garbage")),
            httpx2.Response(200, json=chat_payload(content="still garbage")),
        ],
    )
    with pytest.raises(MalformedOutputError):
        await provider.adapter().complete_structured([LLMMessage(role="user", content="x")], Understanding)


def test_strip_think_removes_reasoning_blocks():
    assert strip_think("<think>internal</think>answer") == "answer"
    assert strip_think("plain") == "plain"


def test_extract_json_object_finds_embedded_json():
    assert extract_json_object('noise {"a": 1} trailing') == {"a": 1}
    with pytest.raises(ValueError):
        extract_json_object("no json here")
