"""Multi-server catalog: merging, ownership, collision detection."""

import pytest

from backend.agent.catalog import (
    SERVER_ALLOWLISTS,
    SERVER_DOMAINS,
    ToolCatalog,
    ToolCatalogCollisionError,
)
from backend.mcp.client_manager import ToolInfo
from backend.mcp.errors import TransportError


class FakeManager:
    def __init__(self, name: str, tools: list[str]):
        self.name = name
        self._tools = [
            ToolInfo(name=t, description=f"{t} desc", input_schema={"type": "object"}) for t in tools
        ]

    def tools(self):
        return self._tools


def test_fourteen_tools_across_three_servers():
    total = sum(len(tools) for tools in SERVER_ALLOWLISTS.values())
    assert total == 14
    assert set(SERVER_ALLOWLISTS) == {"postgres-mcp", "monitoring-mcp", "github-mcp"}
    assert set(SERVER_DOMAINS) == set(SERVER_ALLOWLISTS)


def test_merge_preserves_server_ownership_and_qualifies_names():
    managers = {
        "postgres-mcp": FakeManager("postgres-mcp", ["get_daily_sales"]),
        "monitoring-mcp": FakeManager("monitoring-mcp", ["get_error_rate"]),
    }
    catalog = ToolCatalog.from_managers(managers, SERVER_ALLOWLISTS)
    entry = catalog.resolve("monitoring-mcp__get_error_rate")
    assert entry is not None
    assert entry.server == "monitoring-mcp" and entry.name == "get_error_rate"
    assert entry.policy == "read_only"
    assert catalog.names() == [
        "postgres-mcp__get_daily_sales",
        "monitoring-mcp__get_error_rate",
    ]


def test_duplicate_tool_name_across_servers_raises_typed_collision():
    managers = {
        "postgres-mcp": FakeManager("postgres-mcp", ["get_daily_sales", "ping"]),
        "monitoring-mcp": FakeManager("monitoring-mcp", ["ping"]),
    }
    with pytest.raises(ToolCatalogCollisionError) as excinfo:
        ToolCatalog.from_managers(managers, SERVER_ALLOWLISTS)
    assert "ping" in str(excinfo.value)
    assert {"postgres-mcp", "monitoring-mcp"} <= set(excinfo.value.servers)


def test_unexpected_tools_are_flagged_not_merged_into_llm_defs():
    managers = {"postgres-mcp": FakeManager("postgres-mcp", ["get_daily_sales", "drop_tables"])}
    catalog = ToolCatalog.from_managers(managers, SERVER_ALLOWLISTS)
    assert catalog.unexpected() == ["postgres-mcp.drop_tables"]
    assert "postgres-mcp__drop_tables" not in catalog.names()
    assert not catalog.is_executable("postgres-mcp__drop_tables")


def test_multi_manager_rejects_unknown_server():
    from backend.mcp.client_manager import MultiMCPManager

    managers = MultiMCPManager({})
    try:
        managers.manager("nope")
    except TransportError as exc:
        assert "unknown MCP server" in str(exc)
    else:
        raise AssertionError("expected TransportError")
