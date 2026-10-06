"""Deterministic seed data for the sales/business database.

`build_sales_rows` is a pure function of the scenario spec: identical specs
produce identical datasets (stdlib RNG with a fixed seed, fixed iteration
order, no wall-clock input). `seed_database` applies it idempotently — it drops
and recreates the `sales_data` schema, creates the dedicated read-only role for
the postgres-mcp server, and records scenario metadata for coherence tests.
"""

from __future__ import annotations

import asyncio
import os
import random
from datetime import timedelta
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

import asyncpg
from asyncpg.exceptions import DuplicateObjectError

from backend.mcp.fixtures.loader import ScenarioSpec, load_scenario

DEFAULT_ADMIN_DSN = "postgresql://ops_admin:ops_admin_dev@localhost:5433/ops_agent"
DEFAULT_RO_PASSWORD = "ro_dev_password"
SEED_VERSION = "1"
SCHEMA = "sales_data"

# (id, name, category, unit_price, popularity) — popularity scales daily units.
PRODUCTS: list[tuple[int, str, str, float, float]] = [
    (1, "Aurora Headphones", "Electronics", 129.99, 52.0),
    (2, "Nimbus Keyboard", "Electronics", 89.50, 36.0),
    (3, "Vertex Phone Stand", "Electronics", 29.50, 24.0),
    (4, "Summit Backpack", "Outdoor", 74.25, 28.0),
    (5, "Trail Water Bottle", "Outdoor", 24.99, 44.0),
    (6, "Lumen Desk Lamp", "Home", 45.00, 20.0),
    (7, "Drift Coffee Beans", "Grocery", 18.75, 60.0),
    (8, "Cobalt Notebook", "Stationery", 9.99, 68.0),
]

# (id, name, country)
REGIONS: list[tuple[int, str, str]] = [
    (1, "North America", "US"),
    (2, "Europe", "DE"),
    (3, "Asia Pacific", "SG"),
    (4, "Latin America", "BR"),
]

# (name, share of units)
CHANNELS: list[tuple[str, float]] = [
    ("mobile", 0.55),
    ("desktop", 0.45),
]

_GROWTH_PER_DAY = 0.0006          # ~+5.6% cumulative over the 90-day range
_WEEKDAY_FACTOR = {4: 1.12, 5: 1.15, 6: 1.05}  # Mon=0..Sun=6 (Fri/Sat/Sun retail lift)
_NOISE_RANGE = (0.94, 1.06)
_ORDER_RATIO_RANGE = (0.62, 0.78)
_SCALE = 2.0                      # global units scale so aggregates stay smooth
_CENTS = Decimal("0.01")


def _money(value: Decimal | float | int) -> Decimal:
    return Decimal(str(value)).quantize(_CENTS, rounding=ROUND_HALF_UP)


def build_sales_rows(spec: ScenarioSpec) -> list[dict[str, Any]]:
    """Generate the full daily_sales fact table as a list of row dicts."""
    rng = random.Random(spec.seed)
    region_weights = {1: 0.34, 2: 0.28, 3: 0.24, 4: 0.14}
    channel_weights = dict(CHANNELS)
    affected = spec.incident.affected_channel

    rows: list[dict[str, Any]] = []
    for day_offset in range(spec.days):
        current = spec.start_date + timedelta(days=day_offset)
        growth = 1.0 + _GROWTH_PER_DAY * day_offset
        weekday = _WEEKDAY_FACTOR.get(current.weekday(), 1.0)
        in_incident = spec.incident.start_date <= current <= spec.incident.end_date
        incident_offset = (current - spec.incident.start_date).days if in_incident else None

        for channel, channel_weight in CHANNELS:
            # Healthy-season drift keeps the incident from being the only variation.
            channel_factor = rng.uniform(0.98, 1.02)
            if incident_offset is not None:
                channel_factor = spec.incident_day_multipliers(channel, incident_offset)

            for region_id, _, _ in REGIONS:
                for product_id, _, _, unit_price, popularity in PRODUCTS:
                    base = popularity * region_weights[region_id] * channel_weight * _SCALE
                    noise = rng.uniform(*_NOISE_RANGE)
                    units = max(0, int(round(base * growth * weekday * channel_factor * noise)))
                    orders = int(round(units * rng.uniform(*_ORDER_RATIO_RANGE)))
                    rows.append(
                        {
                            "sale_date": current,
                            "product_id": product_id,
                            "region_id": region_id,
                            "channel": channel,
                            "orders": orders,
                            "units": units,
                            "revenue": _money(units * unit_price),
                        }
                    )
    return rows


def checksum(rows: list[dict[str, Any]]) -> str:
    total = sum((row["revenue"] for row in rows), Decimal("0"))
    return f"rows={len(rows)};revenue={total.quantize(_CENTS)}"


def ro_password() -> str:
    return os.environ.get("POSTGRES_MCP_RO_PASSWORD", DEFAULT_RO_PASSWORD)


def _sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


