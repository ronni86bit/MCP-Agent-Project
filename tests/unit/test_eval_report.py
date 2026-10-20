"""Phase 9 Gate 5: aggregation semantics (pure, no runs executed).

Builds synthetic run directories (manifests + rows.jsonl) and verifies:
successful-run-only aggregation, incomplete-run reporting and exclusion,
strict vs infra-excluded pass rates, nearest-rank p50/p95, token aggregation
with missing-usage accounting, revision metadata exposure, and rendered
markdown. Malformed manifests must raise, never silently aggregate.
"""

import json
from pathlib import Path

import pytest

from backend.evaluation.report import (
    aggregate_run,
    aggregate_runs,
    describe_incomplete,
    nearest_rank,
    read_manifest_strict,
    render_markdown,
)


def _write_run(
    out: Path, name: str, *, label: str = "run", suite: str = "model",
    commit: str | None = "abc1234567890", dirty: bool = False,
    rows: list[dict] | None = None, manifest_extra: dict | None = None,
    manifest: dict | None = None,
) -> Path:
    run_dir = out / name
    run_dir.mkdir(parents=True)
    if manifest is None:
        manifest = {
            "schema_version": 1, "run_id": f"bench-{name}", "label": label,
            "suite": suite, "kind": suite,
            "started_utc": "2026-10-20T00:00:00Z", "finished_utc": "2026-10-20T01:00:00Z",
            "code_commit": commit, "code_dirty": dirty,
            "scenario": {"name": "checkout_incident_v1", "seed": 42, "source_path": "x"},
            "model": {"kind": "real", "name": "qwen3:4b-instruct"},
        }
        manifest.update(manifest_extra or {})
    (run_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=1), encoding="utf-8")
    if rows is not None:
        lines = [json.dumps(row, ensure_ascii=False) for row in rows]
        (run_dir / "rows.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return run_dir


def _row(seq: int, case_id: str, *, passed: bool, latency_ms: float,
         failure_layer: str = "none", prompt_tokens=None, completion_tokens=None,
         include_totals: bool = True) -> dict:
    totals = None
    if include_totals:
        totals = {"calls": 1, "llm_ms": latency_ms,
                  "prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens,
                  "usage_missing_calls": 1 if prompt_tokens is None else 0}
    return {
        "schema_version": 1, "seq": seq, "repeat_index": 0, "case_id": case_id,
        "suite": "model", "kind": "model", "input": {}, "expected": {}, "observed":
        {"status": "complete", "totals": totals},
        "checks": [{"name": "c", "passed": passed, "detail": ""}],
        "error_class": None, "latency_ms": latency_ms, "diagnostics": {},
        "failure_layer": failure_layer, "failure_diagnostics": {},
        "passed": passed,
    }


# -- nearest-rank percentiles ---------------------------------------------------


def test_nearest_rank_matches_the_approved_method():
    assert nearest_rank([], 50) is None
    assert nearest_rank([5.0], 50) == 5.0
    assert nearest_rank([1.0, 2.0, 3.0, 4.0], 50) == 2.0    # rank ceil(0.5*4)=2
    assert nearest_rank([1.0, 2.0, 3.0, 4.0], 95) == 4.0    # rank ceil(0.95*4)=4
    assert nearest_rank(list(range(1, 101)), 50) == 50.0    # rank 50
    assert nearest_rank(list(range(1, 101)), 95) == 95.0    # rank 95
    assert nearest_rank(list(range(1, 101)), 100) == 100.0  # clamped to n


# -- aggregation semantics --------------------------------------------------------


def test_aggregate_run_computes_strict_and_infra_excluded_pass_rates(tmp_path):
    rows = [
        _row(1, "A", passed=True, latency_ms=100.0, prompt_tokens=10, completion_tokens=5),
        _row(2, "B", passed=True, latency_ms=200.0, prompt_tokens=20, completion_tokens=8),
        _row(3, "C", passed=False, latency_ms=400.0, failure_layer="infra"),
        _row(4, "D", passed=False, latency_ms=800.0, failure_layer="reasoning"),
    ]
    run_dir = _write_run(tmp_path, "r1", rows=rows)
    summary = aggregate_run(run_dir)

    assert summary.cases == 4 and summary.passed == 2 and summary.failed == 2
    assert summary.pass_rate == pytest.approx(0.5)
    assert summary.infra_failures == 1
    # excluding the infra failure: 2 passed / 3 comparable
    assert summary.pass_rate_excluding_infra == pytest.approx(2 / 3)
    assert summary.failure_layers == {"none": 2, "infra": 1, "reasoning": 1}
    assert summary.latency_ms_p50 == 200.0   # rank 2 of 4
    assert summary.latency_ms_p95 == 800.0   # rank 4 of 4
    assert summary.prompt_tokens == 30 and summary.completion_tokens == 13
    # the two failed rows recorded no token usage: counted, not zero-filled
    assert summary.usage_missing_rows == 2
    assert summary.code_commit == "abc1234567890" and summary.code_dirty is False


def test_all_infra_failures_make_excluded_rate_none_not_zero(tmp_path):
    rows = [_row(1, "A", passed=False, latency_ms=10.0, failure_layer="infra")]
    run_dir = _write_run(tmp_path, "infra-only", rows=rows)
    summary = aggregate_run(run_dir)
    assert summary.pass_rate == 0.0
    assert summary.pass_rate_excluding_infra is None  # empty comparable set


def test_missing_token_usage_is_counted_never_zero_filled(tmp_path):
    rows = [
        _row(1, "A", passed=True, latency_ms=10.0),                      # no totals at all
        _row(2, "B", passed=True, latency_ms=20.0, prompt_tokens=None,
             completion_tokens=None),                                    # totals, no usage
        _row(3, "C", passed=True, latency_ms=30.0, prompt_tokens=7,
             completion_tokens=2),
    ]
    run_dir = _write_run(tmp_path, "usage", rows=rows)
    summary = aggregate_run(run_dir)
    assert summary.prompt_tokens == 7 and summary.completion_tokens == 2
    assert summary.usage_missing_rows == 2


def test_per_case_repeats_group_by_case_id(tmp_path):
    rows = [
        _row(1, "M-SEL", passed=True, latency_ms=100.0),
        _row(2, "M-SEL", passed=False, latency_ms=300.0, failure_layer="reasoning"),
        _row(3, "M-CANON", passed=True, latency_ms=500.0),
    ]
    run_dir = _write_run(tmp_path, "repeats", rows=rows)
    summary = aggregate_run(run_dir)
    by_id = {c.case_id: c for c in summary.per_case}
    assert by_id["M-SEL"].repeats == 2 and by_id["M-SEL"].passed_repeats == 1
    assert by_id["M-SEL"].failure_layers == {"none": 1, "reasoning": 1}
    assert by_id["M-CANON"].repeats == 1


# -- incomplete-run handling --------------------------------------------------------


def test_aggregate_runs_reports_but_excludes_incomplete_runs(tmp_path):
    _write_run(tmp_path, "good", rows=[_row(1, "A", passed=True, latency_ms=10.0)])

    interrupted = tmp_path / "interrupted"
    interrupted.mkdir()
    (interrupted / "run_incomplete.json").write_text(
        json.dumps({"status": "incomplete", "completed_seq": [1],
                    "reason": "KeyboardInterrupt"}), encoding="utf-8")

    (tmp_path / "hardkill").mkdir()  # no manifest, no sidecar

    summaries, incompletes = aggregate_runs(tmp_path)
    assert [s.label for s in summaries] == ["run"]
    assert len(incompletes) == 2
    reasons = {i.run_dir: i.reason for i in incompletes}
    assert any("KeyboardInterrupt" in r for r in reasons.values())
    assert any("incomplete-assumed" in r for r in reasons.values())


def test_corrupted_manifest_raises_instead_of_silent_success(tmp_path):
    bad = tmp_path / "corrupt"
    bad.mkdir()
    (bad / "manifest.json").write_text("{not json", encoding="utf-8")
    with pytest.raises((json.JSONDecodeError, ValueError)):
        aggregate_run(bad)
    with pytest.raises((json.JSONDecodeError, ValueError)):
        aggregate_runs(tmp_path)


def test_wrong_schema_manifest_is_rejected(tmp_path):
    _write_run(tmp_path, "v99", manifest={"schema_version": 99, "run_id": "x"})
    with pytest.raises(ValueError, match="schema v1"):
        aggregate_run(tmp_path / "v99")


def test_empty_run_dir_aggregates_to_zero_cases(tmp_path):
    run_dir = _write_run(tmp_path, "empty", rows=[])
    summary = aggregate_run(run_dir)
    assert summary.cases == 0
    assert summary.pass_rate is None and summary.pass_rate_excluding_infra is None
    assert summary.latency_ms_p50 is None


# -- revision metadata and rendering -------------------------------------------------


def test_revision_metadata_exposes_clean_vs_dirty(tmp_path):
    _write_run(tmp_path, "clean", label="clean", commit="aaa111", dirty=False,
               rows=[_row(1, "A", passed=True, latency_ms=1.0)])
    _write_run(tmp_path, "dirty", label="dirty", commit="bbb222", dirty=True,
               rows=[_row(1, "A", passed=True, latency_ms=1.0)])
    summaries, _ = aggregate_runs(tmp_path)
    states = {(s.label, s.code_commit, s.code_dirty) for s in summaries}
    assert ("clean", "aaa111", False) in states
    assert ("dirty", "bbb222", True) in states
    note = render_markdown(summaries, [])
    assert "MIXED revision states" in note  # incomparable runs are identifiable


def test_render_markdown_includes_incomplete_section_and_tables(tmp_path):
    summaries, incompletes = aggregate_runs(tmp_path)
    text = render_markdown(summaries, incompletes)
    assert "No successful runs to aggregate." in text

    _write_run(tmp_path, "s1", rows=[
        _row(1, "M-SEL", passed=True, latency_ms=10.0, prompt_tokens=3,
             completion_tokens=1)])
    summaries, incompletes = aggregate_runs(tmp_path)
    text = render_markdown(summaries, incompletes)
    assert "100.0%" in text and "M-SEL" in text and "Per-case stability" in text


def test_read_manifest_strict_missing_file_raises(tmp_path):
    with pytest.raises(ValueError, match="no manifest"):
        read_manifest_strict(tmp_path)
    assert describe_incomplete(tmp_path).reason.startswith("incomplete-assumed")
