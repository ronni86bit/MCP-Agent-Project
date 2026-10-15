"""monitoring-mcp tool definitions (five approved read-only tools, nothing else).

Handlers validate inputs (models.py), read the deterministic fixture store
(store.py), and return structured dicts. The server reports observations only.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any

import structlog
from pydantic import ValidationError
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from backend.mcp.servers.monitoring_mcp import store as store_module
from backend.mcp.servers.monitoring_mcp.models import (
    AcknowledgeAlertInputs,
    AlertsInputs,
    CompareMetricsInputs,
    ErrorRateInputs,
    IncidentsInputs,
    ServiceMetricsInputs,
)

mcp = MCPServer(
    "monitoring-mcp",
    instructions=(
        "Read-only operational telemetry for the demo stack: hourly service metrics "
        "(error_rate percent, latency_p95_ms, requests_per_min), alerts, and incidents, "
        "from a fixed demo timeline. Use it to check whether services degraded and when. "
        "Observations only — this server does not explain causes. All tools are read-only."
    ),
)

log = structlog.get_logger(__name__)
_store = store_module.MonitoringStore.load()


def _inputs(model: type, **kwargs: Any) -> Any:
    try:
        return model(**kwargs)
    except ValidationError as exc:
        reasons = "; ".join(
            f"{'.'.join(str(part) for part in err['loc'])}: {err['msg']}" for err in exc.errors()
        )
        raise ToolError(f"invalid arguments: {reasons}") from None


def _require_alert(alert_id: str) -> None:
    if not _store.get_alert(alert_id):
        raise ToolError(f"unknown alert {alert_id!r}; alerts are referenced by id as returned "
                        f"by get_recent_alerts")


def _require_service(service: str) -> None:
    if not _store.is_service(service):
        raise ToolError(f"unknown service {service!r}; valid services: {_store.valid_services()}")


def _finish(tool: str, started: float, payload: dict[str, Any]) -> dict[str, Any]:
    log.info(
        "monitoring_tool_executed",
        tool=tool,
        count=payload.get("count", 0),
        duration_ms=round((time.perf_counter() - started) * 1000, 1),
    )
    return payload


@mcp.tool()
async def get_service_metrics(
    service: str,
    window_start: str,
    window_end: str,
    limit: int = 200,
) -> dict[str, Any]:
    """Hourly metric points for one service over a date window (max 14 days).

    Each point carries error_rate (percent), latency_p95_ms, and
    requests_per_min, in chronological order.

    Args:
        service: service name (e.g. "checkout", "payment", "catalog", "web")
        window_start: window start as ISO date (YYYY-MM-DD), inclusive
        window_end: window end as ISO date (YYYY-MM-DD), inclusive; at most 14 days after window_start
        limit: maximum number of hourly points to return (1-400, default 200)
    """
    inp: ServiceMetricsInputs = _inputs(
        ServiceMetricsInputs, service=service, window_start=window_start,
        window_end=window_end, limit=limit,
    )
    _require_service(inp.service)
    started = time.perf_counter()
    points = _store.metrics(inp.service, inp.start, inp.end, inp.limit)
    return _finish(
        "get_service_metrics", started,
        {
            "service": inp.service,
            "window": {"start": inp.window_start, "end": inp.window_end},
            "count": len(points),
            "points": points,
        },
    )


@mcp.tool()
async def get_error_rate(
    service: str,
    window_start: str,
    window_end: str,
) -> dict[str, Any]:
    """Daily error-rate rollup and summary for one service over a window (max 31 days).

    Returns per-day average/peak error rate (percent) plus window-level
    average and the peak timestamp — the quickest read on whether a service
    degraded and when it peaked.

    Args:
        service: service name (e.g. "checkout")
        window_start: window start as ISO date (YYYY-MM-DD), inclusive
        window_end: window end as ISO date (YYYY-MM-DD), inclusive; at most 31 days after window_start
    """
    inp: ErrorRateInputs = _inputs(
        ErrorRateInputs, service=service, window_start=window_start, window_end=window_end
    )
    _require_service(inp.service)
    started = time.perf_counter()
    daily = _store.daily_error_rate(inp.service, inp.start, inp.end)
    summary = {"window_avg_pct": 0.0, "peak_pct": 0.0, "peak_at": ""}
    if daily:
        peak_day = max(daily, key=lambda row: row["peak_pct"])
        summary = {
            "window_avg_pct": round(sum(row["avg_pct"] for row in daily) / len(daily), 3),
            "peak_pct": peak_day["peak_pct"],
            "peak_at": peak_day["peak_at"],
        }
    return _finish(
        "get_error_rate", started,
        {
            "service": inp.service,
            "window": {"start": inp.window_start, "end": inp.window_end},
            "summary": summary,
            "count": len(daily),
            "daily": daily,
        },
    )


@mcp.tool()
async def get_incidents(
    window_start: str,
    window_end: str,
    service: str | None = None,
) -> dict[str, Any]:
    """Incident records overlapping a date window (max 31 days).

    Args:
        window_start: window start as ISO date (YYYY-MM-DD), inclusive
        window_end: window end as ISO date (YYYY-MM-DD), inclusive; at most 31 days after window_start
        service: optional exact service name filter
    """
    inp: IncidentsInputs = _inputs(
        IncidentsInputs, window_start=window_start, window_end=window_end, service=service
    )
    if inp.service is not None:
        _require_service(inp.service)
    started = time.perf_counter()
    records = _store.incidents(inp.start, inp.end, inp.service)
    return _finish(
        "get_incidents", started,
        {
            "window": {"start": inp.window_start, "end": inp.window_end},
            "count": len(records),
            "incidents": records,
        },
    )


@mcp.tool()
async def get_recent_alerts(
    window_start: str,
    window_end: str,
    service: str | None = None,
    severity: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    """Alert records that fired within a date window (max 31 days).

    Args:
        window_start: window start as ISO date (YYYY-MM-DD), inclusive
        window_end: window end as ISO date (YYYY-MM-DD), inclusive; at most 31 days after window_start
        service: optional exact service name filter
        severity: optional severity filter: "info", "warning", "high", or "critical"
        limit: maximum number of alerts to return (1-100, default 50)
    """
    inp: AlertsInputs = _inputs(
        AlertsInputs, window_start=window_start, window_end=window_end,
        service=service, severity=severity, limit=limit,
    )
    if inp.service is not None:
        _require_service(inp.service)
    started = time.perf_counter()
    records = _store.alerts(inp.start, inp.end, inp.service, inp.severity)[: inp.limit]
    return _finish(
        "get_recent_alerts", started,
        {
            "window": {"start": inp.window_start, "end": inp.window_end},
            "count": len(records),
            "alerts": records,
        },
    )


@mcp.tool()
async def compare_metric_windows(
    service: str,
    metric: str,
    window_a_start: str,
    window_a_end: str,
    window_b_start: str,
    window_b_end: str,
) -> dict[str, Any]:
    """Compare one service metric between two EQUAL-LENGTH date windows.

    Both windows must span the same number of days: averages are only
    comparable between like-for-like periods, and unequal windows are
    rejected with a validation error instead of being normalized.
    Metric is one of "error_rate" (percent), "latency_p95_ms",
    "requests_per_min".

    Args:
        service: service name (e.g. "checkout")
        metric: "error_rate", "latency_p95_ms", or "requests_per_min"
        window_a_start: window A start, ISO date (YYYY-MM-DD), inclusive
        window_a_end: window A end, ISO date (YYYY-MM-DD), inclusive
        window_b_start: window B start, ISO date (YYYY-MM-DD), inclusive; must span the same number of days as window A
        window_b_end: window B end, ISO date (YYYY-MM-DD), inclusive; must span the same number of days as window A
    """
    inp: CompareMetricsInputs = _inputs(
        CompareMetricsInputs, service=service, metric=metric,
        window_a_start=window_a_start, window_a_end=window_a_end,
        window_b_start=window_b_start, window_b_end=window_b_end,
    )
    _require_service(inp.service)
    started = time.perf_counter()
    result = _store.compare(
        inp.service, inp.metric, inp.a_start, inp.a_end, inp.b_start, inp.b_end
    )
    a, b = result["window_a"], result["window_b"]
    delta = round(b["avg"] - a["avg"], 3)
    payload = {
        "service": inp.service,
        "metric": inp.metric,
        "window_a": {"start": inp.window_a_start, "end": inp.window_a_end, **a},
        "window_b": {"start": inp.window_b_start, "end": inp.window_b_end, **b},
        "delta": delta,
        "delta_pct": round(delta / a["avg"] * 100, 2) if a["avg"] else None,
    }
    return _finish("compare_metric_windows", started, payload)


@mcp.tool()
async def acknowledge_alert(
    alert_id: str,
    acknowledged_by: str,
    note: str | None = None,
) -> dict[str, Any]:
    """Acknowledges an alert: records who acknowledged it and when. WRITE TOOL.

    This is the only write operation in the demo stack. It is NOT part of the
    read-only investigation toolset: the investigation agent must never call it
    directly — it is invoked by the human-approval service after explicit
    approval. Rejects unknown alerts and already-acknowledged alerts.

    Args:
        alert_id: identifier of the alert to acknowledge (e.g. "ALT-2071")
        acknowledged_by: non-blank operator label recorded as the acknowledger
        note: optional acknowledgement note (max 200 characters)
    """
    inp: AcknowledgeAlertInputs = _inputs(
        AcknowledgeAlertInputs,
        alert_id=alert_id, acknowledged_by=acknowledged_by, note=note,
    )
    _require_alert(inp.alert_id)
    started = time.perf_counter()
    at = datetime.now(timezone.utc)
    try:
        record = _store.acknowledge_alert(
            inp.alert_id, inp.acknowledged_by, inp.note, at
        )
    except ValueError as exc:
        raise ToolError(str(exc)) from None
    return _finish(
        "acknowledge_alert", started,
        {
            "acknowledged": True,
            "alert_id": record["id"],
            "service": record["service"],
            "acknowledged_at": record["acknowledged_at"],
            "acknowledged_by": record["acknowledged_by"],
            "acknowledgement_note": record.get("acknowledgement_note"),
            "count": 1,
        },
    )
