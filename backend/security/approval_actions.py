"""Registry of executable write actions and deterministic proposal helpers.

`WRITABLE_ACTIONS` is the complete set of write actions the approval service
may ever execute; adding one requires a code change. The LLM catalog never
contains these tools — proposals reference them by name and are validated
against stored evidence before anything is persisted.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any

from backend.agent.evidence import Evidence

TARGET_MONITORING = "monitoring-mcp"

WRITABLE_ACTIONS: dict[str, str] = {
    "acknowledge_alert": TARGET_MONITORING,
}


def eligible_alert_ids(evidence_records: list[Any]) -> list[str]:
    """Deterministically extract unacknowledged alert ids from stored evidence.

    Scans the alert rows embedded in evidence summaries (monitoring-mcp
    `get_recent_alerts` results). Subject to the Phase 5 evidence preview cap:
    alert ids outside the stored previews are not eligible in this phase.
    """
    eligible: list[str] = []
    for record in evidence_records:
        if record.server != TARGET_MONITORING:
            continue
        alerts = record.summary.get("alerts")
        if not isinstance(alerts, list):
            continue
        for alert in alerts:
            if not isinstance(alert, dict):
                continue
            alert_id = alert.get("id")
            if alert_id and alert.get("acknowledged") is False and alert_id not in eligible:
                eligible.append(alert_id)
    return eligible


def args_hash(
    *,
    action: str,
    target_server: str,
    arguments: dict[str, Any],
    proposal_version: int,
    expires_at: datetime,
) -> str:
    """Canonical SHA-256 binding of action, target, exact parameters, proposal
    version, and expiry. Recomputed at execution time; any alteration breaks
    the hash and the proposal is refused without a write."""
    payload = {
        "action": action,
        "target_server": target_server,
        "arguments": arguments,
        "proposal_version": proposal_version,
        "expires_at": expires_at.astimezone(expires_at.tzinfo or None).isoformat()
        if expires_at.tzinfo else expires_at.isoformat(),
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
