# AI Operations Investigation Agent (MCP)

A production-oriented, tool-using agent that dynamically discovers and invokes tools through
the Model Context Protocol to investigate operational questions across **PostgreSQL**,
**GitHub**, and a first-party **Monitoring/Incident** telemetry system — producing
evidence-grounded conclusions with human-approved actions.

Canonical scenario: *"Why did today's sales drop?"* — answered by correlating sales data,
a recent checkout deployment, rising checkout error rates, and the resulting alert/incident.

**Why this project:** the LLM is not the product — the product is the reliable system around
it. Every conclusion must cite tool-retrieved evidence (the verifier drops or flags
unsupported claims and downgrades causal language), every tool call is observable and
budgeted, every failure is typed and recovered explicitly, and the single write action is
gated behind a persisted human-approval workflow. The evaluation framework measures exactly
that: selection accuracy, evidence coverage, grounding, anti-causation discipline, and
failure handling — with recorded, reproducible runs.

**Status: Phase 10 complete — deployment & portfolio (Docker Compose stack, browser console, evaluation framework, docs).**
Phases 1–10 all complete: MCP fundamentals · postgres-mcp · agent runtime · multi-server
correlation · planning & state · reliability · human-in-the-loop approvals · production
observability · evaluation & benchmarking · deployment & portfolio.

- Engineering rules & architecture decisions: see [AGENTS.md](AGENTS.md).
- Project guide: `MCP_Tool_Using_Production_Agent_Project_Guide.docx`.

## Quickstart

### A. Docker Compose demo (browser investigation — the 10-minute path)

Prerequisites: **Docker Desktop** (or Docker Engine + Compose v2) and **host Ollama** with a
tool-capable model. No other services; everything is free and local.

```bash
copy .env.example .env        # Windows (cp on POSIX); dev defaults are documented, not real secrets
docker compose up -d --build  # postgres -> seeder -> backend -> frontend (first build: several minutes)
```

Then open **http://127.0.0.1:8090/** and click **Investigate** on the pre-filled canonical
question. The console shows live SSE status while the agent plans, calls MCP tools, and
synthesizes a grounded report (a CPU-only run takes roughly 5–15 minutes). Operations
endpoints: `http://127.0.0.1:18080/health`, `/ready`, `/metrics`.

> **Security boundary:** this is a **local, single-operator demo**. The API is
> unauthenticated and everything is published on `127.0.0.1` only — do not expose it to an
> untrusted network.

### B. Local development (tests and phase demos)

```bash
uv sync                                   # create .venv and install deps
uv run python scripts/toy_client_demo.py  # Phase 1: toy MCP server round-trip
uv run pytest tests/unit tests/integration -q
uv run python scripts/dev_check.py        # phase gate: tests + demos -> PHASE GATE PASSED (Phases 1-10)
```

### Architecture

```
browser console (React/Vite, nginx)
      │  /api (HTTP + SSE)
      ▼
FastAPI backend (uvicorn) ── InvestigationLoop: UNDERSTAND → PLAN → SELECT → VALIDATE
      │                                    → EXECUTE → OBSERVE → CORRELATE → VERIFY → REPORT
      │                                    → (optional) propose_action
      ├─ MCP stdio subprocesses (same image): postgres-mcp · monitoring-mcp · github-mcp
      │      (discovery + tool invocation over stdio; allowlisted, policy-checked)
      ├─ PostgreSQL: agent_state schema (persisted investigations, approvals) + sales_data (seeded)
      └─ host Ollama (OpenAI-compatible endpoint) for the LLM
```

Component responsibilities: the **loop** orchestrates and reasons; the **MCP servers** expose
exactly the allowed capabilities (14 read-only tools + the human-approved
`acknowledge_alert` write); **nginx** serves the console and proxies `/api` (SSE-safe);
**PostgreSQL** persists investigation state and approval audit; **Postgres seeder** rebuilds
the deterministic `sales_data` fixtures (idempotent, `agent_state` untouched).

## Phase 2 — PostgreSQL MCP (`postgres-mcp`)

```bash
docker compose up -d postgres                       # PostgreSQL 16 on localhost:5433
uv run python scripts/seed_db.py                    # deterministic seed + read-only role
uv run python scripts/postgres_demo.py              # discovery + all five tools via MCP
uv run python -m pytest                             # tests (integration skips without PG)
uv run python -m backend.mcp.servers.postgres_mcp   # run the server standalone (stdio)
```

