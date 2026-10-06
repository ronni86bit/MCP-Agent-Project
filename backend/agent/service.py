"""AgentService: wires the LLM adapter, MCP manager factory, loop, and registry.

The API layer talks only to this service; it never touches the loop internals
or any provider SDK.
"""

from __future__ import annotations

import asyncio
import sys

import structlog

from backend.agent.catalog import POSTGRES_EXPECTED_TOOLS
from backend.agent.llm.adapter import LLMClient, LLMError, OpenAICompatLLM
from backend.agent.loop import InvestigationLoop
from backend.agent.state import InvestigationRecord, InvestigationRegistry
from backend.config import Settings

log = structlog.get_logger(__name__)


def postgres_manager_factory(settings: Settings):
    """Fresh MCP session per investigation: a timed-out call poisons a stdio
    session, so isolation beats sharing."""

    def factory() -> MCPClientManager:
        from backend.mcp.client_manager import MCPClientManager

        return MCPClientManager(
            "postgres-mcp",
            sys.executable,
            ["-m", "backend.mcp.servers.postgres_mcp"],
            call_timeout_seconds=settings.mcp_call_timeout_seconds,
        )

    return factory


class AgentService:
    def __init__(
        self,
        settings: Settings,
        *,
        llm: LLMClient | None = None,
        manager_factory=None,
        allowlist: frozenset[str] = POSTGRES_EXPECTED_TOOLS,
    ):
        self.settings = settings
        self.llm = llm or OpenAICompatLLM(
            base_url=f"{settings.ollama_host}/v1",
            model=settings.ollama_model,
            timeout_seconds=settings.llm_timeout_seconds,
            temperature=settings.llm_temperature,
        )
        self._manager_factory = manager_factory or postgres_manager_factory(settings)
        self.allowlist = allowlist
        self.registry = InvestigationRegistry()
        self._tasks: set[asyncio.Task] = set()

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
        try:
            async with asyncio.timeout(15):
                async with self._manager_factory() as manager:
                    tools = [tool.name for tool in manager.tools()]
            postgres = f"up ({len(tools)} tools)"
        except Exception as exc:
            postgres = f"down: {type(exc).__name__}: {exc}"
        return {"status": "ok", "model": self.llm.model_name, "ollama": ollama, "postgres_mcp": postgres}

    async def shutdown(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
