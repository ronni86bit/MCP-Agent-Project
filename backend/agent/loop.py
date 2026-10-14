"""The InvestigationLoop — an explicit, bounded, checkpointed state machine.

UNDERSTAND -> PLAN -> (SELECT -> VALIDATE -> EXECUTE -> OBSERVE -> REVIEW)*
-> CORRELATE -> VERIFY/REPORT. Bounded by max steps, max LLM calls,
max plan revisions, and wall-clock; every transition is checkpointed to the
state store (write-through, degrading gracefully to in-memory if the store is
unavailable). The loop never fabricates evidence and never re-executes
operations whose status is uncertain after an interruption.
"""

from __future__ import annotations

import asyncio
import structlog

from backend.agent import planner, plan_reviewer, selector, synthesizer
from backend.agent.catalog import SERVER_ALLOWLISTS, ToolCatalog
from backend.agent.evidence import EvidenceStore
from backend.agent.executor import Executor
from backend.agent.llm.adapter import LLMClient, LLMError
from backend.agent.models import (
    PlanVersionInfo,
    StepRecord,
    ToolCallObservation,
    VerificationResult,
)
from backend.agent.persistence.store import InvestigationStore
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
        state_store: InvestigationStore | None = None,
    ):
        self.llm = llm
        self._manager_factory = manager_factory
        self.record = record
        self.settings = settings
        self.allowlist = allowlist
        self.state_store = state_store
        self.evidence = EvidenceStore(record.investigation_id)
        self._llm_tools: list[dict] = []
        self._context = planner.dataset_context()
        self._finished_early = False
        self._revisions_used = 0

    # -- public --------------------------------------------------------------

    async def run(self) -> InvestigationRecord:
        record = self.record
        try:
            async with asyncio.timeout(self.settings.max_investigation_seconds):
                await self._checkpoint("created")
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
        await self._checkpoint("status_" + record.status)
        return record

    # -- phases ---------------------------------------------------------------

    async def _investigate(self, managers: MultiMCPManager) -> None:
        record = self.record
        catalog = ToolCatalog.from_managers(managers.managers, self.allowlist)
        record.states_visited.append("discover")
        await self._checkpoint("tools_discovered")
        if not catalog.allowlisted_entries():
            raise MCPClientError(
                "no allowlisted tools discovered on any configured server; "
                f"discovered: {[f'{e.server}.{e.name}' for e in catalog.entries]}"
            )
        self._llm_tools = catalog.to_llm_tools()
        log.info(
            "tools_discovered",
            investigation_id=record.investigation_id,
            servers={
                server: len([e for e in catalog.entries if e.server == server])
                for server in managers.managers
            },
            allowlisted=len(catalog.allowlisted_entries()),
        )

        record.states_visited.append("understand")
        record.understanding = await self._llm_call(
            "understand", planner.understand(self.llm, record.question, self._context)
        )
        await self._checkpoint("understanding_saved")

        record.states_visited.append("plan")
        plan = await self._llm_call(
            "plan",
            planner.make_plan(self.llm, record.question, record.understanding, catalog.names(), self.settings),
        )
        self._adopt_plan(plan, revision_reason="")
        log.info(
            "investigation_planned",
            investigation_id=record.investigation_id,
            steps=len(plan.steps),
            tools=len(catalog.names()),
        )

        executor = Executor(managers, catalog, self.evidence)
        attempted: set[tuple[int, int]] = set()  # (plan_version, plan_step_id)

        while True:
            if self._finished_early:
                break
            if len(self.record.steps) >= self.settings.max_investigation_steps:
                record.warnings.append("step budget exhausted; remaining plan steps were not executed")
                break
            plan = record.plan
            assert plan is not None
            plan_step = next(
                (s for s in plan.steps if (record.plan_version, s.id) not in attempted), None
            )
            if plan_step is None:
                break  # every step of the current plan has been attempted

            record.states_visited.append("select")
            step_record = StepRecord(
                index=len(record.steps) + 1,
                plan_version=record.plan_version,
                plan_step_id=plan_step.id,
                action=plan_step.action,
                status="running",
            )
            record.steps.append(step_record)
            plan_step.status = "running"
            await self._checkpoint("step_started")
            step_instruction = plan_step.action + (
                f" [use tool: {plan_step.tool}]" if plan_step.tool else ""
            )
            await self._run_step(plan_step, executor, step_record, step_instruction)
            attempted.add((record.plan_version, plan_step.id))

            await self._maybe_revise(plan, attempted)

        record.states_visited.append("correlate")
        # compact digest: the synthesis turn (and its repair turn) must fit a
        # small default context window plus the max_tokens output budget
        digest = self.evidence.digest(max_chars=1100)

        record.states_visited.append("verify_report")
        if len(self.evidence) == 0:
            self._fail("no evidence could be collected; nothing to report")
            return
        if self._llm_calls_left() <= 0:
            self._fail("LLM call budget exhausted before synthesis")
            return
        report = await self._llm_call(
            "synthesize", synthesizer.synthesize(self.llm, record.question, record.plan, digest)
        )
        verified, result = verify(report, self.evidence)
        record.report = verified
        record.verification = result
        if result.dropped_claims or result.downgraded:
            record.warnings.append("report adjusted by verifier: " + "; ".join(result.notes))
        record.status = "complete"
        record.stage = "complete"
        record.finished_at = _now()
        await self._checkpoint("report_saved")
        log.info(
            "investigation_complete",
            investigation_id=record.investigation_id,
            evidence=len(self.evidence),
            confidence=verified.confidence.value,
            verifier_ok=result.ok,
            flagged=len(result.flagged_claims),
        )

    async def _run_step(self, plan_step, executor: Executor, step_record: StepRecord,
                        step_instruction: str) -> None:
        record = self.record
        repair_feedback: str | None = None
        for attempt in (1, 2):
            if self._llm_calls_left() <= 0:
                record.warnings.append("LLM call budget exhausted mid-investigation")
                step_record.status = "skipped"
                plan_step.status = "skipped"
                self._finished_early = True
                await self._checkpoint("step_skipped")
                return
            response = await self._llm_call(
                "select_tool",
                self.llm.chat_with_tools(
                    selector.selection_messages(
                        question=record.question,
                        plan=record.plan,
                        digest=self.evidence.digest(max_chars=900),
                        step_action=step_instruction,
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
                plan_step.status = "skipped"
                self._finished_early = True
                await self._checkpoint("step_skipped")
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
                await self._checkpoint("tool_call_recorded")
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
                await self._checkpoint("tool_call_recorded")
                step_record.status = "failed"
                plan_step.status = "failed"
                await self._checkpoint("step_failed")
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
                step_record.status = "completed"
                plan_step.status = "completed"
                step_record.evidence_ids.append(outcome.evidence_id)
                record.evidence_ids.append(outcome.evidence_id)
                record.evidence.append(self.evidence.get(outcome.evidence_id))
                await self._checkpoint("evidence_added")
                return

            # Tool failure: typed, recorded, never blindly retried — plan
            # review (below) decides whether to change direction.
            step_record.status = "failed"
            plan_step.status = "failed"
            record.warnings.append(f"step {plan_step.id}: {request.name} failed ({outcome.error_class})")
            await self._checkpoint("step_failed")
            return

    async def _maybe_revise(self, attempted_plan: object, attempted: set[tuple[int, int]]) -> None:
        """Post-step review: continue, or adopt a bounded revised plan."""
        record = self.record
        if self._finished_early or self.record.plan is None:
            return
        remaining = any(
            (record.plan_version, step.id) not in attempted for step in record.plan.steps
        )
        if not remaining:
            return  # nothing left to revise
        if self._revisions_used >= self.settings.max_plan_revisions:
            return
        if self._llm_calls_left() <= 4:  # reserve budget for a selection + synthesis
            return

        try:
            decision = await self._llm_call(
                "review_plan",
                plan_reviewer.review_plan(
                    self.llm,
                    record.question,
                    record.plan,
                    self.evidence.digest(max_chars=900),
                    self.settings,
                ),
            )
        except LLMError as exc:
            # the review is advisory: a malformed or failed review degrades to
            # "continue with the current plan", never aborts the investigation
            record.warnings.append(f"plan review failed; continuing current plan ({str(exc)[:160]})")
            log.warning("plan_review_failed", investigation_id=record.investigation_id)
            return
        if decision.action != "revise" or decision.plan is None:
            return
        if not decision.plan.steps:
            record.warnings.append("plan revision ignored: proposed plan had no steps")
            return

        self._revisions_used += 1
        for info in record.plan_history:
            info.superseded = True
        self._adopt_plan(
            decision.plan,
            revision_reason=decision.revision_reason or f"revision {self._revisions_used}",
        )
        record.warnings.append(
            f"plan revised to version {record.plan_version}: "
            f"{decision.revision_reason[:200] or 'no reason given'}"
        )
        log.info(
            "plan_revised",
            investigation_id=record.investigation_id,
            version=record.plan_version,
            steps=len(decision.plan.steps),
        )
        await self._checkpoint("plan_revised")

    def _adopt_plan(self, plan: Plan, *, revision_reason: str) -> None:
        """Install a plan as the current version, preserving history."""
        record = self.record
        bounded = plan.model_copy(update={"steps": plan.steps[: self.settings.max_plan_steps]})
        if len(bounded.steps) != len(plan.steps):
            log.warning("plan_truncated_to_budget", steps=len(plan.steps))
        record.plan_version += 1
        record.plan = bounded
        record.plan_history.append(
            PlanVersionInfo(
                version=record.plan_version,
                plan=bounded,
                revision_reason=revision_reason,
                superseded=False,
            )
        )

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

    async def _checkpoint(self, event: str, payload: dict | None = None) -> None:
        """Write-through persistence; degrades to in-memory if the store fails."""
        self.record.touch()
        if self.state_store is None:
            return
        try:
            await self.state_store.persist_record(self.record)
            await self.state_store.append_event(self.record.investigation_id, event, payload)
        except Exception as exc:
            log.warning(
                "state_persistence_failed",
                investigation_id=self.record.investigation_id,
                event=event,
                error=str(exc)[:200],
            )

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
        self.record.stage = "failed"
        self.record.failure_reason = reason
        self.record.finished_at = _now()
        log.error(
            "investigation_failed",
            investigation_id=self.record.investigation_id,
            reason=reason,
            evidence=len(self.evidence),
        )


def _now():
    from datetime import datetime, timezone

    return datetime.now(timezone.utc)
