"""AgentService: wires the LLM adapter, MCP server factories, loop, and registry.

The API layer talks only to this service; it never touches the loop internals
or any provider SDK. All configured MCP servers start per investigation as
independent stdio subprocesses; a timed-out call poisons one session, so
isolation per investigation beats sharing.
"""

from __future__ import annotations

import asyncio
import sys
import time
from collections.abc import Awaitable, Callable

import structlog

from backend.agent.catalog import SERVER_ALLOWLISTS
from backend.reliability import validate_reliability_settings
from backend.agent.llm.adapter import LLMClient, LLMError, OpenAICompatLLM
from backend.agent.loop import InvestigationLoop
from backend.agent.persistence.store import InvestigationStore
from backend.agent.state import InvestigationRecord, InvestigationRegistry
from backend.config import Settings
from backend.mcp.client_manager import MCPClientManager, MultiMCPManager
from backend.mcp.errors import MCPClientError
from backend.reliability import CircuitOpenError
from backend.security.approval_actions import WRITABLE_ACTIONS
from backend.security.approval_service import ApprovalError, ApprovalService, ApprovalUncertainError
from backend.security.approval_service import (
    ApprovalError,
    ApprovalService,
    ApprovalUncertainError,
)

log = structlog.get_logger(__name__)

READINESS_TTL_SECONDS = 30.0


class ReadinessCache:
    """Single-flight TTL cache over the dependency probe (Phase 8 Gate C).

    `get()` returns `(snapshot, from_cache)`. Within the TTL the cached
    snapshot is served without probing. Concurrent callers that arrive while
    a probe is in flight share that ONE probe — they wait on the lock and
    then read the fresh snapshot — so N simultaneous `/ready` requests cause
    exactly one probe, never a storm. The clock is injectable for
    deterministic TTL tests.
    """

    def __init__(self, ttl_seconds: float = READINESS_TTL_SECONDS, *,
                 clock: Callable[[], float] = time.monotonic):
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be > 0")
        self.ttl_seconds = ttl_seconds
        self._clock = clock
        self._snapshot: dict | None = None
        self._expires_at = float("-inf")
        self._lock = asyncio.Lock()

    async def get(self, probe: Callable[[], Awaitable[dict]]) -> tuple[dict, bool]:
        snapshot = self._snapshot
        if snapshot is not None and self._clock() < self._expires_at:
            return snapshot, True
        async with self._lock:
            # re-check under the lock: a concurrent caller may have refreshed
            if self._snapshot is not None and self._clock() < self._expires_at:
                return self._snapshot, True
            self._snapshot = await probe()
            self._expires_at = self._clock() + self.ttl_seconds
            return self._snapshot, False

    def invalidate(self) -> None:
        self._snapshot = None
        self._expires_at = float("-inf")

    @property
    def probe_in_flight(self) -> bool:
        return self._lock.locked()


def readiness_from_health(snapshot: dict) -> tuple[bool, dict]:
    """Derive readiness from an AgentService.health() snapshot (pure).

    REQUIRED for readiness: the LLM provider and every configured MCP
    server — without them the loop cannot understand, plan, or collect any
    evidence, so the application cannot serve its core function.

    NOT required: the Postgres state store. `startup()` deliberately
    degrades to in-memory operation when it is unavailable and read-only
    investigations still work, so its state is reported informationally
    (see AgentService.readiness) and never gates readiness.
    """
    ollama = snapshot.get("ollama", "down: missing probe result")
    mcp_servers = snapshot.get("mcp_servers", {})
    checks: dict = {
        "ollama": {"up": ollama == "up", "detail": ollama},
        "mcp_servers": {
            name: {"up": status.startswith("up"), "detail": status}
            for name, status in mcp_servers.items()
        },
    }
    ready = checks["ollama"]["up"] and all(c["up"] for c in checks["mcp_servers"].values())
    return ready, checks


