"""Agent-side discovery: catalogs come from live MCP discovery."""

import pytest

from backend.agent.catalog import SERVER_ALLOWLISTS, ToolCatalog
from backend.mcp.client_manager import MultiMCPManager


async def test_catalog_matches_discovered_postgres_tools(pg_manager):
    async with MultiMCPManager({"postgres-mcp": lambda: pg_manager()}) as managers:
        catalog = ToolCatalog.from_managers(managers.managers, SERVER_ALLOWLISTS)
    assert set(catalog.names()) == {f"postgres-mcp__{name}" for name in SERVER_ALLOWLISTS["postgres-mcp"]}
    assert catalog.unexpected() == []
    llm_tools = catalog.to_llm_tools()
    assert len(llm_tools) == 5
    descriptions = {tool["function"]["name"]: tool["function"]["description"] for tool in llm_tools}
    assert all(descriptions.values()), "tool descriptions must come from the MCP server"
    entry = catalog.resolve("postgres-mcp__get_daily_sales")
    assert entry is not None and entry.server == "postgres-mcp" and entry.policy == "read_only"
