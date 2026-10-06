"""Agent-side discovery: the catalog must come from live MCP discovery."""

from backend.agent.catalog import POSTGRES_EXPECTED_TOOLS, ToolCatalog


async def test_catalog_matches_discovered_postgres_tools(pg_manager):
    async with pg_manager() as manager:
        catalog = ToolCatalog.from_manager(manager, POSTGRES_EXPECTED_TOOLS)
    assert set(catalog.names()) == POSTGRES_EXPECTED_TOOLS
    assert catalog.unexpected() == []
    llm_tools = catalog.to_llm_tools()
    assert len(llm_tools) == 5
    descriptions = {tool["function"]["name"]: tool["function"]["description"] for tool in llm_tools}
    assert all(descriptions.values()), "tool descriptions must come from the MCP server"
