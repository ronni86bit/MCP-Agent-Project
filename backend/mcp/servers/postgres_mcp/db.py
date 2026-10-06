"""asyncpg access for postgres-mcp.

The server connects ONLY with the dedicated read-only role (SELECT grants and
`default_transaction_read_only = on`, provisioned by scripts/seed_db.py). The
pool is created lazily on first use; a statement-level command timeout backs
the client-side call timeout.
"""

from __future__ import annotations

from typing import Any

import asyncpg

from backend.config import get_settings

DEFAULT_RO_DSN = "postgresql://postgres_mcp_ro:ro_dev_password@localhost:5433/ops_agent"

_pool: asyncpg.Pool | None = None


def dsn() -> str:
    return get_settings().postgres_mcp_dsn or DEFAULT_RO_DSN


async def _get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(
            dsn(), min_size=1, max_size=4, command_timeout=15
        )
    return _pool


async def fetch(sql: str, *args: Any) -> list[asyncpg.Record]:
    pool = await _get_pool()
    async with pool.acquire() as conn:
        return list(await conn.fetch(sql, *args))


async def fetchrow(sql: str, *args: Any) -> asyncpg.Record | None:
    pool = await _get_pool()
    async with pool.acquire() as conn:
        return await conn.fetchrow(sql, *args)