`scripts/postgres_demo.py` starts the Postgres container and seeds it automatically when
needed, so `scripts/dev_check.py` is a one-command gate.

### The five tools (read-only, parameterized, allowlisted SQL)

| Tool | Purpose |
|---|---|
| `get_daily_sales` | Daily totals (orders/units/revenue) for a date range, with optional product/region/channel filters |
| `compare_sales` | Two-window comparison with absolute + percent delta; **windows must be equal-length** (rejected otherwise — no silent normalization); `group_by` product/region/channel locates where a change came from |
| `get_product_sales` | Per-product totals ranked by revenue |
| `get_region_sales` | Per-region totals ranked by revenue |
| `get_sales_trend` | Day- or week-granularity time series |

Example — the canonical baseline-vs-incident comparison:

```json
{
  "name": "compare_sales",
  "arguments": {
    "window_a_start": "2026-09-28", "window_a_end": "2026-09-30",
    "window_b_start": "2026-10-03", "window_b_end": "2026-10-05",
    "metric": "revenue", "group_by": "channel"
  }
}
```

### Architecture boundary (Phase 2)

`postgres-mcp` is a standalone MCP server (mcp SDK 2.x, stdio) over a Dockerized Postgres
seeded from `backend/mcp/fixtures/scenario.yaml` — the single source of truth for the canon
storyline timeline (healthy baseline → checkout-incident decline on the `mobile` channel →
"today", pinned at 2026-10-05 for determinism). The server connects with a dedicated
read-only role; every statement is a static parameterized template in `queries.py`; there is
no generic SQL tool. GitHub and Monitoring/Incident servers arrive in Phase 4.

## Phase 3 — Agent runtime (Ollama + InvestigationLoop + API)

```bash
ollama pull qwen3:4b-instruct              # or set OLLAMA_MODEL to another tool-capable model
uv run uvicorn backend.main:app --port 8000
uv run python scripts/model_smoke.py       # 4 tool-selection probes against the real model
uv run python scripts/agent_demo.py        # canonical investigation end-to-end (CPU: ~5-15 min)
uv run python -m pytest tests/unit tests/integration   # deterministic tests (scripted LLM)
```

Ask the agent a question (investigations run in the background; poll for the result):

```bash
curl -s -X POST localhost:8000/api/investigations \
     -H "content-type: application/json" \
     -d '{"question": "Why did today'"'"'s sales drop?"}'
curl -s localhost:8000/api/investigations/<investigation_id>
```

Flow: `POST /api/investigations` → AgentService → **InvestigationLoop**
(UNDERSTAND → PLAN → SELECT → VALIDATE → EXECUTE → OBSERVE → CORRELATE → VERIFY → REPORT)
→ Ollama via the provider-agnostic adapter (`backend/agent/llm/`) → tools discovered from
postgres-mcp through MCP → every tool result becomes an `Evidence` record → the Synthesizer
must cite evidence ids → the Verifier drops ungrounded claims and downgrades causal language.
The final `InvestigationReport` carries conclusion, claims, confidence, caveats, and evidence gaps.

Key env knobs (see `.env.example`): `OLLAMA_HOST`, `OLLAMA_MODEL`, `MAX_INVESTIGATION_STEPS`,
`MAX_LLM_CALLS`, `MAX_INVESTIGATION_SECONDS`, `LLM_TIMEOUT_SECONDS`.

## Phase 7 — Human-in-the-loop approvals

The monitoring-mcp `acknowledge_alert` write tool is the first gated action.
The loop's optional `propose_action` phase (enabled by `ACTION_MODE_ENABLED=true`) drafts a
proposal referencing an eligible unacknowledged alert from the collected evidence. The
proposal is persisted with a canonical SHA-256 hash binding action + target + arguments +
version + expiry. A human approves or rejects via the API (`decided_by` is a mandatory
non-blank audit label). Only an `approved` proposal can execute — always with the stored
arguments, never client-supplied. Execution runs pre-dispatch read-back (rejecting stale or
already-acknowledged targets), then dispatches the write. Post-dispatch cancellation or
transport failure marks the approval `uncertain`; read-back-based recovery with a fresh
operator label resolves it without a blind replay. Verification read-back on the same session
confirms the acknowledgement — `verification.ok=false` is persisted as a visible discrepancy,
distinct from the execution status.

API:
- `GET /api/approvals?status=` — list, optionally filtered by status
- `GET /api/approvals/{id}` — one approval (404 if unknown)
- `POST /api/approvals/{id}/decision` — `{decision: "approved"|"rejected", decided_by, comment?}`
- `POST /api/approvals/{id}/execute` — `{executed_by}` (no write arguments accepted)

