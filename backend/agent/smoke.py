"""Minimal tool-selection smoke probe (Phase 3 model evaluation).

Not the Phase 9 benchmark: a handful of simple prompts recording which tool
the model selected, whether arguments validated, whether the MCP invocation
succeeded, and latency. Shared by the pytest smoke test and scripts/model_smoke.py.
"""

from __future__ import annotations

import time
from typing import Any

import structlog

from backend.agent import planner, selector
from backend.agent.catalog import POSTGRES_EXPECTED_TOOLS, ToolCatalog
from backend.agent.evidence import EvidenceStore
from backend.agent.executor import Executor
from backend.agent.llm.adapter import LLMClient
from backend.agent.models import Plan, PlanStep
from backend.mcp.client_manager import MCPClientManager

log = structlog.get_logger(__name__)

DEFAULT_PROBES: list[tuple[str, str]] = [
    ("Show me today's sales.", "get_daily_sales"),
    ("Compare today's revenue with the previous period.", "compare_sales"),
    ("Which region had the lowest sales?", "get_region_sales"),
    ("Show the sales trend.", "get_sales_trend"),
]


async def run_selection_probes(
    llm: LLMClient, manager: MCPClientManager, prompts: list[tuple[str, str]] | None = None
) -> list[dict[str, Any]]:
    """Run one selection + MCP invocation per prompt; returns result rows."""
    prompts = prompts or DEFAULT_PROBES
    catalog = ToolCatalog.from_manager(manager, POSTGRES_EXPECTED_TOOLS)
    store = EvidenceStore("smoke")
    executor = Executor(manager, catalog, store)
    context = planner.dataset_context()
    plan = Plan(
        goal="fulfil the user's request with one tool call",
        steps=[PlanStep(id=1, action="the user request below", evidence_needed="the relevant sales data")],
        expected_evidence=[],
        completion_criteria="the request is answered with real data",
    )

    rows: list[dict[str, Any]] = []
    for question, expected in prompts:
        started = time.perf_counter()
        response = await llm.chat_with_tools(
            selector.selection_messages(
                question=question,
                plan=plan,
                digest="(none)",
                step_action=question,
                step_evidence_needed="the data that answers the request",
                dataset_context=context,
            ),
            catalog.to_llm_tools(),
            max_tokens=300,
        )
        selection_latency = round((time.perf_counter() - started) * 1000, 1)
        request = selector.parse_selection(response)
        row: dict[str, Any] = {
            "question": question,
            "expected_tool": expected,
            "selected_tool": request.name if request else None,
            "arguments_valid": False,
            "invocation_ok": False,
            "selection_ms": selection_latency,
            "execution_ms": 0.0,
        }
        if request is not None:
            validation = executor.validate(request)
            row["arguments_valid"] = validation.ok
            if validation.ok:
                outcome = await executor.execute(request, 1)
                row["invocation_ok"] = outcome.ok
                row["execution_ms"] = round(outcome.duration_ms, 1)
                row["error"] = outcome.error
            else:
                row["error"] = validation.error
        rows.append(row)
        log.info("smoke_probe", **row)
    return rows
