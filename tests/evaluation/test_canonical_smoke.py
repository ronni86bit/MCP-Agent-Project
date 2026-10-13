"""Canonical cross-system smoke test: the real model must correlate sales,
operational telemetry, and engineering/deployment evidence through MCP and
produce a grounded structured report.

compare_sales observations are checked for equal-duration windows; evidence
must span all three systems; every claim must cite evidence ids.
"""

import asyncio
import time
from datetime import date

import pytest

from backend.agent.service import AgentService
from backend.config import Settings

pytestmark = pytest.mark.ollama


def _window_days(observation_arguments: dict) -> tuple[int, int] | None:
    try:
        a = (date.fromisoformat(observation_arguments["window_a_end"])
             - date.fromisoformat(observation_arguments["window_a_start"])).days + 1
        b = (date.fromisoformat(observation_arguments["window_b_end"])
             - date.fromisoformat(observation_arguments["window_b_start"])).days + 1
    except (KeyError, TypeError, ValueError):
        return None
    return a, b


async def test_canonical_question_correlates_three_systems(real_llm):
    settings = Settings(max_investigation_seconds=1200, max_llm_calls=16)
    service = AgentService(settings, llm=real_llm)
    try:
        record = service.start_investigation("Why did today's sales drop?")
        deadline = time.monotonic() + 1200
        while record.status == "running" and time.monotonic() < deadline:
            await asyncio.sleep(2)

        assert record.status == "complete", f"failure_reason={record.failure_reason}"
        assert record.plan and len(record.plan.steps) >= 1
        assert len(record.evidence_ids) >= 3, "a cross-system investigation needs several evidence records"

        evidence_servers = {obs.tool.split("__")[0] for obs in record.tool_calls if obs.status == "ok"}
        assert evidence_servers >= {"postgres-mcp", "monitoring-mcp", "github-mcp"}, evidence_servers

        assert record.report is not None
        assert len(record.report.conclusion) > 30
        assert record.report.claims, "the report must carry evidence-backed claims"
        for claim in record.report.claims:
            assert claim.evidence_ids
        assert record.verification is not None

        for obs in record.tool_calls:
            if obs.tool.endswith("__compare_sales") and obs.status == "ok":
                windows = _window_days(obs.arguments)
                assert windows is not None and windows[0] == windows[1], (
                    f"unequal-duration comparison reached execution: {obs.arguments}"
                )

        print("\n--- canonical cross-system report ---")
        print("conclusion:", record.report.conclusion)
        print("confidence:", record.report.confidence.value, "-", record.report.confidence_rationale)
        for claim in record.report.claims:
            print("claim:", claim.text, claim.evidence_ids)
        print("gaps:", record.report.evidence_gaps)
    finally:
        await service.shutdown()
