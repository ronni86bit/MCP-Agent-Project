"""Phase 3 model smoke: verify Ollama + the configured model can select and
invoke postgres-mcp tools for simple prompts.

    uv run python scripts/model_smoke.py
"""

import asyncio
import sys

from backend.agent.llm.adapter import LLMError, OpenAICompatLLM
from backend.agent.service import default_server_factories
from backend.agent.smoke import DEFAULT_PROBES, run_selection_probes
from backend.config import get_settings
from backend.mcp.client_manager import MultiMCPManager
from backend.observability.logging import configure_logging


async def main() -> int:
    settings = get_settings()
    configure_logging(settings.log_level)
    llm = OpenAICompatLLM(
        base_url=f"{settings.ollama_host}/v1",
        model=settings.ollama_model,
        timeout_seconds=settings.llm_timeout_seconds,
        temperature=settings.llm_temperature,
    )
    print(f"LLM: {settings.ollama_model} at {settings.ollama_host}")
    try:
        await llm.check_provider()
    except LLMError as exc:
        print(f"FAILED: {exc}")
        return 1

    from backend.mcp.client_manager import MultiMCPManager as _M

    async with _M(default_server_factories(settings)) as managers:
        rows = await run_selection_probes(llm, managers, DEFAULT_PROBES)

    print(f"\n{'question':<50} {'selected':<18} {'args':<6} {'invoked':<8} {'select ms':>9}")
    for row in rows:
        print(
            f"{row['question']:<50} {str(row['selected_tool']):<18} "
            f"{str(row['arguments_valid']):<6} {str(row['invocation_ok']):<8} {row['selection_ms']:>9}"
        )
    ok = all(
        row["selected_tool"] == row["expected_tool"] and row["arguments_valid"] and row["invocation_ok"]
        for row in rows
    )
    print("\nMODEL SMOKE PASSED" if ok else "\nMODEL SMOKE FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
