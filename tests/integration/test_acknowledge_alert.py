"""Phase 7 Gate 2: `acknowledge_alert` write tool over real monitoring MCP.

Verifies acknowledgement, read-back on the SAME session (shared in-memory
store), unknown/duplicate rejections, and the documented cross-session reset.
"""

import sys

import pytest

from backend.mcp.errors import MCPClientError

from backend.mcp.client_manager import MCPClientManager

EXPECTED_TOOLS = {
    "get_service_metrics",
    "get_error_rate",
    "get_incidents",
    "get_recent_alerts",
    "compare_metric_windows",
    "acknowledge_alert",  # raw discovery: write tool present, excluded from LLM catalog
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


async def test_discovery_includes_write_tool(monitoring_manager):
    async with monitoring_manager as mcp:
        names = {tool.name for tool in mcp.tools()}
    assert "acknowledge_alert" in names
    assert EXPECTED_TOOLS <= names


async def test_acknowledge_alert_write_and_same_session_read_back(monitoring_manager):
    async with monitoring_manager as mcp:
        before = mcp_data(await mcp.call_tool(
            "get_recent_alerts",
            {"window_start": "2026-10-01", "window_end": "2026-10-05", "severity": "high"},
        ))
        target = next(a for a in before["alerts"] if a["id"] == "ALT-2071")
        assert target["acknowledged"] is False

        result = mcp_data(await mcp.call_tool(
            "acknowledge_alert",
            {"alert_id": "ALT-2071", "acknowledged_by": "operator-1",
             "note": "acknowledged during incident response"},
        ))
        assert result["acknowledged"] is True
        assert result["acknowledged_by"] == "operator-1"

        # SAME session: the read-back observes the write (shared in-memory store)
        after = mcp_data(await mcp.call_tool(
            "get_recent_alerts",
            {"window_start": "2026-10-01", "window_end": "2026-10-05", "severity": "high"},
        ))
    acknowledged = next(a for a in after["alerts"] if a["id"] == "ALT-2071")
    assert acknowledged["acknowledged"] is True
    assert acknowledged["acknowledged_by"] == "operator-1"
    assert acknowledged["acknowledged_at"] is not None


async def test_cross_session_reset_is_documented(monitoring_manager):
    """Acknowledgement state lives in the monitoring process's memory: a NEW
    server process starts with the canonical unacknowledged fixtures, while
    approval history stays durable in the state store."""
    async with MCPClientManager(
        "monitoring-mcp", sys.executable, ["-m", "backend.mcp.servers.monitoring_mcp"],
        call_timeout_seconds=15,
    ) as first:
        await first.call_tool("acknowledge_alert",
                              {"alert_id": "ALT-2071", "acknowledged_by": "op"})
    async with MCPClientManager(
        "monitoring-mcp", sys.executable, ["-m", "backend.mcp.servers.monitoring_mcp"],
        call_timeout_seconds=15,
    ) as second:
        after = mcp_data(await second.call_tool(
            "get_recent_alerts",
            {"window_start": "2026-10-01", "window_end": "2026-10-05", "service": "checkout"},
        ))
    assert after["alerts"][0]["acknowledged"] is False, \
        "a fresh monitoring process starts from canonical fixtures"


async def test_unknown_alert_is_typed_error(monitoring_manager):
    async with monitoring_manager as mcp:
        with pytest.raises(MCPClientError) as excinfo:
            await mcp.call_tool(
                "acknowledge_alert",
                {"alert_id": "ALT-NOPE", "acknowledged_by": "op"},
            )
    assert "unknown alert" in str(excinfo.value)


async def test_duplicate_acknowledgement_is_rejected(monitoring_manager):
    async with monitoring_manager as mcp:
        await mcp.call_tool("acknowledge_alert",
                            {"alert_id": "ALT-2071", "acknowledged_by": "op"})
        with pytest.raises(MCPClientError) as excinfo:
            await mcp.call_tool("acknowledge_alert",
                                {"alert_id": "ALT-2071", "acknowledged_by": "op-2"})
    assert "already acknowledged" in str(excinfo.value)


async def test_blank_operator_label_is_rejected(monitoring_manager):
    async with monitoring_manager as mcp:
        with pytest.raises(MCPClientError):
            await mcp.call_tool("acknowledge_alert",
                                {"alert_id": "ALT-2071", "acknowledged_by": "   "})