### Acknowledgement-state durability

Acknowledgement state lives in the monitoring process's in-memory fixture store. It resets
when the monitoring server restarts. The durable audit record lives in the `approvals` table
in the state store.

### Architecture boundary (Phase 3)

The agent runtime only ever sees tools through MCP discovery (policy allowlists filter
unexpected ones) and only ever reaches the LLM through the adapter interface. No approvals,
persistence, or frontend yet — Phases 5-8.

## Phase 4 — Multi-server MCP + cross-system correlation

The agent now investigates across three independent MCP servers (14 read-only tools) and
correlates their evidence for the canonical question:

| Server | Tools | Evidence domain |
|---|---|---|
| `postgres-mcp` | 5 sales tools | business |
| `monitoring-mcp` | `get_service_metrics`, `get_error_rate`, `get_incidents`, `get_recent_alerts`, `compare_metric_windows` (deterministic fixtures from `scenario.yaml`) | operational |
| `github-mcp` | `get_recent_commits`, `get_deployments`, `get_open_issues`, `get_repository_activity` (GitHub REST; read-only token) | engineering |

Tools are addressed as `server__tool` in the LLM's tool catalog; duplicate base tool names
across servers are a typed configuration error. Evidence records carry their originating
server and domain, and the report's claims must cite them — temporal overlap between sales
decline, service degradation, and a deployment is reported as correlation, never causation.

The canonical storyline is one timeline from `backend/mcp/fixtures/scenario.yaml`: healthy
baseline → checkout deployment 2026-10-02T18:30Z → checkout error rate spikes → alert
ALT-2071 → incident INC-1042 → mobile-channel sales collapse 10-03..10-05.

```bash
uv run python scripts/seed_monitoring.py    # materialize monitoring fixtures as JSON
uv run python scripts/model_smoke.py        # 6 selection probes across all three servers
uv run python scripts/persistence_demo.py   # persistence + restart-recovery demo
uv run python scripts/agent_demo.py         # canonical cross-system investigation
```

## Phase 5 — Planning & state

Investigations persist to PostgreSQL (`agent_state` schema, versioned initialization,
logically separate from the `sales_data` business schema): investigation records, per-version
plans, steps, tool calls, evidence, reports, and an append-only event log. The API's
`GET /api/investigations/{id}` returns persisted state — retrieval survives backend restarts.

- **Plan lifecycle:** plans are versioned; a post-step reviewer (bounded by `MAX_PLAN_REVISIONS`)
  may adopt a revised plan when evidence changes direction. Previous versions are preserved
  and never silently discarded; collected evidence always survives a revision.
- **Recovery:** on startup, investigations still marked `running` become `interrupted`, and
  tool calls recorded mid-flight become `unknown` — uncertain external operations are never
  re-executed blindly. (Resuming execution in-flight is deferred to Phase 6.)
- **Grounding:** the verifier now also runs deterministic checks — every percentage/currency
  figure and every quoted or multi-word named entity in a claim must be locatable in the
  cited evidence, else the claim is flagged with confidence capped and an explicit caveat.
  This narrows but does not solve semantic grounding (Phase 9).
- **Forward design:** an `approvals` table exists in the state schema so Phase 7's
  approval-gated actions have a home (nothing writes to it yet).

Compatible API changes (additive): investigation `status` may now be `interrupted`; step
statuses use `pending|running|completed|failed|skipped`; responses include `plan_version`,
`plan_history`, `stage`, and full `evidence` records.

GitHub configuration (optional): set `GITHUB_TOKEN` (fine-grained, read-only) and
`GITHUB_REPO` in `.env`. Without a token, public repositories work unauthenticated at lower
rate limits. `get_deployments` includes one record labeled `source=scenario-fixture` — the
demo storyline's checkout deployment, not live GitHub state. Tests never touch live GitHub:
they run the server against a local mock (`GITHUB_API_BASE_URL`).

## Phase 8 — Production observability

### Structured logging, correlation IDs, redaction

Every process logs **structlog JSON to stderr** (stdout is reserved for MCP protocol
traffic). Incoming requests get a correlation ID: a client-supplied `X-Request-ID` is
validated against `^[a-z]{3,8}-[0-9a-f]{12}$` (a fresh one is generated otherwise), bound
to the request and to every investigation's log context, returned in the response header,
and always cleaned up. Log values pass through a redaction processor that recursively
removes sensitive keys (case-insensitive: `authorization`, `api_key`, `token`, `password`,
`secret`, …), strips credentials from URLs, never mutates the original payload, and never
raises. Log level: `LOG_LEVEL` env (see `.env.example`).

