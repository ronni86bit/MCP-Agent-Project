"""Development phase gate: tests + Phase 1 demo + Phase 2 postgres demo.

    uv run python scripts/dev_check.py
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
        ("tests", [sys.executable, "-m", "pytest", "-q"]),
        ("phase1-demo", [sys.executable, "scripts/toy_client_demo.py"]),
        ("phase2-postgres-demo", [sys.executable, "scripts/postgres_demo.py"]),
    ]
    for label, cmd in checks:
        if run(label, cmd) != 0:
            return 1
    print("\nPHASE GATE PASSED (Phases 1-2)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
