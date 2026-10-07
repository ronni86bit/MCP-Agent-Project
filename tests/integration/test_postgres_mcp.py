"""postgres-mcp integration tests: discovery, tool behavior, validation,
caps, read-only enforcement — all through real MCP stdio round-trips."""

import asyncio
import json

import pytest
from asyncpg import exceptions as pgexc

from backend.mcp.client_manager import result_text
from backend.mcp.errors import InvalidArgumentsError, MCPClientError, ToolExecutionError
from tests.conftest import RO_DSN

EXPECTED_TOOLS = {
    "get_daily_sales",
    "compare_sales",
    "get_product_sales",
    "get_region_sales",
    "get_sales_trend",
}


def mcp_data(result) -> dict:
    structured = getattr(result, "structured_content", None)
    if structured:
        return structured
    return json.loads(result_text(result))


async def test_discovery_offers_exactly_the_five_approved_tools(pg_manager):
    async with pg_manager() as mcp:
        assert {tool.name for tool in mcp.tools()} == EXPECTED_TOOLS


async def test_daily_sales_roundtrip(pg_manager):
    async with pg_manager() as mcp:
        data = mcp_data(await mcp.call_tool(
            "get_daily_sales",
            {"start_date": "2026-09-28", "end_date": "2026-10-05", "limit": 10},
        ))
    assert data["count"] == 8
    dates = [row["date"] for row in data["rows"]]
    assert dates == sorted(dates)
    for row in data["rows"]:
        assert set(row) == {"date", "orders", "units", "revenue"}
        assert row["revenue"] > 0 and row["orders"] > 0


async def test_compare_sales_quantifies_the_canonical_decline(pg_manager):
    async with pg_manager() as mcp:
        data = mcp_data(await mcp.call_tool(
            "compare_sales",
            {
                "window_a_start": "2026-09-28", "window_a_end": "2026-09-30",
                "window_b_start": "2026-10-03", "window_b_end": "2026-10-05",
                "metric": "revenue", "group_by": "channel",
            },
        ))
    assert data["metric"] == "revenue"
    assert data["delta_pct"] is not None and data["delta_pct"] <= -20

    by_channel = {item["group"]: item for item in data["breakdown"]}
    assert set(by_channel) == {"mobile", "desktop"}
    assert by_channel["mobile"]["delta_pct"] <= -40
    assert by_channel["desktop"]["delta_pct"] >= -12
    assert data["breakdown"][0]["group"] == "mobile"  # sorted by delta: worst first


async def test_product_and_region_roundtrips(pg_manager):
    async with pg_manager() as mcp:
        products = mcp_data(await mcp.call_tool(
            "get_product_sales", {"start_date": "2026-10-03", "end_date": "2026-10-05", "limit": 5}
        ))
        regions = mcp_data(await mcp.call_tool(
            "get_region_sales", {"start_date": "2026-10-03", "end_date": "2026-10-05"}
        ))
        europe = mcp_data(await mcp.call_tool(
            "get_product_sales",
            {"start_date": "2026-10-03", "end_date": "2026-10-05", "region": "Europe"},
        ))
    assert 0 < products["count"] <= 5
    revenues = [row["revenue"] for row in products["rows"]]
    assert revenues == sorted(revenues, reverse=True)
    assert regions["count"] == 4
    assert {row["name"] for row in regions["rows"]} == {
        "North America", "Europe", "Asia Pacific", "Latin America",
    }
    assert 0 < europe["count"] <= 8  # region-filtered product list over the same window


async def test_trend_day_and_week_are_consistent(pg_manager):
    async with pg_manager() as mcp:
        daily = mcp_data(await mcp.call_tool(
            "get_sales_trend",
            {"start_date": "2026-07-08", "end_date": "2026-10-05", "granularity": "day"},
        ))
        weekly = mcp_data(await mcp.call_tool(
            "get_sales_trend",
            {"start_date": "2026-07-08", "end_date": "2026-10-05", "granularity": "week"},
        ))
    assert daily["count"] == 90
    assert 13 <= weekly["count"] <= 14
    assert sum(row["revenue"] for row in daily["rows"]) == pytest.approx(
        sum(row["revenue"] for row in weekly["rows"]), rel=1e-6
    )