### Metrics (`/metrics`)

Prometheus text format on the private registry — 14 `ops_agent_*` families with strictly
bounded labels (server, tool, purpose, outcome, status — never IDs or free text):

| Family | Type | What it counts |
|---|---|---|
| `ops_agent_investigations_total` | counter | terminal investigation outcomes (`complete`/`failed`) — normal terminal returns only (see caveat) |
| `ops_agent_investigation_duration_seconds` | histogram | wall-clock duration of those runs |
| `ops_agent_investigation_budget_exhausted_total` | counter | `wall_clock` / `steps` / `llm_calls` budget exhaustion events |
| `ops_agent_llm_calls_total` | counter | LLM calls per purpose × outcome (`ok`/`timeout`/`unavailable`/`model_not_available`/`malformed`/`error`) |
| `ops_agent_llm_call_duration_seconds` | histogram | successful LLM call latency per purpose |
| `ops_agent_llm_retries_total` | counter | transient-failure retries inside the LLM adapter |
| `ops_agent_mcp_tool_calls_total` | counter | tool executions per server × tool × outcome (`ok`/`circuit_open`/`timeout`/`transport`/`error`) |
| `ops_agent_mcp_tool_duration_seconds` | histogram | tool execution latency |
| `ops_agent_circuit_breaker_state` | gauge | per-server breaker state (0 closed, 1 half_open, 2 open) |
| `ops_agent_circuit_breaker_transitions_total` | counter | breaker state transitions |
| `ops_agent_rate_limit_waits_total` / `..._wait_seconds` | counter/histogram | token-bucket waits per server |
| `ops_agent_approvals_total` | counter | approval lifecycle transitions into each status |
| `ops_agent_approval_verification_total` | counter | post-write verification outcomes (`true`/`false`) |

Instrumentation is non-invasive by rule: every observation is synchronous, wrapped so a
metrics failure can only degrade to a debug log — it never changes an investigation, tool
call, or approval outcome.

**Caveat — investigation metric semantics:** the counter is observed at the single normal
return of `InvestigationLoop.run()`, so it measures *normal terminal returns*
(`complete`, or `failed` via the loop's `_fail`). A hard cancellation (`task.cancel()`,
e.g. shutdown) propagates `CancelledError` — deliberately unconverted — before the
observation line; such runs are **not** counted, and the startup sweep later persists them
as `interrupted`. The metric therefore does not include every persisted terminal status.

### Liveness & readiness

| Endpoint | Semantics |
|---|---|
| `GET /health` | Liveness — static 200 while the process can serve; probes nothing, so a hung dependency can never hang it |
| `GET /ready` | Readiness — 200 `ready` / 503 `not_ready`; probes the LLM provider and every configured MCP server, **cached 30 s with single-flight** (concurrent callers share one probe; no stampede) |
| `GET /metrics` | Prometheus exposition (no redirect) |
| `GET /api/health` | Phase 3 contract, unchanged: `{status, model, ollama, mcp_servers}` with live per-dependency detail |

**Readiness dependency decision:** *required* — the LLM provider and all configured MCP
servers (without them the loop cannot understand, plan, or collect any evidence).
*Not required* — the PostgreSQL state store: `startup()` deliberately degrades to
in-memory operation when it is unreachable and read-only investigations still work, so
`/ready` reports `checks.state_store = {gating: false, mode: persistent | in_memory_fallback}`
informationally and never gates on it.

## Phase 9 — Evaluation & benchmarking

A reproducible benchmark harness (`scripts/run_benchmark.py`) over a fixed case
registry (`backend/evaluation/`), scored by pure graders that reuse the
verifier and the preserved smoke/canonical assertions.

### Suites and commands

```bash
uv run python scripts/run_benchmark.py --suite deterministic --label L   # scripted, no Ollama, seconds
uv run python scripts/run_benchmark.py --suite model --label L           # M-SEL probes + M-CANON (real model, ~20-40 min)
uv run python scripts/run_benchmark.py --suite anti-causation --label L  # three scenario-variant investigations (~20-30 min)
uv run python scripts/run_benchmark.py --suite model --label L --repeat 3  # stability repetitions
```

- `deterministic`: scripted LLM + stub/failing doubles; PostgreSQL needed for
  two cases; **no Ollama**; part of the project gate via
  `pytest tests/evaluation -m "not ollama"`.
