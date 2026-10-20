"""Phase 9 Gate 2: benchmark runner persistence and gate wiring.

Covers the agreed runner requirements: valid manifest + JSONL rows, complete
case coverage, run isolation, collision abort, interruption sidecar, atomic
last-written manifest, successful-run filtering, schema round-trip with a
derived `passed`, and dev_check inclusion of the deterministic evaluation
step. The full-suite test is PostgreSQL-gated (D-SEL/D-APPROVE) like the rest
of the integration suite; the persistence tests are pure.
"""

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import scripts.run_benchmark as runner  # noqa: E402
from backend.evaluation.schema import CaseResult, CheckResult  # noqa: E402


def _reach_ping() -> bool:
    import asyncio

    import asyncpg

    from backend.config import get_settings

    dsn = get_settings().ops_admin_dsn or runner._DEFAULT_ADMIN_DSN

    async def ping() -> bool:
        try:
            conn = await asyncpg.connect(dsn, timeout=3)
            await conn.close()
            return True
        except Exception:
            return False

    return asyncio.run(ping())


def _ok_case(case_id: str = "FAKE-OK") -> CaseResult:
    return CaseResult(case_id=case_id, checks=[CheckResult(name="always", passed=True)])


async def _ok_runner() -> CaseResult:
    return _ok_case()


async def _boom_runner() -> CaseResult:
    raise RuntimeError("case exploded")


# -- requirement 1 + 2: full suite -> valid manifest + complete coverage --------


