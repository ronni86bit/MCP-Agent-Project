"""Scenario-variant harness (Phase 9 Gate 4).

Provides one isolated environment per anti-causation case:

- a UNIQUE throwaway PostgreSQL database, seeded from the variant spec, whose
  exact name is tracked and dropped — after verifying ownership via
  `current_database()` — in a `finally` block. No age- or name-based sweeping
  exists anywhere in this module.
- MCP manager factories that receive the variant explicitly through their
  per-process environment (`OPS_SCENARIO_PATH` for monitoring/github,
  `POSTGRES_MCP_DSN` for postgres), so Monitoring, GitHub, and PostgreSQL all
  serve the SAME variant. In-process consumers (planner.dataset_context) are
  covered by `scoped_variant_env`, a restored-on-exit patch of the process
  environment strictly scoped to the case body.
- `assert_cross_store_coherence`: deterministic MCP round-trips proving all
  three stores tell the same story BEFORE any model runs. A mismatch is a
  setup/infrastructure failure by construction.
"""

from __future__ import annotations

import asyncio
import os
import sys
import uuid
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import asyncpg

from backend.config import get_settings
from backend.mcp.fixtures.loader import VARIANT_ENV_VAR, load_scenario
from backend.mcp.fixtures.sales_seed import seed_database

SCENARIO_DIR = Path(__file__).resolve().parents[2] / "tests" / "evaluation" / "scenarios"
_VARIANT_NAMES = ("deployment_without_incident", "incident_without_deployment", "no_anomaly")

# dev-default DSNs mirror tests/conftest.py; the environment always wins.
_DEFAULT_ADMIN_DSN = "postgresql://ops_admin:ops_admin_dev@localhost:5433/ops_agent"

# wide read window used by the coherence round-trips (mirrors the approval
# service's read args so tool contracts are exercised identically)
_READ_WINDOW = {"window_start": "2026-09-06", "window_end": "2026-10-05", "limit": 100}
# equal-length compare windows for the sales-coherence round-trip
_COMPARE_A = {"window_a_start": "2026-09-30", "window_a_end": "2026-10-02",
              "window_b_start": "2026-10-03", "window_b_end": "2026-10-05",
              "metric": "revenue"}


def variant_path(variant: str) -> Path:
    if variant not in _VARIANT_NAMES:
        raise ValueError(f"unknown variant {variant!r}; known: {_VARIANT_NAMES}")
    return SCENARIO_DIR / f"{variant}.yaml"


@contextmanager
def scoped_variant_env(variant: str):
    """Scope OPS_SCENARIO_PATH to the enclosed block; always restored (success,
    exception, or cancellation) via patch.dict. No other global state moves."""
    from unittest import mock

    with mock.patch.dict(os.environ, {VARIANT_ENV_VAR: str(variant_path(variant))}):
        yield


@dataclass(frozen=True)
class VariantContext:
    variant: str
    spec: Any                     # ScenarioSpec of the variant
    spec_path: Path
    eval_dsn: str
    db_name: str


def _admin_dsn() -> str:
    return get_settings().ops_admin_dsn or _DEFAULT_ADMIN_DSN


async def _create_eval_db(admin_dsn: str) -> tuple[str, str]:
    """Create one uniquely named database; returns (db_name, eval_dsn)."""
    db_name = f"ops_agent_eval_{uuid.uuid4().hex[:8]}"
    admin = await asyncpg.connect(admin_dsn, timeout=10)
    try:
        await admin.execute(f"CREATE DATABASE {db_name}")
    finally:
        await admin.close()
    return db_name, admin_dsn.rsplit("/", 1)[0] + f"/{db_name}"


async def _drop_eval_db(admin_dsn: str, db_name: str, eval_dsn: str) -> None:
    """Drop EXACTLY the database this case created — ownership is verified by
    connecting to the eval DSN and asserting `current_database()` matches the
    tracked name before the DROP is issued. Never drops anything else."""
    conn = await asyncpg.connect(eval_dsn, timeout=10)
    try:
        actual = await conn.fetchval("SELECT current_database()")
    finally:
        await conn.close()
    if actual != db_name:
        raise RuntimeError(
            f"ownership verification failed: eval DSN resolves to {actual!r}, "
            f"expected {db_name!r}; refusing to drop"
        )
    admin = await asyncpg.connect(admin_dsn, timeout=10)
    try:
        await admin.execute(f"DROP DATABASE {db_name} WITH (FORCE)")
    finally:
        await admin.close()


