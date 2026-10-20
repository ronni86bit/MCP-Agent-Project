"""Fixtures for real-model (Ollama) evaluation tests.

These run only when the configured Ollama endpoint and model are reachable;
otherwise they skip (the phase gate verifies them via scripts instead).
"""

import asyncio

import pytest

from backend.agent.llm.adapter import LLMClient, LLMError, OpenAICompatLLM
from backend.config import get_settings


def _build_llm() -> OpenAICompatLLM:
    settings = get_settings()
    return OpenAICompatLLM(
        base_url=f"{settings.ollama_host}/v1",
        model=settings.ollama_model,
        timeout_seconds=settings.llm_timeout_seconds,
        temperature=settings.llm_temperature,
    )


def _ollama_ready() -> bool:
    try:
        asyncio.run(_build_llm().check_provider())
        return True
    except LLMError:
        return False


@pytest.fixture
def real_llm() -> LLMClient:
    """Function-scoped ON PURPOSE: the OpenAI-compatible client's httpx pool
    binds to the first event loop that uses it, and pytest-asyncio gives each
    test a fresh loop — a session-scoped client breaks the second test with
    'Event loop is closed' (observed in the Gate 5 verification chain)."""
    if not _ollama_ready():
        pytest.skip(
            "Ollama or the configured model is unavailable — start Ollama and "
            "pull the model named in OLLAMA_MODEL"
        )
    return _build_llm()
