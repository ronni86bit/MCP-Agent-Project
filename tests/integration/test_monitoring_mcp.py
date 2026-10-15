"""monitoring-mcp integration tests: discovery, five tools, validation,
empty windows, determinism — through real MCP stdio round-trips."""

import sys

import pytest

from backend.mcp.client_manager import MCPClientManager
from backend.mcp.errors import MCPClientError, ToolExecutionError

EXPECTED_TOOLS = {
    "monitoring-mcp__get_service_metrics",
    "monitoring-mcp__get_error_rate",
    "monitoring-mcp__get_incidents",
    "monitoring-mcp__get_recent_alerts",
    "monitoring-mcp__compare_metric_windows",
}


@pytest.fixture
def monitoring_manager():
    return MCPClientManager(
        "monitoring-mcp",
        sys.executable,
        ["-m", "backend.mcp.servers.monitoring_mcp"],
        call_timeout_seconds=15,
    )


def mcp_data(result) -> dict:
    structured = getattr(result, "structured_content", None)
    if structured:
        return structured
    import json

    return json.loads("".join(getattr(b, "text", "") for b in result.content))


async def test_discovery_offers_five_readonly_tools_plus_excluded_write(monitoring_manager):
    """Raw monitoring discovery lists the 5 approved read-only tools plus the
    acknowledge_alert WRITE tool, which stays excluded from the LLM catalog
    (SERVER_ALLOWLISTS) and from the agent executor's policy gate."""
    from backend.agent.catalog import SERVER_ALLOWLISTS

    async with monitoring_manager as mcp:
        names = {tool.name for tool in mcp.tools()}
    readonly = SERVER_ALLOWLISTS["monitoring-mcp"]
    assert names == readonly | {"acknowledge_alert"}
    assert "acknowledge_alert" not in readonly


async def test_get_service_metrics_returns_hourly_series(monitoring_manager):
    async with monitoring_manager as mcp:
        data = mcp_data(await mcp.call_tool(
            "get_service_metrics",
            {"service": "checkout", "window_start": "2026-10-03", "window_end": "2026-10-03", "limit": 30},
        ))
    assert data["count"] == 24  # one full day of hourly points
    for point in data["points"]:
        assert set(point) == {"timestamp", "error_rate", "latency_p95_ms", "requests_per_min"}


async def test_get_error_rate_flags_the_degradation(monitoring_manager):
    async with monitoring_manager as mcp:
        incident = mcp_data(await mcp.call_tool(
            "get_error_rate",
            {"service": "checkout", "window_start": "2026-10-03", "window_end": "2026-10-05"},
        ))
        healthy = mcp_data(await mcp.call_tool(
            "get_error_rate",
            {"service": "checkout", "window_start": "2026-09-20", "window_end": "2026-09-22"},
        ))
    assert incident["summary"]["window_avg_pct"] > 5.0
    assert healthy["summary"]["window_avg_pct"] < 2.0
    assert len(incident["daily"]) == 3


async def test_get_incidents_and_alerts_find_the_canonical_records(monitoring_manager):
    async with monitoring_manager as mcp:
        incidents = mcp_data(await mcp.call_tool(
            "get_incidents", {"window_start": "2026-10-01", "window_end": "2026-10-05", "service": "checkout"}
        ))
        alerts = mcp_data(await mcp.call_tool(
            "get_recent_alerts",
            {"window_start": "2026-10-01", "window_end": "2026-10-05", "severity": "high"},
        ))
    assert incidents["count"] == 1
    assert incidents["incidents"][0]["id"] == "INC-1042"
    assert incidents["incidents"][0]["resolved_at"] is None  # still open at "today"
    assert any(alert["id"] == "ALT-2071" for alert in alerts["alerts"])


async def test_compare_metric_windows_before_and_after(monitoring_manager):
    async with monitoring_manager as mcp:
        data = mcp_data(await mcp.call_tool(
            "compare_metric_windows",
            {
                "service": "checkout", "metric": "error_rate",
                "window_a_start": "2026-09-30", "window_a_end": "2026-10-02",
                "window_b_start": "2026-10-03", "window_b_end": "2026-10-05",
            },
        ))
    assert data["delta"] > 0
    assert data["window_b"]["avg"] > data["window_a"]["avg"] * 3


async def test_unknown_service_is_typed_error_with_valid_names(monitoring_manager):
    async with monitoring_manager as mcp:
        with pytest.raises(MCPClientError) as excinfo:
            await mcp.call_tool(
                "get_error_rate",
                {"service": "not-a-service", "window_start": "2026-10-03", "window_end": "2026-10-05"},
            )
    assert "unknown service" in str(excinfo.value)
    assert "checkout" in str(excinfo.value)  # valid names listed for the agent


async def test_unequal_duration_compare_rejected_with_explanation(monitoring_manager):
    async with monitoring_manager as mcp:
        with pytest.raises(MCPClientError) as excinfo:
            await mcp.call_tool(
                "compare_metric_windows",
                {
                    "service": "checkout", "metric": "error_rate",
                    "window_a_start": "2026-09-06", "window_a_end": "2026-10-02",
                    "window_b_start": "2026-10-03", "window_b_end": "2026-10-05",
                },
            )
    assert "equal-duration" in str(excinfo.value)


async def test_empty_window_is_clean_not_an_error(monitoring_manager):
    async with monitoring_manager as mcp:
        incidents = mcp_data(await mcp.call_tool(
            "get_incidents", {"window_start": "1999-01-01", "window_end": "1999-01-31"}
        ))
        alerts = mcp_data(await mcp.call_tool(
            "get_recent_alerts", {"window_start": "1999-01-01", "window_end": "1999-01-31"}
        ))
    assert incidents["count"] == 0 and incidents["incidents"] == []
    assert alerts["count"] == 0


async def test_invalid_window_and_severity_are_typed_errors(monitoring_manager):
    async with monitoring_manager as mcp:
        with pytest.raises((MCPClientError, ToolExecutionError)):
            await mcp.call_tool(
                "get_service_metrics",
                {"service": "checkout", "window_start": "2026-09-01", "window_end": "2026-10-05"},  # >14 days
            )
        with pytest.raises((MCPClientError, ToolExecutionError)):
            await mcp.call_tool(
                "get_recent_alerts",
                {"window_start": "2026-10-03", "window_end": "2026-10-05", "severity": "mega"},
            )


async def test_fixture_serving_is_deterministic(monitoring_manager):
    async with monitoring_manager as mcp:
        first = mcp_data(await mcp.call_tool(
            "get_error_rate",
            {"service": "checkout", "window_start": "2026-10-03", "window_end": "2026-10-05"},
        ))
        second = mcp_data(await mcp.call_tool(
            "get_error_rate",
            {"service": "checkout", "window_start": "2026-10-03", "window_end": "2026-10-05"},
        ))
    assert first == second
