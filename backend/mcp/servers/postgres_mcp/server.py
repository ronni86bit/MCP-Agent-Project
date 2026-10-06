"""postgres-mcp tool definitions (five approved read-only tools, nothing else).

Handlers stay thin: validate inputs (models.py) -> run the allowlisted template
(queries.py) via the read-only connection (db.py) -> return a predictable dict.
Invalid input raises ToolError so the client sees a clean, single-line reason.
"""

from __future__ import annotations

import time
from typing import Any

import structlog
from pydantic import ValidationError
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from backend.mcp.servers.postgres_mcp import db, queries
from backend.mcp.servers.postgres_mcp.models import (
    CompareInputs,
    DailySalesInputs,
    ProductSalesInputs,
    RegionSalesInputs,
    TrendInputs,
)

mcp = MCPServer(
    "postgres-mcp",
    instructions=(
        "Read-only business sales analytics over the ops-agent demo dataset "
        "(~90 days of orders/units/revenue by product, region, and channel). "
        "Use these tools to quantify business metrics, compare windows, and "
        "detect changes over time. All tools are read-only."
    ),
)

log = structlog.get_logger(__name__)


def _inputs(model: type, **kwargs: Any) -> Any:
    try:
        return model(**kwargs)
    except ValidationError as exc:
        reasons = "; ".join(
            f"{'.'.join(str(part) for part in err['loc'])}: {err['msg']}" for err in exc.errors()
        )
        raise ToolError(f"invalid arguments: {reasons}") from None


def _finish(tool: str, started: float, payload: dict[str, Any]) -> dict[str, Any]:
    log.info(
        "pg_tool_executed",
        tool=tool,
        rows=payload.get("count", 0),
        duration_ms=round((time.perf_counter() - started) * 1000, 1),
    )
    return payload


@mcp.tool()
async def get_daily_sales(
    start_date: str,
    end_date: str,
    product: str | None = None,
    region: str | None = None,
    channel: str | None = None,
    limit: int = 90,
) -> dict[str, Any]:
    """Daily sales totals (orders, units, revenue) over an inclusive date range.

    Returns one row per day, ordered by date, optionally filtered by exact
    product name, region name, or sales channel.

    Args:
        start_date: range start as ISO date (YYYY-MM-DD), inclusive
        end_date: range end as ISO date (YYYY-MM-DD), inclusive; at most 180 days after start_date
        product: optional exact product name filter (e.g. "Aurora Headphones")
        region: optional exact region name filter (e.g. "Europe")
        channel: optional exact sales channel filter ("mobile" or "desktop")
        limit: maximum number of daily rows to return (1-400, default 90)
    """
    inp: DailySalesInputs = _inputs(
        DailySalesInputs,
        start_date=start_date, end_date=end_date, product=product,
        region=region, channel=channel, limit=limit,
    )
    started = time.perf_counter()
    records = await db.fetch(
        queries.DAILY_SALES_SQL,
        inp.start, inp.end, inp.product, inp.region, inp.channel, inp.limit,
    )
    rows = [
        {
            "date": r["sale_date"],
            "orders": r["orders"],
            "units": r["units"],
            "revenue": round(r["revenue"], 2),
        }
        for r in records
    ]
    return _finish(
        "get_daily_sales", started,
        {"range": {"start": inp.start_date, "end": inp.end_date}, "count": len(rows), "rows": rows},
    )


@mcp.tool()
async def compare_sales(
    window_a_start: str,
    window_a_end: str,
    window_b_start: str,
    window_b_end: str,
    metric: str = "revenue",
    group_by: str = "none",
    product: str | None = None,
    region: str | None = None,
    channel: str | None = None,
) -> dict[str, Any]:
    """Compare sales between two date windows and quantify the change.

    Returns totals for each window plus absolute delta and percent change for
    the chosen metric. Use group_by to break the change down by product,
    region, or channel — the fastest way to locate where a change came from.

    Args:
        window_a_start: comparison-window A start, ISO date (YYYY-MM-DD), inclusive
        window_a_end: comparison-window A end, ISO date (YYYY-MM-DD), inclusive
        window_b_start: comparison-window B start, ISO date (YYYY-MM-DD), inclusive
        window_b_end: comparison-window B end, ISO date (YYYY-MM-DD), inclusive
        metric: which metric to compare: "revenue" (default), "orders", or "units"
        group_by: "none" (default), "product", "region", or "channel"
        product: optional exact product name filter
        region: optional exact region name filter
        channel: optional exact sales channel filter ("mobile" or "desktop")
    """
    inp: CompareInputs = _inputs(
        CompareInputs,
        window_a_start=window_a_start, window_a_end=window_a_end,
        window_b_start=window_b_start, window_b_end=window_b_end,
        metric=metric, group_by=group_by,
        product=product, region=region, channel=channel,
    )
    started = time.perf_counter()
    args_a = (inp.a_start, inp.a_end, inp.product, inp.region, inp.channel)
    args_b = (inp.b_start, inp.b_end, inp.product, inp.region, inp.channel)
    totals_a = dict(await db.fetchrow(queries.COMPARE_TOTAL_SQL, *args_a))
    totals_b = dict(await db.fetchrow(queries.COMPARE_TOTAL_SQL, *args_b))
    base, new = totals_a[inp.metric], totals_b[inp.metric]

    payload: dict[str, Any] = {
        "metric": inp.metric,
        "window_a": {"start": inp.window_a_start, "end": inp.window_a_end, **totals_a},
        "window_b": {"start": inp.window_b_start, "end": inp.window_b_end, **totals_b},
        "delta": round(new - base, 2),
        "delta_pct": round((new - base) / base * 100, 2) if base else None,
    }

    if inp.group_by != "none":
        group_sql = {
            "product": queries.COMPARE_BY_PRODUCT_SQL,
            "region": queries.COMPARE_BY_REGION_SQL,
            "channel": queries.COMPARE_BY_CHANNEL_SQL,
        }[inp.group_by]
        rows_a = {r["grp"]: r for r in await db.fetch(group_sql, *args_a)}
        rows_b = {r["grp"]: r for r in await db.fetch(group_sql, *args_b)}
        breakdown = []
        for group in sorted(set(rows_a) | set(rows_b)):
            a = rows_a.get(group, {}).get(inp.metric, 0)
            b = rows_b.get(group, {}).get(inp.metric, 0)
            breakdown.append(
                {
                    "group": group,
                    "window_a_total": a,
                    "window_b_total": b,
                    "delta": round(b - a, 2),
                    "delta_pct": round((b - a) / a * 100, 2) if a else None,
                }
            )
        breakdown.sort(key=lambda item: item["delta"])
        payload["breakdown"] = breakdown
        payload["count"] = len(breakdown)

    return _finish("compare_sales", started, payload)