- `model` / `anti-causation`: real Ollama investigations over real MCP
  subprocesses; **never gate-blocking**; Ollama-marked tests follow the same
  rule. Model runs keep action mode disabled — no write path.
- `--repeat N` re-runs each model-kind case N times (per-repeat rows with
  `repeat_index`) for stability statistics.

### Results format

Each run writes `evaluation/results/<UTC>_<label>/`: `rows.jsonl` (one
fsynced line per completed case), `manifest.json` (written **last**,
atomically — the only success marker; records code commit **and whether the
working tree was dirty**, scenario name/seed, model identity and endpoint
without credentials, budgets), `summary.md` (aggregated view), and
`run_incomplete.json` after a handled interruption. Runs are append-only; a
directory collision aborts. Run outputs are gitignored; the schema lives in
`evaluation/results/README.md`.

### Aggregation semantics

`backend/evaluation/report.py` aggregates only manifest-bearing, schema-valid
runs. Incomplete runs (no manifest) are listed with their reason —
sidecar-recorded interruption or "incomplete-assumed" after a hard kill — and
excluded from every statistic. Per run: strict pass rate,
**pass rate excluding infrastructure failures** (infra noise is visible,
never laundered into the model score), nearest-rank p50/p95 latency, token
sums with missing-usage rows counted, per-case repeat stability, and the
failure-layer breakdown. Runs with different code revisions or dirty-tree
state are flagged as incomparable.

### Failure classification

Conservative and evidence-based (`failure_classification.py`): `infra` only on
observed typed infrastructure errors or verified dependency/coherence
failures; `budget` only on recorded budget exhaustion; `reasoning` only when
infrastructure is verified healthy AND the run reached a terminal state AND
grading failed; `unknown` for harness cancellations, timeouts, and ambiguous
partials. An exception alone is never classified as reasoning.

### Anti-causation variants

Three deterministic scenario variants (`tests/evaluation/scenarios/`) split
the canonical story apart: `deployment_without_incident` (a deployment ships,
nothing breaks), `incident_without_deployment` (a real outage with no
release), and `no_anomaly` (calibration). Each case verifies cross-store
coherence (monitoring/github/postgres serve the same variant) **before** the
model runs and grades structurally afterwards: verifier strictness, fixture
truth bands (thresholds mirrored from the storyline tests), causal language
via the verifier's own vocabulary, and — for the incident variant — refusal
of causal deployment attribution (mentioning a deployment is permitted;
coupling it to causal language is not).

### Budgets, limitations, and executed-vs-unexecuted status

Wall budgets: model/anti-causation cases carry a per-case wall guard
(`MAX_INVESTIGATION_SECONDS` + 120 s); an over-running case becomes an
explicit `unknown` row. Realistic single-pass runtimes on the reference
laptop (i5-1135G7, CPU-only): selection probes 8–95 s each, a canonical or
variant investigation 6–15 min, `--suite model` ≈ 20–40 min, `--suite
anti-causation` ≈ 20–30 min. Limitations: qwen3:4b selection is varianced
(temperature 0.1, no seed) — stability is measured, not eliminated;
GITHUB_TOKEN is unset so GitHub runs unauthenticated (rate-limit failures are
classified `infra`); a hard kill leaves an incomplete run that is reported
but never reconstructed. Which model benchmarks were actually executed is
recorded by the committed runs under `evaluation/results/` — see their
manifests; unexecuted suites are not claimed anywhere.

## Phase 10 — Deployment & portfolio (compose stack, console, docs)

### The Compose stack

| Service | Image | Purpose | Published (host) |
|---|---|---|---|
| `postgres` | postgres:16-alpine | business + state store, named volume `ops_pgdata` | `127.0.0.1:5433` + `[::1]:5433` |
| `seeder` | built backend image | one-shot deterministic `sales_data` seed (idempotent; `agent_state` untouched) | — |
| `backend` | built backend image | FastAPI API + the three MCP stdio subprocess runtimes | `127.0.0.1:18080` |
| `frontend` | node build → nginx | console static bundle + `/api` reverse proxy (SSE-safe) | `127.0.0.1:${FRONTEND_HOST_PORT:-8090}` |

Startup ordering is dependency-driven: `postgres` (healthcheck) → `seeder` (must exit 0) →
`backend` (healthcheck on `/health`) → `frontend`. Host ports 8000/8080 are deliberately
avoided (commonly occupied on Windows by WSL relays); all publishes are loopback-only.