def variant_manager_factories(
    ctx: VariantContext, *, github_base_url: str | None = None
) -> dict[str, Callable[[], Any]]:
    """One factory per MCP server, each receiving the variant explicitly
    through ITS OWN process environment. The postgres server is pointed at
    this case's seeded database; monitoring/github at the variant spec.
    `github_base_url` optionally routes github-mcp at a local mock (hermetic
    tests); by default the real GitHub API is used, unauthenticated."""
    from backend.mcp.client_manager import MCPClientManager

    settings = get_settings()
    scenario_env = {VARIANT_ENV_VAR: str(ctx.spec_path)}
    postgres_env = {"POSTGRES_MCP_DSN": ctx.eval_dsn}
    github_env = {VARIANT_ENV_VAR: str(ctx.spec_path)}
    if github_base_url is not None:
        github_env["GITHUB_API_BASE_URL"] = github_base_url

    def _factory(name: str, module: str, env: dict[str, str]) -> Callable[[], Any]:
        def _make() -> MCPClientManager:
            return MCPClientManager(
                name, sys.executable, ["-m", module], env=env,
                call_timeout_seconds=settings.mcp_call_timeout_seconds,
            )
        return _make

    return {
        "postgres-mcp": _factory("postgres-mcp", "backend.mcp.servers.postgres_mcp", postgres_env),
        "monitoring-mcp": _factory("monitoring-mcp", "backend.mcp.servers.monitoring_mcp", scenario_env),
        "github-mcp": _factory("github-mcp", "backend.mcp.servers.github_mcp", github_env),
    }


def sys_executable() -> str:
    import sys

    return sys.executable


async def assert_cross_store_coherence(
    ctx: VariantContext, *, github_base_url: str | None = None
) -> tuple[bool, list[str]]:
    """Deterministic MCP round-trips proving Monitoring, GitHub, and
    PostgreSQL all serve the SAME variant. Purely a setup check: it runs
    BEFORE any model involvement and its verdict is infra evidence."""
    from backend.mcp.client_manager import MultiMCPManager

    failures: list[str] = []
    factories = variant_manager_factories(ctx, github_base_url=github_base_url)
    managers = MultiMCPManager(factories)
    try:
        async with managers:
            alerts_result = await managers.call_tool(
                "monitoring-mcp", "get_recent_alerts", dict(_READ_WINDOW))
            incidents_result = await managers.call_tool(
                "monitoring-mcp", "get_incidents", dict(_READ_WINDOW))
            deployments_result = await managers.call_tool(
                "github-mcp", "get_deployments", {"limit": 30})
            compare_result = await managers.call_tool(
                "postgres-mcp", "compare_sales", dict(_COMPARE_A))
    except Exception as exc:
        return False, [f"coherence round-trip failed: {type(exc).__name__}: {exc}"]

    def _payload(result: Any) -> dict:
        structured = getattr(result, "structured_content", None)
        return structured if isinstance(structured, dict) else {}

    alerts = _payload(alerts_result).get("alerts", [])
    incidents = _payload(incidents_result).get("incidents", [])
    deployments = _payload(deployments_result).get("deployments", [])
    compare = _payload(compare_result)

    story_alert = any(a.get("id") == "ALT-2071" for a in alerts)
    story_incident = any(i.get("id") == "INC-1042" for i in incidents)
    scenario_deployment = any(d.get("source") == "scenario-fixture" for d in deployments)

    if story_alert != (ctx.spec.monitoring.alert is not None):
        failures.append(f"monitoring alert presence {story_alert} != spec")
    if story_incident != (ctx.spec.monitoring.incident is not None):
        failures.append(f"monitoring incident presence {story_incident} != spec")
    if scenario_deployment != (ctx.spec.deployment is not None):
        failures.append(f"github scenario-deployment presence {scenario_deployment} != spec")

    delta = compare.get("delta_pct")
    if delta is None:
        failures.append("compare_sales returned no delta_pct")
    elif ctx.spec.monitoring.incident is not None and delta > -20.0:
        # threshold mirrored from tests/integration/test_storyline.py
        failures.append(f"declining variant shows delta_pct={delta} (expected <= -20)")
    elif ctx.spec.monitoring.incident is None and delta <= -10.0:
        failures.append(f"healthy variant shows delta_pct={delta} (expected ~0)")

    return (not failures), failures


@asynccontextmanager
async def variant_environment(variant: str):
    """Full per-case environment: unique seeded database + variant spec.
    Cleanup is `finally`-guaranteed and removes ONLY the tracked database."""
    admin_dsn = _admin_dsn()
    spec = load_scenario(variant_path(variant))  # explicit path: env-independent
    db_name, eval_dsn = await _create_eval_db(admin_dsn)
    try:
        await asyncio.to_thread(seed_database, eval_dsn, spec)
        ctx = VariantContext(variant=variant, spec=spec, spec_path=variant_path(variant),
                             eval_dsn=eval_dsn, db_name=db_name)
        yield ctx
    finally:
        await _drop_eval_db(admin_dsn, db_name, eval_dsn)
