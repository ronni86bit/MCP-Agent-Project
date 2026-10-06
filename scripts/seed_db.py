"""Seed the sales database from backend/mcp/fixtures/scenario.yaml.

    uv run python scripts/seed_db.py [--dsn URL]

Idempotent and deterministic: rebuilds the sales_data schema, provisions the
read-only `postgres_mcp_ro` role, and records scenario metadata for the
coherence tests. Uses OPS_ADMIN_DSN (or the dev default) — never the MCP
server's read-only DSN.
"""

import argparse
import os

from backend.mcp.fixtures.sales_seed import DEFAULT_ADMIN_DSN, seed_database


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dsn", default=os.environ.get("OPS_ADMIN_DSN", DEFAULT_ADMIN_DSN))
    args = parser.parse_args()

    meta = seed_database(args.dsn)
    print("Seeded sales_data schema:")
    for key, value in meta.items():
        print(f"  {key}: {value}")
    print("\nRead-only role postgres_mcp_ro is ready for the postgres-mcp server.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
