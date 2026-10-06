"""Phase 3 model smoke evaluation: tool selection on the real local model.

Small and deterministic by design; the full benchmark belongs to Phase 9.
"""

import pytest

from backend.agent.smoke import DEFAULT_PROBES, run_selection_probes

pytestmark = pytest.mark.ollama


async def test_model_selects_expected_tools(pg_manager, real_llm):
    async with pg_manager() as manager:
        rows = await run_selection_probes(real_llm, manager, DEFAULT_PROBES)
    for row in rows:
        print(
            f"{row['question']!r:55} selected={row['selected_tool']} "
            f"args_ok={row['arguments_valid']} invoked={row['invocation_ok']} "
            f"select={row['selection_ms']}ms execute={row['execution_ms']}ms"
        )
    for row in rows:
        assert row["selected_tool"] == row["expected_tool"], row
        assert row["arguments_valid"], row
        assert row["invocation_ok"], row
        assert row["selection_ms"] < 180_000, row
