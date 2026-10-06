"""Phase 1 demo: discover tools on the toy MCP server and invoke them over stdio.

Run from the repo root:

    uv run python scripts/toy_client_demo.py
"""

import asyncio
import sys

import structlog

from backend.mcp.client_manager import MCPClientManager, result_text
from backend.mcp.errors import MCPClientError
from backend.observability.logging import configure_logging

SERVER_MODULE = "backend.mcp.servers.toy_server"

SAMPLE_CALLS: list[tuple[str, dict]] = [
    ("echo", {"text": "hello from the MCP client"}),
    ("add", {"a": 2, "b": 3}),
    ("get_current_time", {}),
]


def _print_section(title: str) -> None:
    print(f"\n=== {title} ===")


async def main() -> None:
    configure_logging()
    log = structlog.get_logger("toy_client_demo")

    _print_section(f"Starting MCP server: {SERVER_MODULE} (stdio)")
    async with MCPClientManager("toy", sys.executable, ["-m", SERVER_MODULE]) as mcp:
        _print_section("Discovered tools")
        for tool in mcp.tools():
            print(f"  {tool.name}: {tool.description}")

        _print_section("Invocations")
        for name, args in SAMPLE_CALLS:
            result = await mcp.call_tool(name, args)
            print(f"  {name}({args}) -> {result_text(result)!r}")

        _print_section("Error handling (typed failures, session stays usable)")
        try:
            await mcp.call_tool("no_such_tool", {})
        except MCPClientError as exc:
            print(f"  unknown tool -> {type(exc).__name__}: {exc}")
        try:
            await mcp.call_tool("add", {"a": "not-a-number", "b": 2})
        except MCPClientError as exc:
            print(f"  invalid args -> {type(exc).__name__}: {exc}")

        _print_section("Call records (observability)")
        for record in mcp.records:
            print(f"  {record}")

    _print_section("Session closed cleanly")
    log.info("demo_complete")


if __name__ == "__main__":
    asyncio.run(main())