@mcp.tool()
async def get_product_sales(
    start_date: str,
    end_date: str,
    region: str | None = None,
    channel: str | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    """Per-product sales totals over an inclusive date range, ranked by revenue.

    Args:
        start_date: range start as ISO date (YYYY-MM-DD), inclusive
        end_date: range end as ISO date (YYYY-MM-DD), inclusive; at most 180 days after start_date
        region: optional exact region name filter (e.g. "Europe")
        channel: optional exact sales channel filter ("mobile" or "desktop")
        limit: maximum number of products to return (1-100, default 20)
    """
    inp: ProductSalesInputs = _inputs(
        ProductSalesInputs,
        start_date=start_date, end_date=end_date, region=region, channel=channel, limit=limit,
    )
    started = time.perf_counter()
    records = await db.fetch(
        queries.PRODUCT_SALES_SQL,
        inp.start, inp.end, inp.product, inp.region, inp.channel, inp.limit,
    )
    rows = [
        {
            "product_id": r["product_id"],
            "name": r["name"],
            "category": r["category"],
            "orders": r["orders"],
            "units": r["units"],
            "revenue": round(r["revenue"], 2),
        }
        for r in records
    ]
    return _finish(
        "get_product_sales", started,
        {"range": {"start": inp.start_date, "end": inp.end_date}, "count": len(rows), "rows": rows},
    )


@mcp.tool()
async def get_region_sales(
    start_date: str,
    end_date: str,
    product: str | None = None,
    channel: str | None = None,
    limit: int = 10,
) -> dict[str, Any]:
    """Per-region sales totals over an inclusive date range, ranked by revenue.

    Args:
        start_date: range start as ISO date (YYYY-MM-DD), inclusive
        end_date: range end as ISO date (YYYY-MM-DD), inclusive; at most 180 days after start_date
        product: optional exact product name filter (e.g. "Aurora Headphones")
        channel: optional exact sales channel filter ("mobile" or "desktop")
        limit: maximum number of regions to return (1-100, default 10)
    """
    inp: RegionSalesInputs = _inputs(
        RegionSalesInputs,
        start_date=start_date, end_date=end_date, product=product, channel=channel, limit=limit,
    )
    started = time.perf_counter()
    records = await db.fetch(
        queries.REGION_SALES_SQL,
        inp.start, inp.end, inp.product, inp.region, inp.channel, inp.limit,
    )
    rows = [
        {
            "region_id": r["region_id"],
            "name": r["name"],
            "country": r["country"],
            "orders": r["orders"],
            "units": r["units"],
            "revenue": round(r["revenue"], 2),
        }
        for r in records
    ]
    return _finish(
        "get_region_sales", started,
        {"range": {"start": inp.start_date, "end": inp.end_date}, "count": len(rows), "rows": rows},
    )


@mcp.tool()
async def get_sales_trend(
    start_date: str,
    end_date: str,
    granularity: str = "day",
    product: str | None = None,
    region: str | None = None,
    channel: str | None = None,
    limit: int = 200,
) -> dict[str, Any]:
    """Time series of sales (revenue, orders, units) to see movement over time.

    One point per period, ordered chronologically. Use granularity "week" to
    smooth daily noise when looking at multi-week patterns.

    Args:
        start_date: range start as ISO date (YYYY-MM-DD), inclusive
        end_date: range end as ISO date (YYYY-MM-DD), inclusive; at most 180 days after start_date
        granularity: point spacing: "day" (default) or "week" (ISO week, Monday start)
        product: optional exact product name filter
        region: optional exact region name filter
        channel: optional exact sales channel filter ("mobile" or "desktop")
        limit: maximum number of points to return (1-400, default 200)
    """
    inp: TrendInputs = _inputs(
        TrendInputs,
        start_date=start_date, end_date=end_date, granularity=granularity,
        product=product, region=region, channel=channel, limit=limit,
    )
    started = time.perf_counter()
    sql = queries.TREND_DAILY_SQL if inp.granularity == "day" else queries.TREND_WEEKLY_SQL
    records = await db.fetch(
        sql,
        inp.start, inp.end, inp.product, inp.region, inp.channel, inp.limit,
    )
    rows = [
        {
            "period": r["period"],
            "revenue": round(r["revenue"], 2),
            "orders": r["orders"],
            "units": r["units"],
        }
        for r in records
    ]
    return _finish(
        "get_sales_trend", started,
        {
            "range": {"start": inp.start_date, "end": inp.end_date},
            "granularity": inp.granularity,
            "count": len(rows),
            "rows": rows,
        },
    )
