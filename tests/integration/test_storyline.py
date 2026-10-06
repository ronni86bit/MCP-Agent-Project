"""Canonical storyline coherence tests.

Assert that the SEEDED DATABASE (not just the generator) satisfies the
canonical "Why did today's sales drop?" storyline: healthy baseline, a
measurable decline window, concentration in the affected channel, global
regional impact, deterministic data — all readable through the same read-only
role the MCP server uses. No GitHub/Monitoring claims are made here (Phase 4).
"""

import asyncio
import json
import statistics

import asyncpg

from backend.mcp.client_manager import result_text
from backend.mcp.fixtures.loader import load_scenario
from backend.mcp.fixtures.sales_seed import build_sales_rows, checksum, fetch_meta, seed_database
from tests.conftest import ADMIN_DSN

DAILY_SQL = """
SELECT sale_date::text AS day, SUM(revenue)::float8 AS revenue, SUM(units)::bigint AS units
FROM sales_data.daily_sales
WHERE sale_date BETWEEN $1 AND $2
GROUP BY sale_date ORDER BY sale_date
"""
CHANNEL_SQL = """
SELECT channel, SUM(revenue)::float8 AS revenue
FROM sales_data.daily_sales
WHERE sale_date BETWEEN $1 AND $2
GROUP BY channel
"""
REGION_SQL = """
SELECT r.name AS region, SUM(ds.revenue)::float8 AS revenue
FROM sales_data.daily_sales ds
JOIN sales_data.regions r ON r.region_id = ds.region_id
WHERE ds.sale_date BETWEEN $1 AND $2
GROUP BY r.name
"""


async def _rows(dsn: str, sql: str, *args) -> list[asyncpg.Record]:
    conn = await asyncpg.connect(dsn, timeout=10)
    try:
        return list(await conn.fetch(sql, *args))
    finally:
        await conn.close()


def mcp_data(result) -> dict:
    structured = getattr(result, "structured_content", None)
    if structured:
        return structured
    return json.loads(result_text(result))


def test_seed_metadata_matches_the_scenario_spec(admin_dsn):
    spec = load_scenario()
    meta = fetch_meta(admin_dsn)
    assert meta is not None, "database is not seeded"
    assert meta["scenario_name"] == spec.name
    assert meta["seed"] == str(spec.seed)
    assert meta["start_date"] == spec.start_date.isoformat()
    assert meta["end_date"] == spec.end_date.isoformat()
    assert meta["incident_start"] == spec.incident.start_date.isoformat()
    assert meta["affected_channel"] == spec.incident.affected_channel


def test_database_checksum_matches_regenerated_data(admin_dsn):
    meta = fetch_meta(admin_dsn)
    expected = checksum(build_sales_rows(load_scenario()))
    assert meta["checksum"] == expected


def test_reseeding_is_deterministic(admin_dsn):
    first = seed_database(admin_dsn)
    second = seed_database(admin_dsn)
    assert first["checksum"] == second["checksum"]


async def test_baseline_is_stable_and_healthy(ro_dsn):
    spec = load_scenario()
    rows = await _rows(ro_dsn, DAILY_SQL, spec.baseline.start_date, spec.baseline.end_date)
    revenues = [row["revenue"] for row in rows]
    assert len(revenues) == spec.baseline.days
    cv = statistics.stdev(revenues) / statistics.mean(revenues)
    assert cv < 0.12


async def test_decline_is_measurable_against_the_baseline(ro_dsn):
    spec = load_scenario()
    baseline = await _rows(ro_dsn, DAILY_SQL, spec.baseline.start_date, spec.baseline.end_date)
    incident = await _rows(ro_dsn, DAILY_SQL, spec.incident.start_date, spec.incident.end_date)
    ratio = statistics.mean(r["revenue"] for r in incident) / statistics.mean(
        r["revenue"] for r in baseline
    )
    assert ratio <= 0.80, "incident-window daily revenue must drop by >= 20% vs baseline"
    # "today" (the last seeded day) must itself be inside the decline
    last = await _rows(ro_dsn, DAILY_SQL, spec.end_date, spec.end_date)
    assert last[0]["revenue"] < statistics.mean(r["revenue"] for r in baseline)


async def test_decline_concentrates_in_the_affected_channel(ro_dsn):
    spec = load_scenario()
    baseline = {
        row["channel"]: row["revenue"] / spec.baseline.days
        for row in await _rows(ro_dsn, CHANNEL_SQL, spec.baseline.start_date, spec.baseline.end_date)
    }
    incident = {
        row["channel"]: row["revenue"] / spec.incident.days
        for row in await _rows(ro_dsn, CHANNEL_SQL, spec.incident.start_date, spec.incident.end_date)
    }
    affected = spec.incident.affected_channel
    healthy = "desktop"
    mobile_decline = incident[affected] / baseline[affected] - 1
    desktop_decline = incident[healthy] / baseline[healthy] - 1
    assert mobile_decline <= -0.40
    assert -0.12 <= desktop_decline < 0
    assert abs(mobile_decline) >= 5 * abs(desktop_decline)


async def test_all_regions_decline_global_checkout_impact(ro_dsn):
    spec = load_scenario()
    baseline = {
        row["region"]: row["revenue"] / spec.baseline.days
        for row in await _rows(ro_dsn, REGION_SQL, spec.baseline.start_date, spec.baseline.end_date)
    }
    incident = {
        row["region"]: row["revenue"] / spec.incident.days
        for row in await _rows(ro_dsn, REGION_SQL, spec.incident.start_date, spec.incident.end_date)
    }
    assert set(baseline) == set(incident)
    for region, base in baseline.items():
        assert incident[region] / base - 1 <= -0.15, region


async def test_canonical_question_is_answerable_through_mcp(pg_manager):
    spec = load_scenario()
    async with pg_manager() as mcp:
        daily = mcp_data(await mcp.call_tool(
            "get_daily_sales",
            {
                "start_date": spec.baseline.end_date.isoformat(),
                "end_date": spec.end_date.isoformat(),
                "limit": 10,
            },
        ))
        compare = mcp_data(await mcp.call_tool(
            "compare_sales",
            {
                "window_a_start": "2026-09-28", "window_a_end": "2026-09-30",
                "window_b_start": spec.incident.start_date.isoformat(),
                "window_b_end": spec.incident.end_date.isoformat(),
                "metric": "revenue", "group_by": "channel",
            },
        ))
    # the visible story: healthy days, then a drop on the incident days
    healthy = [row["revenue"] for row in daily["rows"] if row["date"] < spec.incident.start_date.isoformat()]
    dropped = [row["revenue"] for row in daily["rows"] if row["date"] >= spec.incident.start_date.isoformat()]
    assert healthy and dropped
    assert statistics.mean(dropped) / statistics.mean(healthy) <= 0.80

    assert compare["delta_pct"] <= -20
    worst = compare["breakdown"][0]
    assert worst["group"] == spec.incident.affected_channel
