"""Typed failure taxonomy for MCP tool invocation.

The executor (later phases) maps these classes onto recovery policy:
retryable (timeout, transport), repairable (invalid arguments), or terminal
(not found, execution error) — failures are never retried blindly.
"""

from __future__ import annotations


class MCPClientError(Exception):
    """Base class for MCP client-side failures."""

    def __init__(self, message: str, *, tool: str | None = None, duration_ms: float | None = None):
        super().__init__(message)
        self.tool = tool
        self.duration_ms = duration_ms


class ToolNotFoundError(MCPClientError):
    """The requested tool is not in the discovered catalog (stale or wrong name)."""


class InvalidArgumentsError(MCPClientError):
    """Arguments failed validation against the tool's input schema."""


class ToolTimeoutError(MCPClientError):
    """The tool call exceeded the configured per-call timeout."""


class ToolExecutionError(MCPClientError):
    """The server executed the call but reported a tool-level error."""


class TransportError(MCPClientError):
    """The MCP session/transport failed (server crashed, pipe closed, protocol error)."""
