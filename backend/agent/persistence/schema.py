"""Versioned schema initialization for the agent_state schema.

Business sales data lives in `sales_data`; investigation state lives here —
logically separated per the repository conventions. Migrations are plain SQL
applied in order and recorded in agent_state.schema_migrations. The approvals
table exists from v1 so later approval-gated actions have a home without any
write tooling being implemented now.
"""

from __future__ import annotations

import asyncpg

SCHEMA_NAME = "agent_state"
SCHEMA_VERSION = 2

_MIGRATION_V1 = [
    f"""
    CREATE TABLE IF NOT EXISTS {SCHEMA_NAME}.investigations (
        investigation_id TEXT PRIMARY KEY,
        request_id TEXT NOT NULL,
        question TEXT NOT NULL,
        status TEXT NOT NULL,
        stage TEXT NOT NULL DEFAULT 'created',
        understanding JSONB,
        failure_reason TEXT,
        warnings JSONB NOT NULL DEFAULT '[]'::jsonb,
        states_visited JSONB NOT NULL DEFAULT '[]'::jsonb,
        llm_calls INTEGER NOT NULL DEFAULT 0,
        created_at TIMESTAMPTZ NOT NULL,
        updated_at TIMESTAMPTZ NOT NULL,
        finished_at TIMESTAMPTZ
    )
    """,
    f"""
    CREATE TABLE IF NOT EXISTS {SCHEMA_NAME}.plans (
        investigation_id TEXT NOT NULL REFERENCES {SCHEMA_NAME}.investigations(investigation_id),
        version INTEGER NOT NULL,
        goal TEXT NOT NULL,
        steps JSONB NOT NULL,
        expected_evidence JSONB NOT NULL DEFAULT '[]'::jsonb,
        completion_criteria TEXT NOT NULL,
        revision_reason TEXT NOT NULL DEFAULT '',
        superseded BOOLEAN NOT NULL DEFAULT FALSE,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        PRIMARY KEY (investigation_id, version)
    )
    """,
    f"""
    CREATE TABLE IF NOT EXISTS {SCHEMA_NAME}.steps (
        investigation_id TEXT NOT NULL REFERENCES {SCHEMA_NAME}.investigations(investigation_id),
        step_index INTEGER NOT NULL,
        plan_version INTEGER NOT NULL DEFAULT 1,
        plan_step_id INTEGER,
        action TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT 'pending',
        evidence_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
        started_at TIMESTAMPTZ,
        finished_at TIMESTAMPTZ,
        PRIMARY KEY (investigation_id, step_index)
    )
    """,
    f"""
    CREATE TABLE IF NOT EXISTS {SCHEMA_NAME}.tool_calls (
        investigation_id TEXT NOT NULL REFERENCES {SCHEMA_NAME}.investigations(investigation_id),
        call_index INTEGER NOT NULL,
        step_index INTEGER,
        server TEXT NOT NULL DEFAULT '',
        tool TEXT NOT NULL,
        arguments JSONB NOT NULL DEFAULT '{{}}'::jsonb,
        attempt INTEGER NOT NULL DEFAULT 1,
        status TEXT NOT NULL DEFAULT 'running',
        error_class TEXT,
        error TEXT,
        duration_ms DOUBLE PRECISION NOT NULL DEFAULT 0,
        evidence_id TEXT,
        at TIMESTAMPTZ NOT NULL DEFAULT now(),
        PRIMARY KEY (investigation_id, call_index)
    )
    """,
    f"""
    CREATE TABLE IF NOT EXISTS {SCHEMA_NAME}.evidence (
        investigation_id TEXT NOT NULL REFERENCES {SCHEMA_NAME}.investigations(investigation_id),
        evidence_id TEXT NOT NULL,
        server TEXT NOT NULL,
        tool_name TEXT NOT NULL,
        step INTEGER NOT NULL,
        arguments JSONB NOT NULL DEFAULT '{{}}'::jsonb,
        summary JSONB NOT NULL DEFAULT '{{}}'::jsonb,
        at TIMESTAMPTZ NOT NULL DEFAULT now(),
        PRIMARY KEY (investigation_id, evidence_id)
    )
    """,
    f"""
    CREATE TABLE IF NOT EXISTS {SCHEMA_NAME}.reports (
        investigation_id TEXT PRIMARY KEY REFERENCES {SCHEMA_NAME}.investigations(investigation_id),
        report JSONB NOT NULL,
        verification JSONB NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    f"""
    CREATE TABLE IF NOT EXISTS {SCHEMA_NAME}.investigation_events (
        event_id BIGSERIAL PRIMARY KEY,
        investigation_id TEXT NOT NULL REFERENCES {SCHEMA_NAME}.investigations(investigation_id),
        event TEXT NOT NULL,
        payload JSONB NOT NULL DEFAULT '{{}}'::jsonb,
        at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    # Placeholder for the Phase 7 approval flow: the state model exists, no
    # write tool is implemented or advertised.
    f"""
    CREATE TABLE IF NOT EXISTS {SCHEMA_NAME}.approvals (
        approval_id TEXT PRIMARY KEY,
        investigation_id TEXT NOT NULL REFERENCES {SCHEMA_NAME}.investigations(investigation_id),
        tool TEXT NOT NULL,
        arguments JSONB NOT NULL DEFAULT '{{}}'::jsonb,
        status TEXT NOT NULL DEFAULT 'proposed',
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        decided_at TIMESTAMPTZ
    )
    """,
    f"CREATE INDEX IF NOT EXISTS idx_evidence_inv ON {SCHEMA_NAME}.evidence (investigation_id)",
    f"CREATE INDEX IF NOT EXISTS idx_events_inv ON {SCHEMA_NAME}.investigation_events (investigation_id)",
]

MIGRATIONS: dict[int, list[str]] = {1: _MIGRATION_V1}

# v2: approval lifecycle columns (Phase 7, `acknowledge_alert`). Additive only —
# existing v1 rows keep their values and gain NULL/defaulted new columns.
_MIGRATION_V2 = [
    f"ALTER TABLE {SCHEMA_NAME}.approvals ADD COLUMN IF NOT EXISTS proposal JSONB NOT NULL DEFAULT '{{}}'::jsonb",
    f"ALTER TABLE {SCHEMA_NAME}.approvals ADD COLUMN IF NOT EXISTS args_hash TEXT",
    f"ALTER TABLE {SCHEMA_NAME}.approvals ADD COLUMN IF NOT EXISTS proposal_version INTEGER",
    f"ALTER TABLE {SCHEMA_NAME}.approvals ADD COLUMN IF NOT EXISTS expires_at TIMESTAMPTZ",
    f"ALTER TABLE {SCHEMA_NAME}.approvals ADD COLUMN IF NOT EXISTS decided_by TEXT",
    f"ALTER TABLE {SCHEMA_NAME}.approvals ADD COLUMN IF NOT EXISTS decision_comment TEXT",
    f"ALTER TABLE {SCHEMA_NAME}.approvals ADD COLUMN IF NOT EXISTS executed_by TEXT",
    f"ALTER TABLE {SCHEMA_NAME}.approvals ADD COLUMN IF NOT EXISTS dispatched_at TIMESTAMPTZ",
    f"ALTER TABLE {SCHEMA_NAME}.approvals ADD COLUMN IF NOT EXISTS executed_at TIMESTAMPTZ",
    f"ALTER TABLE {SCHEMA_NAME}.approvals ADD COLUMN IF NOT EXISTS execution_result JSONB NOT NULL DEFAULT '{{}}'::jsonb",
    f"ALTER TABLE {SCHEMA_NAME}.approvals ADD COLUMN IF NOT EXISTS verification JSONB",
    f"CREATE INDEX IF NOT EXISTS idx_approvals_status ON {SCHEMA_NAME}.approvals (status)",
]

MIGRATIONS: dict[int, list[str]] = {1: _MIGRATION_V1, 2: _MIGRATION_V2}


async def ensure_schema(conn: asyncpg.Connection) -> int:
    """Apply pending migrations; returns the resulting schema version."""
    await conn.execute(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA_NAME}")
    await conn.execute(
        f"CREATE TABLE IF NOT EXISTS {SCHEMA_NAME}.schema_migrations "
        "(version INTEGER PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
    )
    applied = {row["version"] for row in await conn.fetch(
        f"SELECT version FROM {SCHEMA_NAME}.schema_migrations"
    )}
    for version in sorted(MIGRATIONS):
        if version in applied:
            continue
        for statement in MIGRATIONS[version]:
            await conn.execute(statement)
        await conn.execute(
            f"INSERT INTO {SCHEMA_NAME}.schema_migrations (version) VALUES ($1) "
            "ON CONFLICT (version) DO NOTHING",
            version,
        )
    return SCHEMA_VERSION
