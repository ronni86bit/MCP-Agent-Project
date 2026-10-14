"""Tool catalog: the MCP-discovered, multi-server tool set offered to the model.

The catalog is built from live MCP discovery — tool definitions are never
hardcoded into the agent's reasoning. Server allowlists are POLICY: discovered
tools outside them are logged and excluded from both the LLM tool definitions
and execution, never silently used. LLM-facing tool names are qualified
(`server__tool`) so servers cannot collide at the model interface; a duplicate
base tool name across servers is still a configuration error and surfaces as
ToolCatalogCollisionError.
"""

from __future__ import annotations

import structlog
from pydantic import BaseModel, Field
from typing import Literal

from backend.mcp.client_manager import MCPClientManager

log = structlog.get_logger(__name__)


class ToolCatalogCollisionError(Exception):
    """Two configured MCP servers expose the same base tool name."""

    def __init__(self, name: str, servers: list[str]):
        super().__init__(
            f"tool name collision: {name!r} is offered by multiple servers: {sorted(servers)}. "
            "Each tool must be uniquely owned by one server."
        )
        self.name = name
        self.servers = servers


# Policy allowlists per server (this phase: read-only investigation tools).
POSTGRES_EXPECTED_TOOLS = frozenset(
    {
        "get_daily_sales",
        "compare_sales",
        "get_product_sales",
        "get_region_sales",
        "get_sales_trend",
    }
)
MONITORING_EXPECTED_TOOLS = frozenset(
    {
        "get_service_metrics",
        "get_error_rate",
        "get_incidents",
        "get_recent_alerts",
        "compare_metric_windows",
    }
)
GITHUB_EXPECTED_TOOLS = frozenset(
    {
        "get_recent_commits",
        "get_deployments",
        "get_open_issues",
        "get_repository_activity",
    }
)
SERVER_ALLOWLISTS: dict[str, frozenset[str]] = {
    "postgres-mcp": POSTGRES_EXPECTED_TOOLS,
    "monitoring-mcp": MONITORING_EXPECTED_TOOLS,
    "github-mcp": GITHUB_EXPECTED_TOOLS,
}
SERVER_DOMAINS: dict[str, str] = {
    "postgres-mcp": "business",
    "monitoring-mcp": "operational",
    "github-mcp": "engineering",
}


class ToolCatalogEntry(BaseModel):
    server: str
    name: str
    qualified_name: str
    description: str = ""
    parameters: dict = Field(default_factory=dict)
    allowlisted: bool = True
    policy: Literal["read_only", "write"] = "read_only"


class ToolCatalog(BaseModel):
    entries: list[ToolCatalogEntry] = Field(default_factory=list)

    @classmethod
    def from_managers(
        cls,
        managers: dict[str, MCPClientManager],
        allowlists: dict[str, frozenset[str]],
    ) -> "ToolCatalog":
        entries: list[ToolCatalogEntry] = []
        owner: dict[str, str] = {}
        for server, manager in managers.items():
            expected = allowlists.get(server, frozenset())
            for tool in manager.tools():
                if tool.name in owner and owner[tool.name] != server:
                    raise ToolCatalogCollisionError(tool.name, [owner[tool.name], server])
                owner[tool.name] = server
                entries.append(
                    ToolCatalogEntry(
                        server=server,
                        name=tool.name,
                        qualified_name=f"{server}__{tool.name}",
                        description=tool.description,
                        parameters=tool.input_schema,
                        allowlisted=tool.name in expected,
                    )
                )
            unexpected = [e.name for e in entries if e.server == server and not e.allowlisted]
            if unexpected:
                log.warning(
                    "unexpected_tools_discovered",
                    server=server,
                    unexpected=unexpected,
                    action="excluded_from_llm_and_execution",
                )
            missing = sorted(expected - {e.name for e in entries if e.server == server})
            if missing:
                log.warning("expected_tools_missing", server=server, missing=missing)
        return cls(entries=entries)

    def resolve(self, qualified_name: str) -> ToolCatalogEntry | None:
        return next((e for e in self.entries if e.qualified_name == qualified_name), None)

    def allowlisted_entries(self) -> list[ToolCatalogEntry]:
        return [e for e in self.entries if e.allowlisted]

    def unexpected(self) -> list[str]:
        return [f"{e.server}.{e.name}" for e in self.entries if not e.allowlisted]

    def is_executable(self, qualified_name: str) -> bool:
        entry = self.resolve(qualified_name)
        return entry is not None and entry.allowlisted

    def to_llm_tools(self) -> list[dict]:
        """OpenAI-compatible tool definitions with qualified names.

        Parameter schemas are slimmed for the local model's tight context
        (per-field descriptions and schema noise dropped; types, enums,
        required lists kept; Optional collapsed to its base type). Argument
        validation always runs against the FULL discovered schema. Top-level
        descriptions stay intact — tool-choice quality depends on them.
        """
        return [
            {
                "type": "function",
                "function": {
                    "name": entry.qualified_name,
                    "description": (entry.description or entry.name)[:320],
                    "parameters": _slim_parameters(entry.parameters),
                },
            }
            for entry in self.allowlisted_entries()
        ]

    def names(self) -> list[str]:
        return [entry.qualified_name for entry in self.allowlisted_entries()]


_KEEP_KEYS = ("type", "enum", "items", "minimum", "maximum")


def _slim_parameters(schema: dict, depth: int = 0) -> dict:
    """Compact copy of a JSON schema: structure without descriptions."""
    if depth > 3 or not isinstance(schema, dict):
        return {"type": "object"}
    slim: dict = {}
    options = schema.get("anyOf")
    if isinstance(options, list):
        non_null = next((o for o in options if isinstance(o, dict) and o.get("type") != "null"), None)
        if non_null is not None:
            slim.update(_slim_parameters(non_null, depth + 1))
            return slim
    if "type" in schema:
        slim["type"] = schema["type"]
    if isinstance(schema.get("properties"), dict):
        slim["properties"] = {
            name: _slim_property(spec, depth)
            for name, spec in schema["properties"].items()
            if isinstance(spec, dict)
        }
    if "required" in schema:
        slim["required"] = list(schema["required"])
    return slim


def _slim_property(spec: dict, depth: int) -> dict:
    prop = {key: spec[key] for key in _KEEP_KEYS if key in spec}
    # short field hints: the local model guesses argument usage without them,
    # and schema-invalid arguments cost a whole repair attempt
    if "description" in spec:
        prop["description"] = spec["description"][:48]
    if isinstance(prop.get("items"), dict):
        prop["items"] = _slim_parameters(prop["items"], depth + 1)
    return prop
