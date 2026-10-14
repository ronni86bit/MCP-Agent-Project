"""AgentService: wires the LLM adapter, MCP server factories, loop, and registry.

The API layer talks only to this service; it never touches the loop internals
or any provider SDK. All configured MCP servers start per investigation as
independent stdio subprocesses; a timed-out call poisons one session, so
isolation per investigation beats sharing.
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Callable

import structlog

from backend.agent.catalog import SERVER_ALLOWLISTS
from backend.agent.llm.adapter import LLMClient, LLMError, OpenAICompatLLM
from backend.agent.loop import InvestigationLoop
from backend.agent.persistence.store import InvestigationStore
from backend.agent.state import InvestigationRecord, InvestigationRegistry
from backend.config import Settings
from backend.mcp.client_manager import MCPClientManager, MultiMCPManager

log = structlog.get_logger(__name__)


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
        )
        self.allowlist = allowlist
        self._custom_factory = manager_factory
        self._server_factories = server_factories or default_server_factories(settings)
        self._manager_factory = manager_factory or (
            lambda: MultiMCPManager(self._server_factories)
        )
        self.registry = InvestigationRegistry()
        self.store: InvestigationStore | None = (
            store if store is not None else InvestigationStore()
        )
        self._tasks: set[asyncio.Task] = set()

    async def startup(self) -> None:
        """Initialize the state schema and conservatively resolve anything a
        restart left half-finished (running -> interrupted, running tool
        calls -> unknown). Never blocks app start on a down database."""
        if self.store is None:
            return
        try:
            version = await self.store.ensure_schema()
            interrupted = await self.store.mark_interrupted()
            log.info("state_store_ready", schema_version=version, interrupted=interrupted)
        except Exception as exc:
            log.warning("state_store_unavailable", error=str(exc)[:200])
            self.store = None  # degrade to in-memory operation

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
