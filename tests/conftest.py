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
def pg_manager(ro_dsn: str):
    def _make(**kwargs) -> MCPClientManager:
        return MCPClientManager(
            "postgres-mcp",
            sys.executable,
            ["-m", "backend.mcp.servers.postgres_mcp"],
            **kwargs,
        )

    return _make
