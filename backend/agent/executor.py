"""VALIDATE + EXECUTE: schema-check the selection, then run it through MCP.

Validation uses each tool's own JSON schema from MCP discovery (never a local
hardcode); selections address tools by qualified name (`server__tool`) and
execution goes through the MultiMCPManager, which routes to the owning
server's session. Invalid arguments and unknown tools are typed failures for
the caller (the loop owns the one repair attempt); execution failures are the
typed MCPClientError taxonomy and are never retried blindly.
"""

from __future__ import annotations

import jsonschema
import structlog
import time

from backend.agent.catalog import ToolCatalog
from backend.agent.evidence import EvidenceStore
from backend.agent.models import ToolCallRequest
from backend.mcp.client_manager import MultiMCPManager
from backend.mcp.errors import MCPClientError

log = structlog.get_logger(__name__)


class ValidationOutcome:
    def __init__(self, ok: bool, error: str | None = None):
        self.ok = ok
        self.error = error


class ExecutionOutcome:
    def __init__(
        self,
        *,
        ok: bool,
        evidence_id: str | None = None,
        payload: dict | None = None,
        error_class: str | None = None,
        error: str | None = None,
        duration_ms: float = 0.0,
    ):
        self.ok = ok
        self.evidence_id = evidence_id
        self.payload = payload
        self.error_class = error_class
        self.error = error
        self.duration_ms = duration_ms


class Executor:
    def __init__(self, managers: MultiMCPManager, catalog: ToolCatalog, store: EvidenceStore):
        self.managers = managers
        self.catalog = catalog
        self.store = store

    def validate(self, request: ToolCallRequest) -> ValidationOutcome:
        entry = self.catalog.resolve(request.name)
        if entry is None:
            return ValidationOutcome(False, f"unknown tool {request.name!r} (not offered by any configured server)")
        if not entry.allowlisted:
            return ValidationOutcome(
                False,
                f"tool {request.name!r} on {entry.server} is outside the policy allowlist "
                f"({entry.policy}) and may not be executed",
            )
        arguments = request.arguments
        if "_raw" in arguments:
            return ValidationOutcome(False, f"arguments are not valid JSON: {arguments.get('_raw', '')[:200]}")
        try:
            jsonschema.validate(instance=arguments, schema=entry.parameters)
        except jsonschema.ValidationError as exc:
            return ValidationOutcome(False, f"arguments do not match the tool schema: {exc.message}")
        return ValidationOutcome(True)

    async def execute(self, request: ToolCallRequest, step: int) -> ExecutionOutcome:
        entry = self.catalog.resolve(request.name)
        assert entry is not None  # validate() must have passed
        started = time.perf_counter()
        try:
            result = await self.managers.call_tool(entry.server, entry.name, request.arguments)
        except MCPClientError as exc:
            duration = (time.perf_counter() - started) * 1000
            log.warning(
                "agent_tool_failed",
                server=entry.server,
                tool=entry.name,
                step=step,
                error_class=type(exc).__name__,
                duration_ms=round(duration, 1),
            )
            return ExecutionOutcome(
                ok=False,
                error_class=type(exc).__name__,
                error=str(exc)[:500],
                duration_ms=duration,
            )
        duration = (time.perf_counter() - started) * 1000
        payload = _payload_of(result)
        evidence = self.store.add(
            tool_name=entry.name,
            server=entry.server,
            step=step,
            arguments=request.arguments,
            payload=payload,
        )
        log.info(
            "agent_tool_executed",
            server=entry.server,
            tool=entry.name,
            step=step,
            evidence_id=evidence.evidence_id,
            duration_ms=round(duration, 1),
        )
        return ExecutionOutcome(ok=True, evidence_id=evidence.evidence_id, payload=payload, duration_ms=duration)


def _payload_of(result) -> dict:
    """Best-effort extraction of the structured dict from an MCP CallToolResult."""
    structured = getattr(result, "structured_content", None)
    if isinstance(structured, dict):
        return structured
    text = getattr(result, "content", None)
    if text:
        import json

        try:
            parsed = json.loads("".join(getattr(block, "text", "") for block in text))
            if isinstance(parsed, dict):
                return parsed
        except (json.JSONDecodeError, TypeError):
            pass
    return {"raw": str(result)[:400]}
