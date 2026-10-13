"""MCP client wrapper: one session per server, discovery, and invocation.

Phase 1 scope: stdio subprocess transport, discovery cache, per-call timeout,
typed error mapping, and call records with duration/status. The tool catalog
and policy engine merge into this layer in later phases.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable
from contextlib import AsyncExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import anyio
import structlog
from mcp import ClientSession, MCPError, StdioServerParameters
from mcp.client.stdio import stdio_client

from backend.mcp.errors import (
    InvalidArgumentsError,
    MCPClientError,
    ToolExecutionError,
    ToolNotFoundError,
    ToolTimeoutError,
    TransportError,
)

REPO_ROOT = Path(__file__).resolve().parents[2]

log = structlog.get_logger(__name__)

# JSON-RPC error codes the SDK maps request failures onto (mcp 2.x raises
# MCPError from call_tool instead of returning isError results).
_CODE_REQUEST_TIMEOUT = -32001
_CODE_INVALID_PARAMS = -32602
_CODE_NOT_FOUND = -32601


@dataclass(frozen=True)
class ToolInfo:
    name: str
    description: str
    input_schema: dict[str, Any]


@dataclass(frozen=True)
class ToolCallRecord:
    tool: str
    duration_ms: float
    status: str  # "ok" | "error"
    error_class: str | None = None


def result_text(result: Any) -> str:
    """Concatenate the text content of a CallToolResult."""
    parts = []
    for block in getattr(result, "content", None) or []:
        text = getattr(block, "text", None)
        if text is not None:
            parts.append(text)
    return "".join(parts)


def _short_args(args: dict[str, Any]) -> str:
    try:
        text = json.dumps(args, default=str)
    except (TypeError, ValueError):
        text = repr(args)
    return text[:200]


def _map_mcp_error(exc: MCPError, tool: str, duration_ms: float) -> MCPClientError:
    """Classify a protocol-level MCPError into the local failure taxonomy."""
    code = getattr(exc, "code", None)
    if code == _CODE_REQUEST_TIMEOUT:
        return ToolTimeoutError(
            f"tool {tool!r} exceeded the call timeout: {exc}", tool=tool, duration_ms=duration_ms
        )
    if code == _CODE_NOT_FOUND:
        return ToolNotFoundError(
            f"tool {tool!r} is unknown to server: {exc}", tool=tool, duration_ms=duration_ms
        )
    if code == _CODE_INVALID_PARAMS:
        return InvalidArgumentsError(
            f"tool {tool!r} rejected the arguments: {exc}", tool=tool, duration_ms=duration_ms
        )
    return ToolExecutionError(
        f"tool {tool!r} failed server-side: {exc}", tool=tool, duration_ms=duration_ms
    )


class MCPClientManager:
    """Owns one MCP server session (stdio subprocess by default).

    Use as an async context manager; on exit the server process is terminated.
    A call that times out poisons the session — discard the manager and start
    a fresh one (enforced loop-level in later phases).
    """

    def __init__(
        self,
        name: str,
        command: str,
        args: list[str] | None = None,
        *,
        env: dict[str, str] | None = None,
        cwd: str | Path | None = None,
        call_timeout_seconds: float = 20.0,
        session_timeout_seconds: float = 30.0,
    ):
        self.name = name
        self.call_timeout_seconds = call_timeout_seconds
        self.session_timeout_seconds = session_timeout_seconds
        # caller env entries overlay the parent environment (additive), so tests
        # can inject e.g. GITHUB_API_BASE_URL without dropping PATH
        resolved_env = {**os.environ, **(env or {})}
        # PYTHONPATH so `-m backend.mcp.servers.<name>` resolves regardless of caller cwd.
        resolved_env.setdefault("PYTHONPATH", str(REPO_ROOT))
        resolved_env.setdefault("PYTHONUTF8", "1")
        self.server_params = StdioServerParameters(
            command=command,
            args=list(args or []),
            env=resolved_env,
            cwd=str(cwd) if cwd is not None else None,
        )
        self._stack: AsyncExitStack | None = None
        self._session: ClientSession | None = None
        self._tools: dict[str, ToolInfo] = {}
        self.records: list[ToolCallRecord] = []

    async def __aenter__(self) -> MCPClientManager:
        await self.start()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.stop()

    async def start(self) -> None:
        if self._session is not None:
            return
        stack = AsyncExitStack()
        try:
            read, write = await stack.enter_async_context(stdio_client(self.server_params))
            session = await stack.enter_async_context(
                ClientSession(read, write, read_timeout_seconds=self.session_timeout_seconds)
            )
            await session.initialize()
        except BaseException as exc:
            await stack.aclose()
            raise TransportError(f"failed to start MCP server {self.name!r}: {exc}") from exc
        self._stack, self._session = stack, session
        await self.refresh_tools()
        log.info("mcp_session_started", server=self.name, tools=sorted(self._tools))

    async def stop(self) -> None:
        if self._stack is None:
            return
        stack, self._stack, self._session = self._stack, None, None
        await stack.aclose()
        log.info("mcp_session_stopped", server=self.name)

    async def refresh_tools(self) -> list[ToolInfo]:
        self._require_session()
        result = await self._session.list_tools()
        self._tools = {
            tool.name: ToolInfo(tool.name, tool.description or "", tool.input_schema)
            for tool in result.tools
        }
        return list(self._tools.values())

    def tools(self) -> list[ToolInfo]:
        return list(self._tools.values())

    async def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> Any:
        """Invoke a discovered tool; returns the raw CallToolResult on success."""
        started = time.perf_counter()
        self._require_session()
        if name not in self._tools:
            raise ToolNotFoundError(
                f"tool {name!r} is not offered by server {self.name!r}; "
                "refresh_tools() if the catalog may be stale",
                tool=name,
            )
        try:
            # Per-call deadline, enforced by the session: expiry raises
            # MCPError(-32001) (external cancel scopes are shielded by the
            # session's request loop, so this is the reliable mechanism).
            result = await self._session.call_tool(
                name, arguments or {}, read_timeout_seconds=self.call_timeout_seconds
            )
        except MCPError as exc:
            return self._fail(name, started, _map_mcp_error(exc, name, self._elapsed_ms(started)), exc)
        except Exception as exc:
            return self._fail(name, started, TransportError(
                f"transport failure calling {name!r} on {self.name!r}: {exc}",
                tool=name,
                duration_ms=self._elapsed_ms(started),
            ), exc)

        duration_ms = self._elapsed_ms(started)
        if result.is_error:
            return self._fail(
                name,
                started,
                ToolExecutionError(
                    result_text(result) or f"tool {name!r} reported an error",
                    tool=name,
                    duration_ms=duration_ms,
                ),
            )

        self.records.append(ToolCallRecord(tool=name, duration_ms=duration_ms, status="ok"))
        log.info(
            "tool_call",
            server=self.name,
            tool=name,
            duration_ms=round(duration_ms, 1),
            args=_short_args(arguments or {}),
            status="ok",
        )
        return result

    # -- internals ---------------------------------------------------------

    def _require_session(self) -> None:
        if self._session is None:
            raise TransportError(f"session for server {self.name!r} is not started")

    @staticmethod
    def _elapsed_ms(started: float) -> float:
        return round((time.perf_counter() - started) * 1000, 1)

    def _fail(
        self, tool: str, started: float, error: MCPClientError, cause: BaseException | None = None
    ) -> None:
        duration_ms = self._elapsed_ms(started)
        self.records.append(
            ToolCallRecord(
                tool=tool, duration_ms=duration_ms, status="error", error_class=type(error).__name__
            )
        )
        log.error(
            "tool_call",
            server=self.name,
            tool=tool,
            duration_ms=duration_ms,
            status="error",
            error_class=type(error).__name__,
            error=str(error),
        )
        raise error from cause


class MultiMCPManager:
    """Owns one session per configured MCP server (independent processes).

    Servers never see each other; orchestration stays in the agent runtime.
    Tool addressing is (server, tool_name) — the catalog builds qualified
    names (`server__tool`) for the LLM on top of this.
    """

    def __init__(self, factories: dict[str, Callable[[], MCPClientManager]]):
        self.factories = factories
        self.managers: dict[str, MCPClientManager] = {}
        self._stack: AsyncExitStack | None = None

    async def __aenter__(self) -> "MultiMCPManager":
        stack = AsyncExitStack()
        try:
            for server, factory in self.factories.items():
                self.managers[server] = await stack.enter_async_context(factory())
        except BaseException:
            await stack.aclose()
            self.managers.clear()
            raise
        self._stack = stack
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        stack, self._stack = self._stack, None
        self.managers.clear()
        if stack is not None:
            await stack.aclose()

    def manager(self, server: str) -> MCPClientManager:
        try:
            return self.managers[server]
        except KeyError:
            raise TransportError(
                f"unknown MCP server {server!r}; configured: {sorted(self.managers)}"
            ) from None

    async def call_tool(self, server: str, name: str, arguments: dict[str, Any] | None = None) -> Any:
        return await self.manager(server).call_tool(name, arguments)