async def _seed(dsn: str, spec: ScenarioSpec, password: str) -> dict[str, Any]:
    rows = build_sales_rows(spec)
    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
        await conn.execute(f"CREATE SCHEMA {SCHEMA}")
        await conn.execute(
            f"""
            CREATE TABLE {SCHEMA}.products (
                product_id INTEGER PRIMARY KEY,
                name TEXT NOT NULL UNIQUE,
                category TEXT NOT NULL
            )
            """
        )
        await conn.execute(
            f"""
            CREATE TABLE {SCHEMA}.regions (
                region_id INTEGER PRIMARY KEY,
                name TEXT NOT NULL UNIQUE,
                country TEXT NOT NULL
            )
            """
        )
        await conn.execute(
            f"""
            CREATE TABLE {SCHEMA}.daily_sales (
                sale_date DATE NOT NULL,
                product_id INTEGER NOT NULL REFERENCES {SCHEMA}.products(product_id),
                region_id INTEGER NOT NULL REFERENCES {SCHEMA}.regions(region_id),
                channel TEXT NOT NULL,
                orders INTEGER NOT NULL CHECK (orders >= 0),
                units INTEGER NOT NULL CHECK (units >= 0),
                revenue NUMERIC(12,2) NOT NULL CHECK (revenue >= 0),
                PRIMARY KEY (sale_date, product_id, region_id, channel)
            )
            """
        )
        await conn.execute(f"CREATE INDEX ON {SCHEMA}.daily_sales (sale_date)")
        await conn.execute(f"CREATE INDEX ON {SCHEMA}.daily_sales (product_id, sale_date)")
        await conn.execute(f"CREATE INDEX ON {SCHEMA}.daily_sales (region_id, sale_date)")

        await conn.executemany(
            f"INSERT INTO {SCHEMA}.products (product_id, name, category) VALUES ($1, $2, $3)",
            [(pid, name, category) for pid, name, category, _, _ in PRODUCTS],
        )
        await conn.executemany(
            f"INSERT INTO {SCHEMA}.regions (region_id, name, country) VALUES ($1, $2, $3)",
            REGIONS,
        )
        await conn.executemany(
            f"""
            INSERT INTO {SCHEMA}.daily_sales
                (sale_date, product_id, region_id, channel, orders, units, revenue)
            VALUES ($1, $2, $3, $4, $5, $6, $7)
            """,
            [
                (
                    row["sale_date"], row["product_id"], row["region_id"], row["channel"],
                    row["orders"], row["units"], row["revenue"],
                )
                for row in rows
            ],
        )

        # Dedicated READ-ONLY role for the postgres-mcp server: SELECT grants only,
        # plus a session-level read-only guard.
        role = "postgres_mcp_ro"
        try:
            await conn.execute(f"CREATE ROLE {role} LOGIN")
        except DuplicateObjectError:
            pass
        await conn.execute(f"ALTER ROLE {role} WITH LOGIN PASSWORD {_sql_literal(password)}")
        await conn.execute(f"ALTER ROLE {role} SET default_transaction_read_only = on")
        await conn.execute(f"GRANT USAGE ON SCHEMA {SCHEMA} TO {role}")
        await conn.execute(f"GRANT SELECT ON ALL TABLES IN SCHEMA {SCHEMA} TO {role}")
        await conn.execute(
            f"ALTER DEFAULT PRIVILEGES IN SCHEMA {SCHEMA} GRANT SELECT ON TABLES TO {role}"
        )

        await conn.execute(
            f"""
            CREATE TABLE {SCHEMA}.scenario_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """
        )
        meta = {
            "seed_version": SEED_VERSION,
            "scenario_name": spec.name,
            "seed": str(spec.seed),
            "start_date": spec.start_date.isoformat(),
            "end_date": spec.end_date.isoformat(),
            "baseline_start": spec.baseline.start_date.isoformat(),
            "baseline_end": spec.baseline.end_date.isoformat(),
            "incident_start": spec.incident.start_date.isoformat(),
            "incident_end": spec.incident.end_date.isoformat(),
            "affected_channel": spec.incident.affected_channel,
            "checksum": checksum(rows),
        }
        await conn.executemany(
            f"INSERT INTO {SCHEMA}.scenario_meta (key, value) VALUES ($1, $2)",
            list(meta.items()),
        )
        return meta
    finally:
        await conn.close()


async def _fetch_meta(dsn: str) -> dict[str, str] | None:
    conn = await asyncpg.connect(dsn)
    try:
        exists = await conn.fetchval("SELECT to_regclass($1)", f"{SCHEMA}.scenario_meta")
        if exists is None:
            return None
        records = await conn.fetch(f"SELECT key, value FROM {SCHEMA}.scenario_meta")
        return {r["key"]: r["value"] for r in records}
    finally:
        await conn.close()


def seed_database(dsn: str = DEFAULT_ADMIN_DSN, spec: ScenarioSpec | None = None) -> dict[str, Any]:
    """(Re)create the sales_data schema with deterministic seed data. Idempotent."""
    return asyncio.run(_seed(dsn, spec or load_scenario(), ro_password()))


def fetch_meta(dsn: str = DEFAULT_ADMIN_DSN) -> dict[str, str] | None:
    """Return stored scenario metadata, or None when the database is unseeded."""
    return asyncio.run(_fetch_meta(dsn))


def ensure_seeded(dsn: str = DEFAULT_ADMIN_DSN, spec: ScenarioSpec | None = None) -> dict[str, Any]:
    """Seed only when the stored data is missing or does not match the spec (sync)."""
    return asyncio.run(ensure_seeded_async(dsn, spec))


async def ensure_seeded_async(dsn: str = DEFAULT_ADMIN_DSN, spec: ScenarioSpec | None = None) -> dict[str, Any]:
    """Async form of ensure_seeded — safe to call inside a running event loop."""
    spec = spec or load_scenario()
    meta = await _fetch_meta(dsn)
    expected = {"seed_version": SEED_VERSION, "scenario_name": spec.name, "seed": str(spec.seed)}
    if meta and all(meta.get(k) == v for k, v in expected.items()):
        return dict(meta, seeded_now=False)
    return dict(await _seed(dsn, spec, ro_password()), seeded_now=True)
