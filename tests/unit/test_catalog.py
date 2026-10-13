"""ToolCatalog (postgres server slice): built from discovery, filtered by the
policy allowlist. Multi-server merge/collision tests live in
test_catalog_multiserver.py."""

from backend.agent.catalog import SERVER_ALLOWLISTS, ToolCatalog
from backend.mcp.client_manager import ToolInfo


class FakeManager:
    def __init__(self, name: str, tools: list[str]):
        self.name = name
        self._tools = [
            ToolInfo(name=t, description=f"{t} description", input_schema={"type": "object"})
            for t in tools
        ]

    def tools(self):
        return self._tools


def _postgres_expected():
    return SERVER_ALLOWLISTS["postgres-mcp"]


def test_catalog_offers_exactly_the_expected_postgres_tools():
    tools = sorted(_postgres_expected())
    catalog = ToolCatalog.from_managers({"postgres-mcp": FakeManager("postgres-mcp", tools)}, SERVER_ALLOWLISTS)
    assert set(catalog.names()) == {f"postgres-mcp__{t}" for t in tools}
    assert catalog.unexpected() == []
    llm_tools = catalog.to_llm_tools()
    assert len(llm_tools) == 5
    assert all(tool["type"] == "function" for tool in llm_tools)
    assert {tool["function"]["name"] for tool in llm_tools} == {
        f"postgres-mcp__{t}" for t in tools
    }


def test_unexpected_tools_are_excluded_and_not_executable():
    tools = sorted(_postgres_expected()) + ["drop_tables"]
    catalog = ToolCatalog.from_managers({"postgres-mcp": FakeManager("postgres-mcp", tools)}, SERVER_ALLOWLISTS)
    assert catalog.unexpected() == ["postgres-mcp.drop_tables"]
    assert "postgres-mcp__drop_tables" not in {t["function"]["name"] for t in catalog.to_llm_tools()}
    assert not catalog.is_executable("postgres-mcp__drop_tables")
    assert catalog.is_executable("postgres-mcp__get_daily_sales")


def test_missing_expected_tools_are_reported():
    catalog = ToolCatalog.from_managers(
        {"postgres-mcp": FakeManager("postgres-mcp", ["get_daily_sales"])}, SERVER_ALLOWLISTS
    )
    missing = _postgres_expected() - {"get_daily_sales"}
    assert "compare_sales" in missing
    assert len(catalog.allowlisted_entries()) == 1
