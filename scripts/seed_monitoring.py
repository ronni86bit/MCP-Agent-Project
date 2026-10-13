"""Write deterministic monitoring fixtures as JSON (for inspection/tests).

    uv run python scripts/seed_monitoring.py

The monitoring-mcp server regenerates identical data in memory from
scenario.yaml; this script materializes it under backend/mcp/fixtures/monitoring/
so the fixture set is reviewable and diffable.
"""

import json
from pathlib import Path

from backend.mcp.fixtures.monitoring_seed import build_monitoring_fixtures
from backend.mcp.fixtures.loader import load_scenario

OUTPUT_DIR = Path(__file__).resolve().parents[1] / "backend" / "mcp" / "fixtures" / "monitoring"


def main() -> int:
    spec = load_scenario()
    fixtures = build_monitoring_fixtures(spec)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "scenario": spec.name,
        "services": fixtures.services,
        "series": fixtures.series,
        "alerts": fixtures.alerts,
        "incidents": fixtures.incidents,
    }
    out = OUTPUT_DIR / "fixtures.json"
    out.write_text(json.dumps(payload, indent=1), encoding="utf-8")
    points = sum(len(v) for v in fixtures.series.values())
    print(f"Wrote {out} ({points} metric points, {len(fixtures.alerts)} alerts, "
          f"{len(fixtures.incidents)} incidents)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
