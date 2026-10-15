"""Multi-server discovery: all three MCP servers connect and merge into one
catalog of exactly 14 read-only tools with server ownership preserved."""

import sys

import pytest

from backend.agent.catalog import SERVER_ALLOWLISTS, SERVER_DOMAINS, ToolCatalog
from backend.mcp.client_manager import MCPClientManager, MultiMCPManager
from tests.support.github_mock import build_github_mock


def _server_factories(github_base_url: str) -> dict:
    return {
        "postgres-mcp": lambda: MCPClientManager(
            "postgres-mcp", sys.executable, ["-m", "backend.mcp.servers.postgres_mcp"],
            call_timeout_seconds=15,
        ),
        "monitoring-mcp": lambda: MCPClientManager(
            "monitoring-mcp", sys.executable, ["-m", "backend.mcp.servers.monitoring_mcp"],
            call_timeout_seconds=15,
        ),
        "github-mcp": lambda: MCPClientManager(
            "github-mcp", sys.executable, ["-m", "backend.mcp.servers.github_mcp"],
            env={"GITHUB_API_BASE_URL": github_base_url},
            call_timeout_seconds=15,
        ),
    }


@pytest.fixture(scope="module")
def github_mock():
    server = build_github_mock()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


async def test_all_three_servers_discover_exactly_fourteen_tools(github_mock):
    async with MultiMCPManager(_server_factories(github_mock)) as managers:
        catalog = ToolCatalog.from_managers(managers.managers, SERVER_ALLOWLISTS)
    assert len(catalog.allowlisted_entries()) == 14
    # the write tool appears in raw discovery but is excluded (and logged)
    assert catalog.unexpected() == ["monitoring-mcp.acknowledge_alert"]
    assert not catalog.is_executable("monitoring-mcp__acknowledge_alert")

    per_server = {server: 0 for server in SERVER_ALLOWLISTS}
    for entry in catalog.allowlisted_entries():
        per_server[entry.server] += 1
        assert entry.qualified_name == f"{entry.server}__{entry.name}"
        assert entry.policy == "read_only"
        assert entry.description, "descriptions must come from the servers"
    assert per_server == {"postgres-mcp": 5, "monitoring-mcp": 5, "github-mcp": 4}


async def test_llm_tool_definitions_carry_qualified_names(github_mock):
    async with MultiMCPManager(_server_factories(github_mock)) as managers:
        catalog = ToolCatalog.from_managers(managers.managers, SERVER_ALLOWLISTS)
        llm_tools = catalog.to_llm_tools()
        # each tool is invocable through its owning server
        entry = catalog.resolve("monitoring-mcp__get_error_rate")
        result = await managers.call_tool(
            entry.server, entry.name,
            {"service": "checkout", "window_start": "2026-10-03", "window_end": "2026-10-05"},
        )
    structured = getattr(result, "structured_content", None)
    assert structured is not None and structured["service"] == "checkout"
    assert {tool["function"]["name"] for tool in llm_tools} == set(catalog.names())


async def test_domains_cover_every_server(github_mock):
    assert set(SERVER_DOMAINS) == set(SERVER_ALLOWLISTS)
    assert SERVER_DOMAINS == {
        "postgres-mcp": "business",
        "monitoring-mcp": "operational",
        "github-mcp": "engineering",
    }
