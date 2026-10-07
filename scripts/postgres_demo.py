"""Phase 2 demo: verify seeded PostgreSQL and exercise all five postgres-mcp
tools through real MCP (discovery + invocation over stdio).

    uv run python scripts/postgres_demo.py

Starts the Dockerized Postgres if it is not reachable, seeds when the stored
scenario does not match the spec, then runs the round-trip.
"""

import asyncio
import subprocess
import sys
import time

import asyncpg
import structlog

from backend.config import get_settings
from backend.mcp.client_manager import MCPClientManager, result_text
from backend.mcp.fixtures.sales_seed import DEFAULT_ADMIN_DSN, ensure_seeded_async
from backend.mcp.fixtures.loader import load_scenario
from backend.observability.logging import configure_logging

EXPECTED_TOOLS = {
    "get_daily_sales",
    "compare_sales",
    "get_product_sales",
    "get_region_sales",
    "get_sales_trend",
}

CANON_CALLS: list[tuple[str, dict, str]] = [
    (
        "get_daily_sales",
        {"start_date": "2026-09-28", "end_date": "2026-10-05", "limit": 10},
        "the decline window vs healthy days",
    ),
    (
        "compare_sales",
        {
            "window_a_start": "2026-09-30", "window_a_end": "2026-10-02",
            "window_b_start": "2026-10-03", "window_b_end": "2026-10-05",
            "metric": "revenue", "group_by": "channel",
        },
        "the 3 healthy days right before the incident vs the 3 incident days, by channel",
    ),
    (
        "get_product_sales",
        {"start_date": "2026-10-03", "end_date": "2026-10-05", "limit": 5},
        "top products during the incident",
    ),
    (
        "get_region_sales",
        {"start_date": "2026-10-03", "end_date": "2026-10-05", "limit": 10},
        "regional impact during the incident",
    ),
    (
        "get_sales_trend",
        {"start_date": "2026-09-01", "end_date": "2026-10-05", "granularity": "week"},
        "weekly trend around the incident",
    ),
]


def _admin_dsn() -> str:
    return get_settings().ops_admin_dsn or DEFAULT_ADMIN_DSN


async def _reachable(dsn: str) -> bool:
    try:
        conn = await asyncpg.connect(dsn, timeout=2)
        await conn.close()
        return True
    except Exception:
        return False


async def ensure_postgres(dsn: str, wait_seconds: int = 60) -> bool:
    if await _reachable(dsn):
        return True
    print("PostgreSQL not reachable — starting docker compose service...")
    subprocess.run(["docker", "compose", "up", "-d", "postgres"], check=False)
    deadline = time.monotonic() + wait_seconds
    while time.monotonic() < deadline:
        if await _reachable(dsn):
            return True
        await asyncio.sleep(1)
    return False


def data_of(result) -> dict:
    structured = getattr(result, "structured_content", None)
    if structured:
        return structured
    import json

    return json.loads(result_text(result))


async def run_demo() -> int:
    spec = load_scenario()
    print(f"Scenario: {spec.name}  ({spec.start_date} .. {spec.end_date}, incident "
          f"{spec.incident.start_date}..{spec.incident.end_date} on channel "
          f"'{spec.incident.affected_channel}')")

    admin = _admin_dsn()
    if not await ensure_postgres(admin):
        print("FAILED: PostgreSQL unreachable. Start it with: docker compose up -d postgres")
        return 1
    meta = await ensure_seeded_async(admin, spec)
    print(f"Seed data OK ({'seeded now' if meta.get('seeded_now') else 'already current'}): "
          f"{meta['checksum']}")

    print("\n=== Starting MCP server: backend.mcp.servers.postgres_mcp (stdio) ===")
    async with MCPClientManager(
        "postgres-mcp", sys.executable, ["-m", "backend.mcp.servers.postgres_mcp"]
    ) as mcp:
        tools = {tool.name for tool in mcp.tools()}
        print(f"=== Discovered tools: {sorted(tools)} ===")
        if tools != EXPECTED_TOOLS:
            print(f"FAILED: expected exactly {sorted(EXPECTED_TOOLS)}, got {sorted(tools)}")
            return 1

        for name, args, why in CANON_CALLS:
            result = await mcp.call_tool(name, args)
            data = data_of(result)
            summary = {k: v for k, v in data.items() if k != "rows"}
            print(f"\n--- {name} ({why}) ---")
            print(f"  meta: {summary}")
            for row in (data.get("rows") or data.get("breakdown") or [])[:6]:
                print(f"  {row}")

    print("\nPHASE 2 DEMO PASSED")
    log = structlog.get_logger("postgres_demo")
    log.info("demo_complete")
    return 0


def main() -> int:
    configure_logging()
    return asyncio.run(run_demo())


if __name__ == "__main__":
    raise SystemExit(main())
