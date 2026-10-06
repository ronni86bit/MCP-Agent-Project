"""ToolCatalog: built from discovery, filtered by the policy allowlist."""

from backend.agent.catalog import POSTGRES_EXPECTED_TOOLS, ToolCatalog
from backend.mcp.client_manager import ToolInfo


class FakeManager:
    name = "postgres-mcp"

    def __init__(self, tools: list[ToolInfo]):
        self._tools = tools

    def tools(self) -> list[ToolInfo]:
        return self._tools


def _tool(name: str) -> ToolInfo:
    return ToolInfo(
        name=name,
        description=f"{name} description",
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
    )


def test_catalog_offers_exactly_the_expected_postgres_tools():
    tools = [_tool(name) for name in sorted(POSTGRES_EXPECTED_TOOLS)]
    catalog = ToolCatalog.from_manager(FakeManager(tools), POSTGRES_EXPECTED_TOOLS)
    assert set(catalog.names()) == POSTGRES_EXPECTED_TOOLS
    assert catalog.unexpected() == []
    llm_tools = catalog.to_llm_tools()
    assert len(llm_tools) == 5
    assert all(tool["type"] == "function" for tool in llm_tools)
    assert {tool["function"]["name"] for tool in llm_tools} == POSTGRES_EXPECTED_TOOLS


def test_unexpected_tools_are_excluded_and_not_executable():
    tools = [_tool(name) for name in sorted(POSTGRES_EXPECTED_TOOLS)] + [_tool("drop_tables")]
    catalog = ToolCatalog.from_manager(FakeManager(tools), POSTGRES_EXPECTED_TOOLS)
    assert catalog.unexpected() == ["drop_tables"]
    assert "drop_tables" not in {tool["function"]["name"] for tool in catalog.to_llm_tools()}
    assert not catalog.is_executable("drop_tables")
    assert catalog.is_executable("get_daily_sales")


def test_missing_expected_tools_are_reported():
    catalog = ToolCatalog.from_manager(FakeManager([_tool("get_daily_sales")]), POSTGRES_EXPECTED_TOOLS)
    missing = POSTGRES_EXPECTED_TOOLS - set(catalog.names())
    assert "compare_sales" in missing
    assert len(catalog.allowlisted_entries()) == 1
