# Benchmark results — schema v1

Output of `scripts/run_benchmark.py` (Phase 9). Every run gets a **unique,
append-only directory** — existing runs are never read for writing and never
overwritten (the runner aborts on a directory-name collision before touching
anything).

## Directory layout

```
evaluation/results/<UTC>_<label>/
├── rows.jsonl           one line per COMPLETED case (flushed + fsynced per row)
├── manifest.json        written LAST, atomically — the ONLY success marker
└── run_incomplete.json  sidecar for a HANDLED interruption (status, completed_seq, reason)
```

## `rows.jsonl` row (schema v1)

One JSON object per completed case. `seq` is the monotonic completion order.

```json
{
  "schema_version": 1,
  "seq": 7,
  "case_id": "D-BUDGET:wall_clock",
  "suite": "deterministic",
  "kind": "deterministic",
  "input": {},
  "expected": {},
  "observed": {"status": "failed", "llm_calls": 0, "evidence_ids": [],
               "failure_reason": "wall-clock budget exceeded before completion",
               "budget_class": "wall_clock",
               "budget_metric_delta": "{'budget_class': 'wall_clock'}: 1"},
  "checks": [{"name": "terminal_status", "passed": true, "detail": "status=failed"},
             {"name": "budget_metric_recorded", "passed": true, "detail": "..."}],
  "error_class": null,
  "latency_ms": 213.4,
  "diagnostics": {},
  "passed": false
}
```

`passed` is **derived** (`all(checks)`) by `backend.evaluation.schema.CaseResult`
— it is recomputed on every deserialization and can never be set by a caller.

## `manifest.json`

Written only after the whole suite finishes (per-case failures are still
recorded as failed rows; the run itself completed). Fields: `schema_version`,
`run_id`, `label`, `suite`, `kind`, `started_utc`/`finished_utc`,
`code_commit` (git, best-effort), `scenario` (name/seed/source path of the
fixture spec), `model` (scripted for the deterministic suite),
`config` (loop budgets in effect), `cases` (per-case seq/id/passed/failed
checks), `summary` (`cases`/`passed`/`failed`).

Atomicity: `manifest.json` is written to `manifest.json.tmp`, fsynced, then
`os.replace`d — readers observe either the previous state (absent) or the
complete file, never a partial one.

## Incomplete runs

- **Handled interruption** (Ctrl-C, runner exception outside a case): a
  `run_incomplete.json` sidecar is written with the completed sequence
  numbers and the reason; **no manifest exists**, so the run is never
  aggregated as successful. Completed rows remain valid and readable.
- **Hard kill** (process death): completed rows survive (per-row fsync), but
  no sidecar is written — such directories (no manifest, no sidecar) are
  reported as *incomplete-assumed* by tooling. A partial `manifest.json.tmp`
  left behind is ignored and never treated as an artifact. Nothing
  reconstructs a success that did not finish.
- **Consumers** must use successful runs only via
  `scripts.run_benchmark.iter_successful_runs()`, which yields exclusively
  manifest-bearing, schema-valid directories; a truncated or unparseable
  manifest excludes the run.

## Suites

- `deterministic` (Phase 9 Gate 2): scripted-LLM cases only; no Ollama, no
  network; PostgreSQL required for `D-SEL` and `D-APPROVE` (the runner fails
  fast with instructions if it is unreachable). Case ids: `D-SEL`, `D-EV`,
  `D-GROUND`, `D-GAP`, `D-FAIL`, `D-CIRCUIT`, `D-BUDGET:{llm_calls,steps,wall_clock}`,
  `D-APPROVE`, `D-CANCEL` — the registry ids of
  `backend/evaluation/cases_deterministic.py` (D-BUDGET runs once per budget class).
- Model-dependent suites (`model`, anti-causation, stability) arrive with
  Gate 3/4 and will use the same directory contract.
