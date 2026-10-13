"""The InvestigationLoop — an explicit, bounded state machine.

UNDERSTAND -> PLAN -> (SELECT -> VALIDATE -> EXECUTE -> OBSERVE)* -> CORRELATE
-> VERIFY/REPORT. Bounded by max steps, max LLM calls, and wall-clock; it
always terminates with a complete record or a typed failure, and never
fabricates evidence (the verifier rejects ungrounded claims).
"""

from __future__ import annotations

import asyncio
import structlog

from backend.agent import planner, selector, synthesizer
from backend.agent.catalog import SERVER_ALLOWLISTS, ToolCatalog
from backend.agent.evidence import EvidenceStore
from backend.agent.executor import Executor
from backend.agent.llm.adapter import LLMClient, LLMError
from backend.agent.models import StepRecord, ToolCallObservation, VerificationResult
from backend.agent.state import InvestigationRecord
from backend.agent.verifier import verify
from backend.config import Settings
from backend.mcp.client_manager import MultiMCPManager
from backend.mcp.errors import MCPClientError

log = structlog.get_logger(__name__)


class InvestigationLoop:
    def __init__(
        self,
        *,
        llm: LLMClient,
        manager_factory,
        record: InvestigationRecord,
        settings: Settings,
        allowlist: dict[str, frozenset[str]] = SERVER_ALLOWLISTS,
    ):
        self.llm = llm
        self._manager_factory = manager_factory
        self.record = record
        self.settings = settings
        self.allowlist = allowlist
        self.store = EvidenceStore(record.investigation_id)
        self._llm_tools: list[dict] = []
        self._context = planner.dataset_context()
        self._finished_early = False

    # -- public ------------------------------------------------------------

    async def run(self) -> InvestigationRecord:
        record = self.record
        try:
            async with asyncio.timeout(self.settings.max_investigation_seconds):
                async with self._manager_factory() as managers:
                    await self._investigate(managers)
        except TimeoutError:
            self._fail("wall-clock budget exceeded before completion")
        except LLMError as exc:
            self._fail(f"llm failure: {exc}")
        except MCPClientError as exc:
            self._fail(f"mcp failure: {exc}")
        except Exception as exc:  # orchestrator boundary: typed wrap, never hang
            log.exception("investigation_unexpected_error", investigation_id=record.investigation_id)
            self._fail(f"unexpected {type(exc).__name__}: {exc}")
        return record

    # -- phases --------------------------------------------------------------

    async def _investigate(self, managers: MultiMCPManager) -> None:
        record = self.record
        catalog = ToolCatalog.from_managers(managers.managers, self.allowlist)
        record.states_visited.append("discover")
        if not catalog.allowlisted_entries():
            raise MCPClientError(
                "no allowlisted tools discovered on any configured server; "
                f"discovered: {[f'{e.server}.{e.name}' for e in catalog.entries]}"
            )
        self._llm_tools = catalog.to_llm_tools()
        log.info(
            "tools_discovered",
            investigation_id=record.investigation_id,
            servers={server: len([e for e in catalog.entries if e.server == server]) for server in managers.managers},
            allowlisted=len(catalog.allowlisted_entries()),
        )

        record.states_visited.append("understand")
        record.understanding = await self._llm_call(
            "understand", planner.understand(self.llm, record.question, self._context)
        )

        record.states_visited.append("plan")
        record.plan = await self._llm_call(
            "plan",
            planner.make_plan(self.llm, record.question, record.understanding, catalog.names(), self.settings),
        )
        log.info(
            "investigation_planned",
            investigation_id=record.investigation_id,
            steps=len(record.plan.steps),
            tools=len(catalog.names()),
        )

        executor = Executor(managers, catalog, self.store)
        for plan_step in record.plan.steps[: self.settings.max_investigation_steps]:
            if self._finished_early:
                break
            record.states_visited.append("select")
            step_record = StepRecord(index=plan_step.id, action=plan_step.action)
            record.steps.append(step_record)
            await self._run_step(plan_step, executor, step_record)

        record.states_visited.append("correlate")
        # compact digest: the synthesis turn (and its repair turn) must fit a
        # small default context window plus the max_tokens output budget
        digest = self.store.digest(max_chars=1100)

        record.states_visited.append("verify_report")
        if len(self.store) == 0:
            self._fail("no evidence could be collected; nothing to report")
            return
        if self._llm_calls_left() <= 0:
            self._fail("LLM call budget exhausted before synthesis")
            return
        report = await self._llm_call(
            "synthesize", synthesizer.synthesize(self.llm, record.question, record.plan, digest)
        )
        verified, result = verify(report, self.store)
        record.report = verified
        record.verification = result
        if result.dropped_claims or result.downgraded:
            record.warnings.append("report adjusted by verifier: " + "; ".join(result.notes))
        record.status = "complete"
        record.finished_at = _now()
        log.info(
            "investigation_complete",
            investigation_id=record.investigation_id,
            evidence=len(self.store),
            confidence=verified.confidence.value,
            verifier_ok=result.ok,
        )

    async def _run_step(self, plan_step, executor: Executor, step_record: StepRecord) -> None:
        record = self.record
        repair_feedback: str | None = None
        for attempt in (1, 2):
            if self._llm_calls_left() <= 0:
                record.warnings.append("LLM call budget exhausted mid-investigation")
                step_record.status = "skipped"
                self._finished_early = True
                return
            response = await self._llm_call(
                "select_tool",
                self.llm.chat_with_tools(
                    selector.selection_messages(
                        question=record.question,
                        plan=record.plan,
                        digest=self.store.digest(max_chars=900),
                        step_action=plan_step.action,
                        step_evidence_needed=plan_step.evidence_needed,
                        dataset_context=self._context,
                        repair_feedback=repair_feedback,
                        recent_failures=self._recent_failures(),
                    ),
                    self._llm_tools,
                    max_tokens=400,
                ),
            )
            request = selector.parse_selection(response)
            if request is None:
                # Model signalled completion (or refused): stop selecting.
                log.info("model_signalled_completion", investigation_id=record.investigation_id, step=plan_step.id)
                step_record.status = "skipped"
                self._finished_early = True
                return

            validation = executor.validate(request)
            if not validation.ok and attempt == 1:
                repair_feedback = validation.error
                record.states_visited.append("validate")
                record.tool_calls.append(
                    ToolCallObservation(
                        step=plan_step.id, tool=request.name, arguments=request.arguments,
                        attempt=attempt, status="error", error_class="InvalidSelection",
                        error=validation.error,
                    )
                )
                log.warning(
                    "tool_selection_rejected",
                    investigation_id=record.investigation_id,
                    tool=request.name,
                    error=validation.error,
                    action="one_repair_attempt",
                )
                continue

            record.states_visited.append("validate")
            if not validation.ok:
                record.tool_calls.append(
                    ToolCallObservation(
                        step=plan_step.id, tool=request.name, arguments=request.arguments,
                        attempt=attempt, status="error", error_class="InvalidArguments",
                        error=validation.error,
                    )
                )
                step_record.status = "failed"
                log.warning(
                    "tool_selection_failed_after_repair",
                    investigation_id=record.investigation_id,
                    tool=request.name,
                    error=validation.error,
                )
                return

            record.states_visited.append("execute")
            outcome = await executor.execute(request, plan_step.id)  # OBSERVE inside executor
            record.tool_calls.append(
                ToolCallObservation(
                    step=plan_step.id,
                    tool=request.name,
                    arguments=request.arguments,
                    attempt=attempt,
                    status="ok" if outcome.ok else "error",
                    error_class=outcome.error_class,
                    error=outcome.error,
                    duration_ms=outcome.duration_ms,
                    evidence_id=outcome.evidence_id,
                )
            )
            if outcome.ok:
                step_record.status = "ok"
                step_record.evidence_ids.append(outcome.evidence_id)
                record.evidence_ids.append(outcome.evidence_id)
                return

            # Tool failure: typed, recorded, never blindly retried — the next
            # step may still find alternate evidence.
            step_record.status = "failed"
            record.warnings.append(f"step {plan_step.id}: {request.name} failed ({outcome.error_class})")
            return

    # -- helpers -----------------------------------------------------------

    def _recent_failures(self, limit: int = 2) -> list[str]:
        """Compact texts of the most recent tool failures, so the model can
        correct course instead of repeating a rejected call."""
        failures = [
            f"{obs.tool} (step {obs.step}) failed: {(obs.error or obs.error_class or 'unknown')[:200]}"
            for obs in self.record.tool_calls
            if obs.status == "error"
        ]
        return failures[-limit:]

    async def _llm_call(self, purpose: str, coro):
        self.record.llm_calls += 1
        log.info(
            "llm_call",
            investigation_id=self.record.investigation_id,
            request_id=self.record.request_id,
            purpose=purpose,
            call_number=self.record.llm_calls,
            agent_state=self.record.states_visited[-1] if self.record.states_visited else None,
        )
        return await coro

    def _llm_calls_left(self) -> int:
        return self.settings.max_llm_calls - self.record.llm_calls

    def _fail(self, reason: str) -> None:
        self.record.status = "failed"
        self.record.failure_reason = reason
        self.record.finished_at = _now()
        log.error(
            "investigation_failed",
            investigation_id=self.record.investigation_id,
            reason=reason,
            evidence=len(self.store),
        )


def _now():
    from datetime import datetime, timezone

    return datetime.now(timezone.utc)
