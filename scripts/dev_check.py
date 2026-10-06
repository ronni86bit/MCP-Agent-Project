"""Phase gate: run the test suite, then the end-to-end demo.

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
    if run("tests", [sys.executable, "-m", "pytest", "-q"]) != 0:
        return 1
    if run("demo", [sys.executable, "scripts/toy_client_demo.py"]) != 0:
        return 1
    print("\nPHASE GATE PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
