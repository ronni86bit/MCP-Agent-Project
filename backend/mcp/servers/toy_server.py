"""Phase 1 practice MCP server.

Purpose: understand tools, JSON schemas, discovery, and invocation over stdio
before building the real postgres/github/monitoring servers. Run standalone:

    uv run python -m backend.mcp.servers.toy_server
"""

from datetime import datetime, timezone
import time

from mcp.server.mcpserver import MCPServer

mcp = MCPServer(
    "toy",
    instructions="Practice server for Phase 1: verifies the client round-trip (discovery, invocation, error paths).",
)


@mcp.tool()
def echo(text: str) -> str:
    """Return the input text unchanged. Use to verify client-server round-trips."""
    return text


@mcp.tool()
def get_current_time() -> str:
    """Current UTC time as an ISO-8601 string."""
    return datetime.now(timezone.utc).isoformat()


@mcp.tool()
def add(a: float, b: float) -> float:
    """Add two numbers and return the sum."""
    return a + b


@mcp.tool()
def sleep(seconds: float) -> str:
    """Sleep for the given number of seconds, then confirm. Use to exercise client timeouts."""
    time.sleep(seconds)
    return f"slept {seconds}s"


if __name__ == "__main__":
    mcp.run()  # stdio transport; stdout is protocol-reserved
