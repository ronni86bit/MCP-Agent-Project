"""Canonical smoke test: the real model must investigate the sales drop
end-to-end through MCP and produce a grounded structured report.

compare_sales observations are checked for equal-duration windows: the model
must not compare unlike periods and read raw totals as percentage changes.
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


async def test_canonical_question_end_to_end(pg_manager, real_llm):
    settings = Settings(max_investigation_seconds=900, max_llm_calls=16)
    service = AgentService(settings, llm=real_llm, manager_factory=pg_manager)
    try:
        record = service.start_investigation("Why did today's sales drop?")
        deadline = time.monotonic() + 900
        while record.status == "running" and time.monotonic() < deadline:
            await asyncio.sleep(2)

        assert record.status == "complete", f"failure_reason={record.failure_reason}"
        assert record.plan and len(record.plan.steps) >= 1
        assert len(record.evidence_ids) >= 2, "the investigation must collect several evidence records"
        assert record.report is not None
        assert len(record.report.conclusion) > 30
        assert record.report.claims, "the report must carry evidence-backed claims"
        for claim in record.report.claims:
            assert claim.evidence_ids
        assert record.verification is not None

        comparisons = [obs for obs in record.tool_calls if obs.tool == "compare_sales"]
        assert comparisons, "the canonical investigation should compare against a baseline"
        for obs in comparisons:
            if obs.status != "ok":
                continue
            windows = _window_days(obs.arguments)
            assert windows is not None, f"compare_sales without parsable windows: {obs.arguments}"
            assert windows[0] == windows[1], (
                f"unequal-duration comparison reached execution: {obs.arguments}"
            )

        print("\n--- canonical report ---")
        print("conclusion:", record.report.conclusion)
        print("confidence:", record.report.confidence.value, "-", record.report.confidence_rationale)
        for claim in record.report.claims:
            print("claim:", claim.text, claim.evidence_ids)
        print("gaps:", record.report.evidence_gaps)
    finally:
        await service.shutdown()
