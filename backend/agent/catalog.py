"""Tool catalog: the MCP-discovered tool set offered to the model.

The catalog is built from live MCP discovery — tool definitions are never
hardcoded into the agent's reasoning. The expected-tools set is a POLICY
allowlist: discovered tools outside it are logged and excluded from both the
LLM tool definitions and execution, never silently used.
"""

from __future__ import annotations

import structlog
from pydantic import BaseModel, Field

from backend.mcp.client_manager import MCPClientManager, ToolInfo

log = structlog.get_logger(__name__)

# Policy allowlist for the postgres-mcp server in this phase.
POSTGRES_EXPECTED_TOOLS = frozenset(
    {
        "get_daily_sales",
        "compare_sales",
        "get_product_sales",
        "get_region_sales",
        "get_sales_trend",
    }
)


class ToolCatalogEntry(BaseModel):
    name: str
    server: str
    description: str = ""
    parameters: dict = Field(default_factory=dict)
    allowlisted: bool = True


class ToolCatalog(BaseModel):
    server: str
    entries: list[ToolCatalogEntry] = Field(default_factory=list)

    @classmethod
    def from_manager(cls, manager: MCPClientManager, expected: frozenset[str]) -> "ToolCatalog":
        discovered: list[ToolInfo] = manager.tools()
        entries = [
            ToolCatalogEntry(
                name=tool.name,
                server=manager.name,
                description=tool.description,
                parameters=tool.input_schema,
                allowlisted=tool.name in expected,
            )
            for tool in discovered
        ]
        unexpected = [entry.name for entry in entries if not entry.allowlisted]
        if unexpected:
            log.warning(
                "unexpected_tools_discovered",
                server=manager.name,
                unexpected=unexpected,
                action="excluded_from_llm_and_execution",
            )
        missing = sorted(expected - {entry.name for entry in entries})
        if missing:
            log.warning("expected_tools_missing", server=manager.name, missing=missing)
        return cls(server=manager.name, entries=entries)

    def allowlisted_entries(self) -> list[ToolCatalogEntry]:
        return [entry for entry in self.entries if entry.allowlisted]

    def unexpected(self) -> list[str]:
        return [entry.name for entry in self.entries if not entry.allowlisted]

    def get(self, name: str) -> ToolCatalogEntry | None:
        return next((entry for entry in self.entries if entry.name == name), None)

    def is_executable(self, name: str) -> bool:
        entry = self.get(name)
        return entry is not None and entry.allowlisted

    def to_llm_tools(self) -> list[dict]:
        """OpenAI-compatible tool definitions for the chat API."""
        return [
            {
                "type": "function",
                "function": {
                    "name": entry.name,
                    "description": entry.description or entry.name,
                    "parameters": entry.parameters,
                },
            }
            for entry in self.allowlisted_entries()
        ]

    def names(self) -> list[str]:
        return [entry.name for entry in self.allowlisted_entries()]
