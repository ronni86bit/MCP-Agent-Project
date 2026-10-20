"""Benchmark aggregation and summaries (Phase 9 Gate 5).

Aggregates ONLY successful runs: directories whose `manifest.json` exists and
parses as schema-v1 (the same rule as `scripts.run_benchmark.iter_successful_runs`).
Incomplete runs — sidecar-only, empty, or corrupted — are reported separately
and excluded from every statistic.

Semantics:
- strict pass_rate = passed rows / completed rows;
- pass_rate_excluding_infra = passed rows / (completed rows - infra failures):
  infrastructure noise is visible but never silently folded into the model
  score; denominator 0 -> None;
- latency p50/p95 over row `latency_ms` by the NEAREST-RANK method
  (sorted ascending, rank = ceil(p/100 * n), 1-indexed);
- token sums over rows that reported usage; rows lacking usage data are
  counted (`usage_missing_rows`), never zero-filled;
- revision metadata (`code_commit` + `code_dirty`) is surfaced per run: runs
  with differing revision state are NOT comparable and are flagged as such.

Pure functions over parsed JSON; nothing here executes cases or writes.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1


def load_rows(run_dir: Path) -> list[dict[str, Any]]:
    rows_path = run_dir / "rows.jsonl"
    if not rows_path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    for line in rows_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


def read_manifest_strict(run_dir: Path) -> dict[str, Any]:
    """Parse the manifest or raise — malformed artifacts must surface as
    errors, never silently become successful runs."""
    path = run_dir / "manifest.json"
    if not path.is_file():
        raise ValueError(f"{run_dir}: no manifest.json (incomplete run)")
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict) or manifest.get("schema_version") != SCHEMA_VERSION:
        raise ValueError(f"{run_dir}: manifest is not schema v1")
    return manifest


def nearest_rank(values: list[float], percentile: float) -> float | None:
    """Nearest-rank percentile: sorted ascending, rank = ceil(p/100 * n)."""
    if not values:
        return None
    ordered = sorted(values)
    rank = math.ceil(percentile / 100.0 * len(ordered))
    return float(ordered[max(1, min(rank, len(ordered))) - 1])


@dataclass(frozen=True)
class CaseSummary:
    case_id: str
    repeats: int
    passed_repeats: int
    failure_layers: dict[str, int]
    latency_ms: list[float]


@dataclass(frozen=True)
class RunSummary:
    run_dir: str
    run_id: str
    label: str
    suite: str
    started_utc: str
    finished_utc: str
    code_commit: str | None
    code_dirty: bool
    scenario_name: str
    scenario_seed: int | None
    model: dict[str, Any]
    cases: int
    passed: int
    failed: int
    pass_rate: float | None
    infra_failures: int
    pass_rate_excluding_infra: float | None
    latency_ms_p50: float | None
    latency_ms_p95: float | None
    prompt_tokens: int | None
    completion_tokens: int | None
    usage_missing_rows: int
    failure_layers: dict[str, int]
    per_case: list[CaseSummary] = field(default_factory=list)


@dataclass(frozen=True)
class IncompleteRun:
    run_dir: str
    reason: str


def aggregate_run(run_dir: Path) -> RunSummary:
    manifest = read_manifest_strict(run_dir)
    rows = load_rows(run_dir)

    passed = sum(1 for r in rows if r.get("passed") is True)
    failed = sum(1 for r in rows if r.get("passed") is False)
    total = len(rows)
    layers: dict[str, int] = {}
    for r in rows:
        layers[r.get("failure_layer", "none")] = layers.get(r.get("failure_layer", "none"), 0) + 1
    infra_failures = sum(1 for r in rows
                         if r.get("failure_layer") == "infra" and r.get("passed") is False)
    comparable = total - infra_failures
    latencies = [float(r["latency_ms"]) for r in rows
                 if isinstance(r.get("latency_ms"), (int, float))]

    prompt = [int(r["observed"]["totals"]["prompt_tokens"]) for r in rows
              if isinstance(r.get("observed", {}).get("totals"), dict)
              and isinstance(r["observed"]["totals"].get("prompt_tokens"), int)]
    completion = [int(r["observed"]["totals"]["completion_tokens"]) for r in rows
                  if isinstance(r.get("observed", {}).get("totals"), dict)
                  and isinstance(r["observed"]["totals"].get("completion_tokens"), int)]
    usage_missing = sum(1 for r in rows
                        if not isinstance(r.get("observed", {}).get("totals"), dict)
                        or r["observed"]["totals"].get("prompt_tokens") is None)

    by_case: dict[str, list[dict]] = {}
    for r in rows:
        by_case.setdefault(str(r.get("case_id")), []).append(r)
    per_case = [
        CaseSummary(
            case_id=cid,
            repeats=len(rows_for),
            passed_repeats=sum(1 for r in rows_for if r.get("passed") is True),
            failure_layers=_layer_counts(rows_for),
            latency_ms=[float(r["latency_ms"]) for r in rows_for
                        if isinstance(r.get("latency_ms"), (int, float))],
        )
        for cid, rows_for in sorted(by_case.items())
    ]

    return RunSummary(
        run_dir=str(run_dir),
        run_id=str(manifest.get("run_id")),
        label=str(manifest.get("label")),
        suite=str(manifest.get("suite")),
        started_utc=str(manifest.get("started_utc")),
        finished_utc=str(manifest.get("finished_utc")),
        code_commit=manifest.get("code_commit"),
        code_dirty=bool(manifest.get("code_dirty")),
        scenario_name=str((manifest.get("scenario") or {}).get("name", "")),
        scenario_seed=(manifest.get("scenario") or {}).get("seed"),
        model=dict(manifest.get("model") or {}),
        cases=total,
        passed=passed,
        failed=failed,
        pass_rate=(passed / total) if total else None,
        infra_failures=infra_failures,
        pass_rate_excluding_infra=(passed / comparable) if comparable else None,
        latency_ms_p50=nearest_rank(latencies, 50),
        latency_ms_p95=nearest_rank(latencies, 95),
        prompt_tokens=sum(prompt) if prompt else None,
        completion_tokens=sum(completion) if completion else None,
        usage_missing_rows=usage_missing,
        failure_layers=layers,
        per_case=per_case,
    )


def _layer_counts(rows: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for r in rows:
        layer = str(r.get("failure_layer", "none"))
        counts[layer] = counts.get(layer, 0) + 1
    return counts


def describe_incomplete(run_dir: Path) -> IncompleteRun:
    sidecar = run_dir / "run_incomplete.json"
    if sidecar.is_file():
        try:
            payload = json.loads(sidecar.read_text(encoding="utf-8"))
            return IncompleteRun(str(run_dir),
                                 f"interrupted: {payload.get('reason', 'unknown')}")
        except (json.JSONDecodeError, OSError):
            return IncompleteRun(str(run_dir), "interrupted (unreadable sidecar)")
    return IncompleteRun(str(run_dir), "incomplete-assumed (no manifest, no sidecar)")


def aggregate_runs(out_dir: Path) -> tuple[list[RunSummary], list[IncompleteRun]]:
    """Aggregate every directory under out_dir. Only manifest-bearing,
    schema-valid runs become summaries; everything else is listed as
    incomplete with its reason and excluded from all statistics. A corrupted
    manifest raises — malformed artifacts must be visible, not swallowed."""
    summaries: list[RunSummary] = []
    incompletes: list[IncompleteRun] = []
    if not out_dir.is_dir():
        return summaries, incompletes
    for entry in sorted(p for p in out_dir.iterdir() if p.is_dir()):
        if (entry / "manifest.json").is_file():
            summaries.append(aggregate_run(entry))  # may raise on corruption
        else:
            incompletes.append(describe_incomplete(entry))
    return summaries, incompletes


def comparability_note(summaries: list[RunSummary]) -> str:
    """Runs are comparable only within the same code revision state."""
    if len(summaries) < 2:
        return "single run: no cross-run comparability constraints"
    states = {(s.code_commit, s.code_dirty) for s in summaries}
    if len(states) == 1:
        commit, dirty = next(iter(states))
        return (f"all runs share revision { (commit or 'unknown')[:8] } "
                f"({'dirty' if dirty else 'clean'} tree): comparable")
    return ("MIXED revision states across runs — compare within, not across: "
            + "; ".join(f"{s.label}={ (s.code_commit or 'unknown')[:8] }"
                        f"({'dirty' if s.code_dirty else 'clean'})" for s in summaries))


def render_markdown(summaries: list[RunSummary], incompletes: list[IncompleteRun]) -> str:
    lines: list[str] = ["# Benchmark summary", ""]
    if incompletes:
        lines.append(f"**Incomplete runs (excluded from all statistics): {len(incompletes)}**")
        for inc in incompletes:
            lines.append(f"- `{inc.run_dir}` — {inc.reason}")
        lines.append("")
    if not summaries:
        lines.append("No successful runs to aggregate.")
        return "\n".join(lines) + "\n"

    lines.append(f"**Runs: {len(summaries)}** — {comparability_note(summaries)}")
    lines.append("")
    lines.append("| run | suite | cases | pass | pass× | pass (excl. infra) | infra | "
                 "p50 ms | p95 ms | tokens (p/c) | commit | dirty |")
    lines.append("|---|---|---|---|---|---|---|---|---|---|---|---|")
    for s in summaries:
        lines.append(
            f"| {s.label} | {s.suite} | {s.cases} | {s.passed} | "
            f"{_fmt_rate(s.pass_rate)} | {_fmt_rate(s.pass_rate_excluding_infra)} | "
            f"{s.infra_failures} | {_fmt_num(s.latency_ms_p50)} | {_fmt_num(s.latency_ms_p95)} | "
            f"{s.prompt_tokens}/{s.completion_tokens}"
            f"{' (+' + str(s.usage_missing_rows) + ' missing)' if s.usage_missing_rows else ''} | "
            f"{(s.code_commit or 'unknown')[:8]} | {'yes' if s.code_dirty else 'no'} |")
    lines.append("")
    lines.append("Failure layers: " + "; ".join(
        f"{s.label}: {_fmt_layers(s.failure_layers)}" for s in summaries))
    lines.append("")
    lines.append("## Per-case stability")
    lines.append("")
    lines.append("| run | case | repeats | passed | layers | p50 ms | p95 ms |")
    lines.append("|---|---|---|---|---|---|---|")
    for s in summaries:
        for c in s.per_case:
            lines.append(
                f"| {s.label} | {c.case_id} | {c.repeats} | {c.passed_repeats} | "
                f"{_fmt_layers(c.failure_layers)} | "
                f"{_fmt_num(nearest_rank(c.latency_ms, 50))} | "
                f"{_fmt_num(nearest_rank(c.latency_ms, 95))} |")
    return "\n".join(lines) + "\n"


def _fmt_rate(rate: float | None) -> str:
    return "n/a" if rate is None else f"{rate * 100:.1f}%"


def _fmt_num(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.1f}"


def _fmt_layers(layers: dict[str, int]) -> str:
    return ", ".join(f"{layer}:{count}" for layer, count in sorted(layers.items())) or "none"