def stdio_server_factory(settings: Settings, module: str, name: str) -> Callable[[], MCPClientManager]:
    def factory() -> MCPClientManager:
        return MCPClientManager(
            name,
            sys.executable,
            ["-m", module],
            call_timeout_seconds=settings.mcp_call_timeout_seconds,
        )

    return factory


def default_server_factories(settings: Settings) -> dict[str, Callable[[], MCPClientManager]]:
    return {
        "postgres-mcp": stdio_server_factory(settings, "backend.mcp.servers.postgres_mcp", "postgres-mcp"),
        "monitoring-mcp": stdio_server_factory(settings, "backend.mcp.servers.monitoring_mcp", "monitoring-mcp"),
        "github-mcp": stdio_server_factory(settings, "backend.mcp.servers.github_mcp", "github-mcp"),
    }


class AgentService:
    def __init__(
        self,
        settings: Settings,
        *,
        llm: LLMClient | None = None,
        manager_factory: Callable[[], MultiMCPManager] | None = None,
        server_factories: dict[str, Callable[[], MCPClientManager]] | None = None,
        allowlist: dict[str, frozenset[str]] = SERVER_ALLOWLISTS,
        store: InvestigationStore | None = None,
    ):
        self.settings = settings
        self.llm = llm or OpenAICompatLLM(
            base_url=f"{settings.ollama_host}/v1",
            model=settings.ollama_model,
            timeout_seconds=settings.llm_timeout_seconds,
            temperature=settings.llm_temperature,
            retry_attempts=settings.llm_retry_attempts,
            retry_backoff_seconds=settings.llm_retry_backoff_seconds,
        )
        validate_reliability_settings(settings)
        self.allowlist = allowlist
        self._custom_factory = manager_factory
        self._server_factories = server_factories or default_server_factories(settings)
        self._manager_factory = manager_factory or (
            lambda: MultiMCPManager(
                self._server_factories,
                rate_limits_per_minute={"github-mcp": 60},
                default_rate_limit_per_minute=settings.rate_limit_per_server_per_minute,
                circuit_failure_threshold=settings.circuit_failure_threshold,
                circuit_cooldown_seconds=settings.circuit_cooldown_seconds,
            )
        )
        self.registry = InvestigationRegistry()
        self.store: InvestigationStore | None = (
            store if store is not None else InvestigationStore()
        )
        self.approvals: ApprovalService | None = ApprovalService(
            store=self.store,
            monitoring_session_factory=lambda: MultiMCPManager(
                {"monitoring-mcp": self._server_factories["monitoring-mcp"]}
            ),
            execution_timeout_seconds=settings.approval_execution_timeout_seconds,
        )
        self._tasks: set[asyncio.Task] = set()
        self._readiness = ReadinessCache()

    async def startup(self) -> None:
        """Initialize the state schema and conservatively resolve anything a
        restart left half-finished (running -> interrupted, running tool
        calls -> unknown). Never blocks app start on a down database."""
        if self.store is None:
            return
        try:
            async with asyncio.timeout(self.settings.state_store_init_timeout_seconds):
                version = await self.store.ensure_schema()
                interrupted = await self.store.mark_interrupted()
            log.info("state_store_ready", schema_version=version, interrupted=interrupted)
        except Exception as exc:
            log.warning("state_store_unavailable", error=str(exc)[:200])
            self.store = None  # degrade to in-memory operation
            if self.approvals is not None:
                self.approvals.store = None  # proposals need persistence

    async def list_approvals(self, status: str | None = None) -> list[ApprovalRecord]:
        if self.store is None:
            return []
        try:
            return await self.store.list_approvals(status)
        except Exception as exc:
            log.warning("state_store_list_approvals_failed", error=str(exc)[:200])
            return []

    async def get_approval(self, approval_id: str) -> ApprovalRecord | None:
        if self.store is None:
            return None
        try:
            return await self.store.get_approval(approval_id)
        except Exception as exc:
            log.warning("state_store_get_approval_failed", error=str(exc)[:200])
            return None

    async def decide_approval(
        self, approval_id: str, decision: str, decided_by: str, comment: str | None = None
    ) -> ApprovalRecord:
        if self.approvals is None:
            raise ApprovalError("approval service unavailable")
        return await self.approvals.decide(approval_id, decision, decided_by, comment)

    async def execute_approval(self, approval_id: str, executed_by: str) -> ApprovalRecord:
        if self.approvals is None:
            raise ApprovalError("approval service unavailable")
        try:
            return await self.approvals.execute(approval_id, executed_by)
        except ApprovalUncertainError:
            # outcome unknown: the row is `uncertain`; expose it, never replay
            record = await self.store.get_approval(approval_id) if self.store else None
            if record is not None:
                return record
            raise
        except CircuitOpenError as exc:
            raise ApprovalError(f"monitoring source unavailable; write not attempted: {exc}") from exc

    async def get_investigation(self, investigation_id: str) -> InvestigationRecord | None:
        """Live record first, persisted record second (survives restarts)."""
        record = self.registry.get(investigation_id)
        if record is not None:
            return record
        if self.store is not None:
            try:
                return await self.store.load_investigation(investigation_id)
            except Exception as exc:
                log.warning("state_store_load_failed", error=str(exc)[:200])
        return None

    def start_investigation(self, question: str) -> InvestigationRecord:
        record = self.registry.create(question)
        task = asyncio.create_task(self._run(record))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        log.info(
            "investigation_started",
            investigation_id=record.investigation_id,
            request_id=record.request_id,
        )
        return record

    async def _run(self, record: InvestigationRecord) -> None:
        loop = InvestigationLoop(
            llm=self.llm,
            manager_factory=self._manager_factory,
            record=record,
            settings=self.settings,
            allowlist=self.allowlist,
            state_store=self.store,
            approval_service=self.approvals,
        )
        result = await loop.run()
        if result.status == "running":  # loop.run() always finalizes; belt for regressions
            result.status = "failed"
            result.failure_reason = "loop returned without finalizing"

    async def health(self) -> dict:
        ollama = "up"
        try:
            await asyncio.wait_for(self.llm.check_provider(), timeout=8)
        except (LLMError, asyncio.TimeoutError) as exc:
            ollama = f"down: {exc}"

        server_status: dict[str, str] = {}
        if self._custom_factory is not None:
            try:
                async with asyncio.timeout(25):
                    async with self._manager_factory() as managers:
                        total = sum(len(manager.tools()) for manager in managers.managers.values())
                server_status["mcp"] = f"up ({total} tools)"
            except Exception as exc:
                server_status["mcp"] = f"down: {type(exc).__name__}: {exc}"
        else:
            for server, factory in self._server_factories.items():
                try:
                    async with asyncio.timeout(20):
                        async with factory() as manager:
                            server_status[server] = f"up ({len(manager.tools())} tools)"
                except Exception as exc:
                    server_status[server] = f"down: {type(exc).__name__}"
        return {"status": "ok", "model": self.llm.model_name, "ollama": ollama, "mcp_servers": server_status}

    async def readiness(self) -> tuple[bool, dict, bool]:
        """Readiness via the same dependency probe /api/health uses, cached
        30s with single-flight (concurrent callers share one probe).

        Returns (ready, checks, from_cache). The state-store entry is
        informational only: startup() intentionally degrades to in-memory
        operation when PostgreSQL is unavailable, so it never gates
        readiness (see readiness_from_health for the required set)."""
        snapshot, from_cache = await self._readiness.get(self.health)
        ready, checks = readiness_from_health(snapshot)
        checks["state_store"] = {
            "gating": False,
            "mode": "in_memory_fallback" if self.store is None else "persistent",
        }
        return ready, checks, from_cache

    async def shutdown(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        if self.store is not None:
            try:
                await self.store.close()
            except Exception as exc:
                log.warning("state_store_close_failed", error=str(exc)[:200])
        if self.approvals is not None:
            await self.approvals.shutdown()
