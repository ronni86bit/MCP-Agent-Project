"""Shared fixtures for integration/evaluation tests (containerized Postgres).

When Postgres is unreachable these fixtures skip with instructions; the phase
gate starts the container before running tests.
"""

import asyncio
import os
import sys

import asyncpg
import pytest

from backend.mcp.client_manager import MCPClientManager

ADMIN_DSN = os.environ.get(
    "OPS_ADMIN_DSN", "postgresql://ops_admin:ops_admin_dev@localhost:5433/ops_agent"
)
RO_DSN = os.environ.get(
    "POSTGRES_MCP_DSN", "postgresql://postgres_mcp_ro:ro_dev_password@localhost:5433/ops_agent"
)


def _reachable(dsn: str) -> bool:
    async def ping() -> bool:
        try:
            conn = await asyncpg.connect(dsn, timeout=3)
            await conn.close()
            return True
        except Exception:
            return False

    return asyncio.run(ping())


@pytest.fixture(scope="session")
def admin_dsn() -> str:
    if not _reachable(ADMIN_DSN):
        pytest.skip("PostgreSQL unavailable — start it with: docker compose up -d postgres")
    return ADMIN_DSN


@pytest.fixture(scope="session")
def ro_dsn(admin_dsn: str) -> str:
    from backend.mcp.fixtures.sales_seed import ensure_seeded

    ensure_seeded(admin_dsn)
    return RO_DSN


@pytest.fixture
def monitoring_manager(ro_dsn: str):
    def _make(**kwargs) -> MCPClientManager:
        return MCPClientManager(
            "monitoring-mcp",
            sys.executable,
            ["-m", "backend.mcp.servers.monitoring_mcp"],
            **kwargs,
        )

    return _make


@pytest.fixture
def pg_manager(ro_dsn: str):
    def _make(**kwargs) -> MCPClientManager:
        return MCPClientManager(
            "postgres-mcp",
            sys.executable,
            ["-m", "backend.mcp.servers.postgres_mcp"],
            **kwargs,
        )

    return _make


# -- approval lifecycle fixtures (Phase 7, Gate 2) -----------------------------

APPROVALS_TEST_DB = "ops_agent_approvals_g2_test"


@pytest.fixture(scope="module")
def approvals_state_dsn(admin_dsn: str) -> str:
    """Dedicated, freshly recreated state database for approval-lifecycle tests."""
    async def recreate() -> str:
        admin = await asyncpg.connect(admin_dsn)
        try:
            await admin.execute(f"DROP DATABASE IF EXISTS {APPROVALS_TEST_DB} WITH (FORCE)")
            await admin.execute(f"CREATE DATABASE {APPROVALS_TEST_DB}")
        finally:
            await admin.close()
        return admin_dsn.rsplit("/", 1)[0] + f"/{APPROVALS_TEST_DB}"

    return asyncio.run(recreate())


@pytest.fixture
def state_test_store(approvals_state_dsn: str):
    from backend.agent.persistence.store import InvestigationStore

    """Fresh InvestigationStore per test on the recreated approvals test DB."""
    def _factory() -> InvestigationStore:
        return InvestigationStore(dsn=approvals_state_dsn)

    return _factory
