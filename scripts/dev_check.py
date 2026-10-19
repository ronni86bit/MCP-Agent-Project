"""Development phase gate: tests + demos for Phases 1-8.

    uv run python scripts/dev_check.py

Model-dependent checks (smoke probes + canonical investigation) run as explicit
gate steps so a missing Ollama/model fails the gate clearly instead of silently
skipping.
"""

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def run(label: str, cmd: list[str]) -> int:
    print(f"\n===== {label}: {' '.join(cmd)} =====")
    result = subprocess.run(cmd, cwd=ROOT)
    if result.returncode != 0:
        print(f"FAILED: {label} (exit {result.returncode})")
    else:
        print(f"OK: {label}")
    return result.returncode


def main() -> int:
    checks = [
        ("tests", [sys.executable, "-m", "pytest", "tests/unit", "tests/integration", "-q"]),
        ("phase1-demo", [sys.executable, "scripts/toy_client_demo.py"]),
        ("phase2-postgres-demo", [sys.executable, "scripts/postgres_demo.py"]),
        ("model-smoke", [sys.executable, "scripts/model_smoke.py"]),
        ("phase5-state-demo", [sys.executable, "scripts/persistence_demo.py"]),
        ("agent-demo", [sys.executable, "scripts/agent_demo.py"]),
    ]
    for label, cmd in checks:
        if run(label, cmd) != 0:
            return 1
    print("\nPHASE GATE PASSED (Phases 1-8)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
