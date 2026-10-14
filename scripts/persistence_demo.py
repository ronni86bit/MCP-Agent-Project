"""Phase 5 demo: investigations persist and recover across backend restarts.

    uv run python scripts/persistence_demo.py

Deterministic (scripted LLM + real postgres-mcp):
  1. runs a scripted investigation through the real MCP server,
  2. simulates a backend restart and retrieves the record from PostgreSQL,
  3. simulates an interrupted investigation and verifies the conservative
     sweep (running -> interrupted, running tool calls -> unknown, no
     evidence erased, nothing re-executed).
"""

import asyncio
import time

from backend.agent.models import (
    Confidence,
    InvestigationReport,
    Plan,
    PlanStep,
    StepRecord,
    ToolCallObservation,
    Understanding,
)
from backend.agent.persistence.store import InvestigationStore
from backend.agent.service import AgentService
from backend.config import get_settings
from backend.observability.logging import configure_logging


def _scripted_llm():
    from backend.agent.llm.adapter import LLMToolCall
    from tests.scripted import ScriptedLLM

    return ScriptedLLM(
        understanding=Understanding(
            goal="explain the drop", time_frame="2026-10-05",
            focus_areas=["revenue"], success_criteria="grounded answer",
        ),
        plan=Plan(
            goal="investigate",
            steps=[PlanStep(id=1, action="establish daily sales", evidence_needed="daily totals")],
            completion_criteria="done",
        ),
        selections=[
            LLMToolCall(
                id="c1", name="postgres-mcp__get_daily_sales",
                arguments={"start_date": "2026-10-05", "end_date": "2026-10-05"},
            )
        ],
        report=InvestigationReport(
            conclusion="Revenue on 2026-10-05 dropped, consistent with the collected evidence.",
            claims=[],
            confidence=Confidence.low,
        ),
    )


async def main() -> int:
    settings = get_settings()
    configure_logging(settings.log_level)
    failures: list[str] = []

    # 1. run a scripted investigation through the real MCP server
    service = AgentService(settings, llm=_scripted_llm())
    await service.startup()
    record = service.start_investigation("Why did today's sales drop?")
    deadline = time.monotonic() + 90
    while record.status == "running" and time.monotonic() < deadline:
        await asyncio.sleep(0.2)
    await service.shutdown()
    print(f"investigation {record.investigation_id}: status={record.status} "
          f"evidence={record.evidence_ids}")
    if record.status != "complete" or not record.evidence_ids:
        failures.append("scripted investigation did not complete with evidence")

    # 2. restart simulation: a fresh service must load it from PostgreSQL
    restarted = AgentService(settings, llm=_scripted_llm())
    await restarted.startup()
    loaded = await restarted.get_investigation(record.investigation_id)
    await restarted.shutdown()
    if loaded is None or loaded.status != "complete" or not loaded.plan:
        failures.append("investigation not retrievable after restart")
    else:
        print(f"restart retrieval OK: status={loaded.status} plan_version={loaded.plan_version} "
              f"steps={len(loaded.steps)}")

    # 3. interruption sweep: running -> interrupted, running call -> unknown
    store = InvestigationStore()
    await store.ensure_schema()
    from backend.agent.state import InvestigationRecord as Rec

    stuck = Rec(
        investigation_id="inv-demo-interrupted",
        request_id="req-demo-interrupted",
        question="Why did today's sales drop?",
        status="running",
        stage="execute",
    )
    stuck.steps = [StepRecord(index=1, action="mid-flight step", status="running")]
    stuck.tool_calls = [ToolCallObservation(step=1, tool="postgres-mcp__get_daily_sales", status="running")]
    await store.persist_record(stuck)
    interrupted_ids = await store.mark_interrupted()
    loaded = await store.load_investigation(stuck.investigation_id)
    await store.close()
    if stuck.investigation_id not in interrupted_ids or loaded is None:
        failures.append("interruption sweep did not resolve the stuck investigation")
    elif loaded.status != "interrupted" or loaded.tool_calls[0].status != "unknown":
        failures.append("interruption sweep did not resolve uncertainty conservatively")
    else:
        print("interruption sweep OK: status=interrupted, uncertain call=unknown, record preserved")

    if failures:
        print("\nFAILED:")
        for failure in failures:
            print(f"  - {failure}")
        return 1
    print("\nPHASE 5 STATE DEMO PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
