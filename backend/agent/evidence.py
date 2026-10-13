"""Evidence records: every successful MCP tool result becomes one.

The synthesizer may only cite evidence ids produced here; the verifier rejects
anything else. Storage is in-memory for Phase 3 — persistence belongs to the
Planning & State phase.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field

from backend.agent.catalog import SERVER_DOMAINS

_MAX_STR = 200
_PREVIEW_ROWS = 3


def sanitize(value: Any) -> Any:
    """Truncate long strings so arguments/results are safe to log and store."""
    if isinstance(value, str):
        return value if len(value) <= _MAX_STR else value[: _MAX_STR - 3] + "..."
    if isinstance(value, dict):
        return {str(k): sanitize(v) for k, v in list(value.items())[:20]}
    if isinstance(value, (list, tuple)):
        return [sanitize(v) for v in value[:20]]
    return value


def summarize_result(payload: Any) -> dict[str, Any]:
    """Compact, LLM-friendly digest of a tool result payload."""
    if not isinstance(payload, dict):
        return {"value": sanitize(payload)}
    summary = {k: sanitize(v) for k, v in payload.items() if k not in ("rows", "breakdown")}
    for key in ("rows", "breakdown"):
        if isinstance(payload.get(key), list):
            summary[key] = payload[key][:3]
            summary[f"{key}_total"] = len(payload[key])
    return summary


class Evidence(BaseModel):
    evidence_id: str
    investigation_id: str
    tool_name: str
    server: str
    step: int
    arguments: dict[str, Any] = Field(default_factory=dict)
    summary: dict[str, Any] = Field(default_factory=dict)
    at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class EvidenceStore:
    """Ordered, id-assigning in-memory evidence collection."""

    def __init__(self, investigation_id: str):
        self.investigation_id = investigation_id
        self._items: list[Evidence] = []

    def add(self, *, tool_name: str, server: str, step: int, arguments: dict, payload: Any) -> Evidence:
        record = Evidence(
            evidence_id=f"ev-{len(self._items) + 1:04d}",
            investigation_id=self.investigation_id,
            tool_name=tool_name,
            server=server,
            step=step,
            arguments=sanitize(arguments),
            summary=summarize_result(payload),
        )
        self._items.append(record)
        return record

    def get(self, evidence_id: str) -> Evidence | None:
        return next((item for item in self._items if item.evidence_id == evidence_id), None)

    def all(self) -> list[Evidence]:
        return list(self._items)

    def __len__(self) -> int:
        return len(self._items)

    def digest(self, max_chars: int = 4000) -> str:
        """Compact textual digest of all evidence, used for correlation/synthesis.

        Lines are prefixed with the evidence domain (business / operational /
        engineering) so the synthesizer can cite across systems deliberately.
        """
        if not self._items:
            return "(no evidence collected)"
        lines = []
        for item in self._items:
            domain = SERVER_DOMAINS.get(item.server, item.server)
            lines.append(f"[{item.evidence_id}] ({domain}) {item.server}.{item.tool_name} args={item.arguments}")
            lines.append(f"    -> {item.summary}")
        text = "\n".join(lines)
        return text if len(text) <= max_chars else text[: max_chars - 3] + "..."
