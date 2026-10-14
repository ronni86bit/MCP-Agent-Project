# AI Operations Investigation Agent (MCP)

A production-oriented, tool-using agent that dynamically discovers and invokes tools through
the Model Context Protocol to investigate operational questions across **PostgreSQL**,
**GitHub**, and a first-party **Monitoring/Incident** telemetry system — producing
evidence-grounded conclusions with human-approved actions.

Canonical scenario: *"Why did today's sales drop?"* — answered by correlating sales data,
a recent checkout deployment, rising checkout error rates, and the resulting alert/incident.

**Status: Phase 5 — planning & state (persistent investigations, plan revisions, recovery, grounding checks).**
(Phase 1: MCP fundamentals · Phase 2: postgres-mcp · Phase 3: agent runtime · Phase 4: multi-server correlation — complete.)

- Engineering rules & architecture decisions: see [AGENTS.md](AGENTS.md).
- Project guide: `MCP_Tool_Using_Production_Agent_Project_Guide.docx`.

## Quickstart

```bash
uv sync                                   # create .venv and install deps
uv run python scripts/toy_client_demo.py  # Phase 1: toy MCP server round-trip
uv run python scripts/dev_check.py        # phase gate: tests + both demos
```

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

## Layout

`backend/` (api, agent, mcp servers + client, security, observability) · `frontend/` (React/Vite console, later phases) · `tests/` (unit, integration, e2e, evaluation) · `scripts/` · `docker/` + `docker-compose.yml`.