### Configuration

Everything is environment-driven (`.env`, see `.env.example`): `OLLAMA_HOST`/`OLLAMA_MODEL`
(host Ollama is required at `http://localhost:11434` by default — the compose backend
overrides this to `http://host.docker.internal:11434`, with `host-gateway` mapping for
Linux), LLM timeout/temperature/retries, loop budgets (`MAX_INVESTIGATION_SECONDS=1200`,
`MAX_LLM_CALLS=16`, …), MCP call timeout, circuit-breaker/rate-limit knobs, Postgres DSNs
(dev credentials are documented defaults, not real secrets), approval settings, and
`GITHUB_TOKEN` (optional, fine-grained read-only). Compose adds `FRONTEND_HOST_PORT`
(default 8090). `ACTION_MODE_ENABLED` ships `false`: the write path is off unless you
explicitly turn it on.

### Operations

- **Seeding & recovery:** the seeder rebuilds `sales_data` deterministically on every
  `compose up` (idempotent, never touches `agent_state`). On boot, the backend applies the
  `agent_state` schema and runs a recovery sweep: investigations interrupted by a previous
  shutdown become `interrupted` (never silently re-run); in-flight approval writes become
  `uncertain` for read-back recovery.
- **Shutdown:** `docker stop` triggers uvicorn's graceful shutdown (investigations
  cancelled, pool closed, approval writes drained within a 30 s grace period).
- **Endpoints:** `/health` (static liveness), `/ready` (30 s-cached dependency check: host
  Ollama + all three MCP servers; the Postgres state store is informational — startup
  degrades to in-memory if it is down), `/metrics` (the `ops_agent_*` Prometheus families),
  `/api/health` (uncached per-dependency detail).
- **SSE:** `GET /api/investigations/{id}/events` streams `status` snapshots (1 s poll of the
  live record — transitions between polls coalesce, so not every internal transition is
  emitted), a `terminal` event, and keepalive comments; the stream is unbuffered end-to-end
  (`X-Accel-Buffering: no` + nginx `proxy_buffering off`).

### Approval-gated actions

`acknowledge_alert` is the only write action. Even with action mode enabled, the model can
only **propose**; a persisted proposal must be approved by a named operator through the
console or API before execution, execution always uses the stored arguments, and a post-
write read-back verification is recorded. With `ACTION_MODE_ENABLED=false` (the default in
compose) the propose phase never runs.

### Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `docker compose up` fails to bind 8000/8080/5433 | Windows WSL relays or a local Postgres often hold 8000/8080; the compose file already uses 18080/8090/5433 — override `FRONTEND_HOST_PORT` or edit the port lines |
| `localhost:5433` connections stall ~2 s | a `127.0.0.1`-only bind makes the `::1` attempt hang; the compose file publishes both `127.0.0.1` and `[::1]` — keep both |
| `/ready` reports `ollama down` | start Ollama on the host and `ollama pull qwen3:4b-instruct` (or set `OLLAMA_MODEL`); on Linux add `host.docker.internal:host-gateway` (compose already does) |
| `FAILED: PostgreSQL unreachable` during `dev_check` | start the stack's database: `docker compose up -d postgres` |
| frontend blank / API 404 from the console | nginx proxies `/api` to the `backend` service — check `docker compose ps backend` is healthy |
| investigation `status: failed` | read `failure_reason` in the API/console; typed MCP failures (timeouts, circuit-open) are reported as source-unavailable, never silently skipped |

### Portfolio description

This repository is an end-to-end demonstration of building a *reliable system around an
LLM*: a bounded, checkpointed investigation loop; dynamic tool discovery over MCP with
policy allowlists; typed failure taxonomy with retries, rate limits, and circuit breakers;
deterministic grounding verification with causal-language discipline; a persisted,
human-approval-gated write path; Prometheus metrics, structured logging with secret
redaction, and liveness/readiness endpoints; a recorded evaluation framework (selection
accuracy, evidence coverage, grounding, anti-causation scenario variants, conservative
failure classification); and a single-command Docker Compose deployment with a browser
console. Evidence: 358 automated tests, a seven-step phase gate, and recorded benchmark
runs under `evaluation/results/` (see the Phase 9 section for honest findings on a
small-model runtime).

## Layout

`backend/` (api, agent, mcp servers + client, security, observability) · `frontend/` (React/Vite console) · `tests/` (unit, integration, e2e, evaluation) · `scripts/` · `docker/` + `docker-compose.yml`.