@pytest.mark.skipif(not _reach_ping(), reason="PostgreSQL unavailable")
async def test_full_deterministic_suite_writes_valid_manifest_and_complete_rows(
    tmp_path: Path,
):
    args = argparse_namespace(tmp_path, "gate2-full")
    exit_code = await runner.main_async(args)
    assert exit_code == 0

    run_dirs = list(tmp_path.iterdir())
    assert len(run_dirs) == 1
    run_dir = run_dirs[0]

    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["schema_version"] == 1
    assert manifest["suite"] == "deterministic"
    assert manifest["scenario"]["seed"] == 42
    assert manifest["code_commit"]
    assert not (run_dir / "run_incomplete.json").exists()
    assert not list(run_dir.glob("*.tmp"))

    specs = runner.build_case_specs()
    rows = [json.loads(line) for line in
            (run_dir / "rows.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(rows) == len(specs) == manifest["summary"]["cases"]
    # every registered case is represented exactly as scheduled, in order
    assert [r["seq"] for r in rows] == list(range(1, len(specs) + 1))
    assert [r["case_id"] for r in rows] == [s.case_id for s in specs]
    # manifest case metadata agrees with the rows (including derived `passed`)
    for row, case_meta in zip(rows, manifest["cases"]):
        case = CaseResult.model_validate(row)
        assert case.passed == case_meta["passed"]
        assert all(c.passed for c in case.checks) == case.passed


def argparse_namespace(tmp_path: Path, label: str):
    import argparse

    return argparse.Namespace(suite="deterministic", label=label, out=str(tmp_path))


# -- requirement 3: two runs -> separate preserved directories -------------------


async def test_two_runs_create_separate_preserved_directories(tmp_path: Path):
    specs = [runner.CaseSpec("FAKE-OK", _ok_runner)]
    for label in ("a", "b"):
        run_dir = runner.build_run_dir(tmp_path, label)
        results = await runner.run_suite(run_dir, specs)
        runner.write_manifest(run_dir, runner._build_manifest(
            run_dir, label, runner._utc(), results))
    dirs = sorted(p.name for p in tmp_path.iterdir())
    assert len(dirs) == 2
    for d in dirs:
        manifest = runner.read_manifest(tmp_path / d)
        assert manifest is not None and manifest["label"] == d.rsplit("_", 1)[1]
        assert len((tmp_path / d / "rows.jsonl").read_text().strip().splitlines()) == 1


# -- requirement 4: collision aborts before touching anything ---------------------


async def test_directory_collision_aborts_without_modifying_existing_run(tmp_path: Path):
    run_dir = runner.build_run_dir(tmp_path, "same")
    sentinel = run_dir / "rows.jsonl"
    sentinel.write_text('{"seq": 1}\n', encoding="utf-8")
    before = sentinel.read_text(encoding="utf-8")

    with pytest.raises(FileExistsError):
        runner.build_run_dir(tmp_path, "same")

    assert sentinel.read_text(encoding="utf-8") == before
    assert list(run_dir.iterdir()) == [sentinel]  # nothing added either


# -- requirement 5 + 6: interruption sidecar, atomic last-written manifest --------


async def test_handled_interruption_preserves_rows_writes_sidecar_no_manifest(
    tmp_path: Path, monkeypatch,
):
    async def _interrupt() -> CaseResult:
        raise KeyboardInterrupt()

    specs = [runner.CaseSpec("FAKE-OK", _ok_runner), runner.CaseSpec("FAKE-STOP", _interrupt)]
    run_dir = runner.build_run_dir(tmp_path, "interrupted")

    with pytest.raises(KeyboardInterrupt):
        await runner.run_suite(run_dir, specs)

    rows = (run_dir / "rows.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert len(rows) == 1  # the completed case survived
    sidecar = json.loads((run_dir / "run_incomplete.json").read_text(encoding="utf-8"))
    assert sidecar["status"] == "incomplete"
    assert sidecar["completed_seq"] == [1]
    assert "KeyboardInterrupt" in sidecar["reason"]
    assert not (run_dir / "manifest.json").exists()


async def test_manifest_written_atomically_and_only_once_on_success(
    tmp_path: Path, monkeypatch,
):
    calls: list[tuple[str, str]] = []
    real_replace = runner.os.replace

    def spy(src, dst):
        calls.append((str(src), str(dst)))
        return real_replace(src, dst)

    monkeypatch.setattr(runner.os, "replace", spy)

    specs = [runner.CaseSpec("FAKE-FAIL-CHECKS", _fail_checks_runner)]
    run_dir = runner.build_run_dir(tmp_path, "atomic")
    results = await runner.run_suite(run_dir, specs)
    runner.write_manifest(run_dir, runner._build_manifest(
        run_dir, "atomic", runner._utc(), results))

    # a case with FAILED checks still completed: the run manifests (one call)
    assert len(calls) == 1
    src, dst = calls[0]
    assert dst.endswith("manifest.json")
    assert src.endswith("manifest.json.tmp")
    assert not Path(src).exists()  # no tmp leftovers


async def _fail_checks_runner() -> CaseResult:
    return CaseResult(case_id="FAKE-FAIL-CHECKS",
                      checks=[CheckResult(name="nope", passed=False, detail="x")])


async def test_case_runner_exception_becomes_explicit_failed_row(tmp_path: Path):
    specs = [runner.CaseSpec("FAKE-BOOM", _boom_runner)]
    run_dir = runner.build_run_dir(tmp_path, "boom")
    results = await runner.run_suite(run_dir, specs)
    assert len(results) == 1  # never silently omitted
    seq, case = results[0]
    assert case.passed is False
    assert case.error_class == "RuntimeError"
    assert not (run_dir / "run_incomplete.json").exists()  # runner-level: fine


# -- requirement 7: manifest-less runs excluded from successful handling ----------


def test_successful_run_filter_excludes_incomplete_artifacts(tmp_path: Path):
    good = runner.build_run_dir(tmp_path, "good")
    runner.write_manifest(good, {"schema_version": 1, "label": "good"})

    sidecar_only = runner.build_run_dir(tmp_path, "interrupted")
    runner.write_incomplete(sidecar_only, [1], "KeyboardInterrupt")

    empty = runner.build_run_dir(tmp_path, "hardkill")

    corrupted = runner.build_run_dir(tmp_path, "corrupt")
    (corrupted / "manifest.json").write_text("{not json", encoding="utf-8")

    wrong_schema = runner.build_run_dir(tmp_path, "wrongschema")
    runner.write_manifest(wrong_schema, {"schema_version": 99})

    successful = {p.name for p in runner.iter_successful_runs(tmp_path)}
    assert successful == {good.name}


# -- requirement 8: derived `passed` survives round-trip ---------------------------


def test_schema_round_trip_keeps_passed_derived():
    failing = CaseResult(case_id="X", checks=[CheckResult(name="a", passed=False)])
    payload = failing.model_dump(mode="json")
    assert payload["passed"] is False
    revived = CaseResult.model_validate(payload)
    assert revived.passed is False

    # a caller cannot forge `passed`: it is recomputed from checks
    forged = dict(payload, passed=True)
    assert CaseResult.model_validate(forged).passed is False

    passing = CaseResult(case_id="Y", checks=[CheckResult(name="a", passed=True)])
    assert CaseResult.model_validate(passing.model_dump(mode="json")).passed is True


# -- requirement 9: dev_check wiring ------------------------------------------------


def test_dev_check_includes_deterministic_evaluation_and_phase_1_9_label():
    """Gate 5 flipped the label once all Phase 9 acceptance criteria passed;
    this now pins the NEW invariant (and guards against a silent revert)."""
    source = (ROOT / "scripts" / "dev_check.py").read_text(encoding="utf-8")
    assert '"tests/evaluation", "-m", "not ollama"' in source
    assert "PHASE GATE PASSED (Phases 1-9)" in source
    assert "PHASE GATE PASSED (Phases 1-8)" not in source

    collected = subprocess_collect()
    assert "test_deterministic_runner.py" in collected
    assert "test_model_smoke" not in collected
    assert "test_canonical_smoke" not in collected


def subprocess_collect() -> str:
    import subprocess

    result = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/evaluation", "-m", "not ollama",
         "--collect-only", "-q"],
        cwd=ROOT, capture_output=True, text=True, timeout=120,
    )
    return result.stdout
