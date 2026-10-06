"""Phase 3 demo: the canonical investigation end to end.

    uv run python scripts/agent_demo.py

Requires Ollama running with the configured model and the seeded PostgreSQL.
Runs: UNDERSTAND -> PLAN -> tool selection/execution via postgres-mcp ->
CORRELATE -> VERIFY -> REPORT, then prints the grounded report.
"""

import asyncio
import sys
import time

from backend.agent.llm.adapter import LLMError, OpenAICompatLLM
from backend.agent.service import AgentService
from backend.config import get_settings
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

    service = AgentService(settings, llm=llm)
    try:
        record = service.start_investigation("Why did today's sales drop?")
        deadline = time.monotonic() + settings.max_investigation_seconds + 30
        while record.status == "running" and time.monotonic() < deadline:
            await asyncio.sleep(2)

        print(f"\n=== Investigation {record.investigation_id} — status: {record.status} ===")
        if record.understanding:
            print(f"Goal: {record.understanding.goal} | timeframe: {record.understanding.time_frame}")
        if record.plan:
            print("Plan:")
            for step in record.plan.steps:
                print(f"  {step.id}. {step.action}")
        print("Tool calls:")
        for obs in record.tool_calls:
            print(
                f"  step {obs.step}: {obs.tool} (attempt {obs.attempt}) -> {obs.status}"
                + (f" [{obs.error_class}]" if obs.error_class else "")
                + (f" evidence={obs.evidence_id}" if obs.evidence_id else "")
            )
        print(f"Evidence: {record.evidence_ids}")
        if record.report:
            print("\n--- REPORT ---")
            print("Conclusion:", record.report.conclusion)
            print("Confidence:", record.report.confidence.value, "—", record.report.confidence_rationale)
            for claim in record.report.claims:
                print(f"  claim: {claim.text}  {claim.evidence_ids}")
            for caveat in record.report.caveats:
                print(f"  caveat: {caveat}")
            for gap in record.report.evidence_gaps:
                print(f"  gap: {gap}")
        elif record.failure_reason:
            print("Failure reason:", record.failure_reason)

        ok = (
            record.status == "complete"
            and record.report is not None
            and len(record.evidence_ids) >= 1
            and all(claim.evidence_ids for claim in record.report.claims)
        )
        print("\nPHASE 3 DEMO PASSED" if ok else "\nPHASE 3 DEMO FAILED")
        return 0 if ok else 1
    finally:
        await service.shutdown()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