async def test_empty_results_are_clean_not_errors(pg_manager):
    async with pg_manager() as mcp:
        empty_range = mcp_data(await mcp.call_tool(
            "get_daily_sales", {"start_date": "1999-01-01", "end_date": "1999-01-31"}
        ))
        unknown_product = mcp_data(await mcp.call_tool(
            "get_daily_sales",
            {"start_date": "2026-10-03", "end_date": "2026-10-05", "product": "No Such Product"},
        ))
    assert empty_range == {"range": {"start": "1999-01-01", "end": "1999-01-31"}, "count": 0, "rows": []}
    assert unknown_product["count"] == 0


async def test_invalid_arguments_are_typed_errors(pg_manager):
    bad_calls = [
        ("get_daily_sales", {"start_date": "not-a-date", "end_date": "2026-10-05"}),
        ("get_daily_sales", {"start_date": "2026-10-05", "end_date": "2026-10-01"}),
        ("get_daily_sales", {"start_date": "2025-01-01", "end_date": "2025-12-31"}),
        ("get_daily_sales", {"start_date": "2026-09-01", "end_date": "2026-09-30", "limit": 0}),
        ("compare_sales", {
            "window_a_start": "2026-09-28", "window_a_end": "2026-09-30",
            "window_b_start": "2026-10-03", "window_b_end": "2026-10-05",
            "metric": "sales",
        }),
        ("compare_sales", {
            "window_a_start": "2026-09-06", "window_a_end": "2026-10-02",  # 27 days
            "window_b_start": "2026-10-03", "window_b_end": "2026-10-05",  # 3 days
        }),
        ("compare_sales", {
            "window_a_start": "2025-01-01", "window_a_end": "2025-12-31",
            "window_b_start": "2026-09-06", "window_b_end": "2026-10-02",
        }),
    ]
    async with pg_manager() as mcp:
        for name, args in bad_calls:
            with pytest.raises((InvalidArgumentsError, ToolExecutionError)) as excinfo:
                await mcp.call_tool(name, args)
        # the unequal-window rejection explains the constraint to the agent
        with pytest.raises(MCPClientError) as unequal:
            await mcp.call_tool("compare_sales", {
                "window_a_start": "2026-09-06", "window_a_end": "2026-10-02",
                "window_b_start": "2026-10-03", "window_b_end": "2026-10-05",
            })
        assert "equal-duration" in str(unequal.value)
        # the session must remain usable after typed failures
        assert mcp_data(await mcp.call_tool(
            "get_daily_sales", {"start_date": "2026-10-05", "end_date": "2026-10-05"}
        ))["count"] == 1


async def test_row_caps_enforced(pg_manager):
    async with pg_manager() as mcp:
        trend = mcp_data(await mcp.call_tool(
            "get_sales_trend",
            {"start_date": "2026-07-08", "end_date": "2026-10-05", "limit": 5},
        ))
        products = mcp_data(await mcp.call_tool(
            "get_product_sales", {"start_date": "2026-07-08", "end_date": "2026-10-05", "limit": 2}
        ))
    assert trend["count"] == 5 and len(trend["rows"]) == 5
    assert products["count"] <= 2


async def test_readonly_role_rejects_writes(ro_dsn):
    conn = await asyncio.wait_for(
        asyncpg_connect(ro_dsn), timeout=10
    )
    try:
        with pytest.raises((pgexc.InsufficientPrivilegeError, pgexc.ReadOnlySQLTransactionError)):
            await conn.execute(
                "INSERT INTO sales_data.products (product_id, name, category) VALUES (99, 'X', 'Y')"
            )
        with pytest.raises((pgexc.InsufficientPrivilegeError, pgexc.ReadOnlySQLTransactionError)):
            await conn.execute("DELETE FROM sales_data.daily_sales")
        with pytest.raises((pgexc.InsufficientPrivilegeError, pgexc.ReadOnlySQLTransactionError)):
            await conn.execute("CREATE TABLE sales_data.pwned (id int)")
    finally:
        await conn.close()


async def asyncpg_connect(dsn: str):
    import asyncpg

    return await asyncpg.connect(dsn, timeout=5)
