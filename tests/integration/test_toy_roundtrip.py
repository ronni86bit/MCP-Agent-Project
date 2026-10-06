"""Phase 1 round-trip tests: in-process and subprocess transports, plus failure paths."""

import sys
from contextlib import asynccontextmanager

import anyio
import pytest
from mcp import ClientSession
from mcp.server.mcpserver import MCPServer
from mcp.shared.memory import create_client_server_memory_streams

from backend.mcp.client_manager import MCPClientManager, result_text
from backend.mcp.errors import (
    InvalidArgumentsError,
    MCPClientError,
    ToolExecutionError,
    ToolNotFoundError,
)
from backend.mcp.servers import toy_server

EXPECTED_TOOLS = {"echo", "add", "get_current_time", "sleep"}


@pytest.fixture
def manager():
    return MCPClientManager(
        "toy",
        sys.executable,
        ["-m", "backend.mcp.servers.toy_server"],
        call_timeout_seconds=10.0,
    )


async def test_subprocess_discovery_and_invocation(manager: MCPClientManager):
    async with manager as mcp:
        assert EXPECTED_TOOLS <= {tool.name for tool in mcp.tools()}

        echoed = await mcp.call_tool("echo", {"text": "ping"})
        assert result_text(echoed) == "ping"

        total = await mcp.call_tool("add", {"a": 2, "b": 3})
        assert float(result_text(total)) == 5.0

        now = await mcp.call_tool("get_current_time")
        assert "T" in result_text(now)  # ISO-8601

    assert mcp.records, "successful calls must be recorded"
    assert all(record.status == "ok" for record in mcp.records)
    assert all(record.duration_ms >= 0 for record in mcp.records)


async def test_unknown_tool_is_typed_error(manager: MCPClientManager):
    async with manager as mcp:
        with pytest.raises(ToolNotFoundError):
            await mcp.call_tool("no_such_tool", {})
        # session remains usable after a typed failure
        assert result_text(await mcp.call_tool("echo", {"text": "still alive"})) == "still alive"


async def test_invalid_arguments_is_typed_error(manager: MCPClientManager):
    async with manager as mcp:
        with pytest.raises((InvalidArgumentsError, ToolExecutionError)):
            await mcp.call_tool("add", {"a": "not-a-number", "b": 2})


async def test_timeout_is_typed_error():
    slow = MCPClientManager(
        "toy",
        sys.executable,
        ["-m", "backend.mcp.servers.toy_server"],
        call_timeout_seconds=0.05,  # server sleeps 1s -> read timeout fires deterministically
    )
    from backend.mcp.errors import ToolTimeoutError

    async with slow:
        with pytest.raises(ToolTimeoutError):
            await slow.call_tool("sleep", {"seconds": 1})


@asynccontextmanager
async def connected_session(server: MCPServer):
    """In-process client session over memory streams (mcp 2.x has no stock helper)."""
    async with create_client_server_memory_streams() as (client_ms, server_ms):
        async with anyio.create_task_group() as tg:
            async def run_server() -> None:
                # _lowlevel_server is SDK-private; revisit if it moves (Phase 1 test aid only)
                await server._lowlevel_server.run(
                    server_ms[0],
                    server_ms[1],
                    server._lowlevel_server.create_initialization_options(),
                    raise_exceptions=True,
                )

            tg.start_soon(run_server)
            try:
                async with ClientSession(client_ms[0], client_ms[1]) as session:
                    await session.initialize()
                    yield session
            finally:
                tg.cancel_scope.cancel()


async def test_in_process_roundtrip():
    async with connected_session(toy_server.mcp) as session:
        listed = await session.list_tools()
        assert EXPECTED_TOOLS <= {tool.name for tool in listed.tools}
        result = await session.call_tool("add", {"a": 1, "b": 2})
        assert float(result_text(result)) == 3.0


def test_error_hierarchy():
    assert issubclass(ToolNotFoundError, MCPClientError)
    assert issubclass(InvalidArgumentsError, MCPClientError)
    assert issubclass(ToolExecutionError, MCPClientError)
