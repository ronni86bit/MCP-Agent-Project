# AGENTS.md — Engineering Rules for the AI Operations Investigation Agent

Stable, project-level rules. Every ZCode task in this repository must follow them.
Locked decisions date from 2026-10-06 (architecture plan Rev 2, approved).
The authoritative project guide is `MCP_Tool_Using_Production_Agent_Project_Guide.docx` (never modify or delete it).

---

## 1. Project & North Star

Build an AI Operations Investigation Agent that **dynamically discovers and invokes tools through MCP**, investigates operational questions across **PostgreSQL, GitHub, and Monitoring/Incident telemetry**, produces **evidence-grounded conclusions**, supports **human-approved actions**, and exposes **production-grade reliability and observability**.

> "The LLM is not the product. The product is the reliable system around the LLM that can discover tools, gather evidence, reason over it, and take controlled action."

The compass loop everything must strengthen:
**QUESTION → PLAN → DISCOVER TOOLS → EXECUTE → COLLECT EVIDENCE → CORRELATE → VERIFY → APPROVE (when needed) → ACT → REPORT → OBSERVE**

## 2. MVP scope — three systems, fourteen tools

| MCP server | Tools (all read-only in MVP) | Role in investigation |
|---|---|---|
| `postgres-mcp` | `get_daily_sales`, `compare_sales`, `get_product_sales`, `get_region_sales`, `get_sales_trend` | Business metrics and historical comparisons |
| `github-mcp` | `get_recent_commits`, `get_deployments`, `get_open_issues`, `get_repository_activity` | Recent software changes and engineering activity |
| `monitoring-mcp` | `get_service_metrics`, `get_error_rate`, `get_incidents`, `get_recent_alerts`, `compare_metric_windows` | Operational telemetry, alerts, incidents (deterministic seeded fixtures) |

- **Slack is NOT part of the MVP.** No Slack code, config, phases, evaluation scenarios, or repository structure. A chat-integration system may only ever enter as a future, separate MCP server.
- `monitoring-mcp` is first-party and fixture-backed: deterministic seeded data generated from a shared scenario spec (`backend/mcp/fixtures/`), coherent with the Postgres sales data and GitHub deployment timeline so cross-system correlation is demonstrable.
- Canonical storyline and demo: **"Why did today's sales drop?"** — checkout deployment occurs shortly before the decline → checkout error rate rises after deployment → alert fires and an incident opens → sales decline concentrates in checkout-affected segments.
- Do not add tools beyond these 14 (+1 HITL write tool, §8) until the Phase 9 evaluation demonstrates a gap.

## 3. Non-negotiable architecture principles

1. MCP is a real architectural component: tools are discovered and invoked through actual MCP servers over the wire — never in-process function calls pretending to be MCP.
2. Preserve the boundary: the **agent runtime reasons and orchestrates**; **MCP servers expose controlled capabilities**. No integration logic in prompts; no business logic in the API layer; servers never call each other or the agent.
3. Prefer modular, explicit components (planner, selector, executor, evidence, verifier, synthesizer) over one giant agent prompt.
4. Read-only investigation is the default. Write tools require policy classification + explicit human approval (§8).
5. Tool failures are typed, handled gracefully, and never masked: bounded retries, alternate evidence paths, explicit evidence gaps in reports. Never pretend success.
6. Conclusions must cite evidence ids returned by tools; the verifier rejects unsupported claims and enforces correlation-vs-causation wording.
7. Tool calls and agent decisions are observable and auditable (structured logs, metrics, traces, append-only audit).
8. Model access goes through one provider-agnostic adapter (`backend/agent/llm/`). The loop never imports a provider SDK. Default target: **Ollama** via its OpenAI-compatible endpoint.
9. No heavyweight frameworks: no LangChain, LangGraph, pydantic-ai, CrewAI, or agent "orchestrators". The loop is the product.
10. Structured Pydantic models at every boundary (LLM output, tool args/results, API payloads).
11. Do not over-engineer: no technology is added to look impressive; every addition must serve the compass loop.

## 4. Locked technology decisions

- **Python 3.12**, managed with **uv** (`pyproject.toml`, `[dependency-groups].dev`).
- **FastAPI** API layer; **Pydantic v2** + pydantic-settings for config.
- **Official `mcp` Python SDK 2.x** — servers via `MCPServer` (`mcp.server.mcpserver`; the 1.x `FastMCP` name no longer exists), client via `ClientSession`. Per-call timeouts go through `call_tool(..., read_timeout_seconds=...)` — the session shields its request loop from external cancel scopes, and expiry raises `MCPError` code `-32001`. Result fields are snake_case (`is_error`, `input_schema`). Transport: **stdio** (subprocess) for dev and MVP deployment; streamable-HTTP may be enabled later by config only.
- **httpx** (+ respx in tests) for GitHub REST; **asyncpg** for Postgres 16 (business data + agent state + audit, separate schemas).
- **LLM: Ollama first** through an OpenAI-compatible client; provider/model switched by env config only (`OLLAMA_HOST`, `OLLAMA_MODEL`).
- **structlog** JSON logs, **prometheus-client** metrics, **OpenTelemetry** traces.
- **React 18 + Vite + TypeScript + TanStack Query** frontend; SSE for live investigation events.
- **pytest + pytest-asyncio** tests; **Docker Compose** (backend, frontend/nginx, postgres). Deploy target is a single host with compose — no Kubernetes.
- Monitoring data is **seeded JSON fixtures** from `scenario.yaml`; no external metrics backend in the MVP.

## 5. Repository ownership map

```
backend/main.py, backend/config.py     FastAPI app + settings
backend/api/                           routes, request/response schemas, deps (NO agent logic)
backend/agent/                         loop, planner, selector, executor, evidence, verifier, synthesizer, state, llm/adapter
backend/mcp/                           client_manager, catalog, policy, errors
backend/mcp/servers/                   one package per MCP server: toy_server, postgres_mcp, github_mcp, monitoring_mcp
backend/mcp/fixtures/                  scenario.yaml + generated seed JSON (single source of truth for the storyline)
backend/security/                      auth, approval flow, secret redaction
backend/observability/                 logging, metrics, tracing, audit
frontend/                              React/Vite console
tests/unit | integration | e2e | evaluation
scripts/                               toy_client_demo, seed_db, seed_monitoring, run_benchmark, dev_check
docker/                                build files, nginx.conf, initdb
```

## 6. Coding conventions

- Full type hints; `from __future__ import annotations` in modules using modern syntax.
- Pydantic models (or typed dataclasses for internal records) at every boundary; no bare dicts crossing module edges.
- Async-first (`asyncio` + `anyio` primitives where the SDK requires them).
- Docstrings state purpose and constraints, not narration. Comments only for non-obvious constraints.
- Error taxonomy: typed exception classes under `backend/mcp/errors.py` (and later API equivalents); no bare `except Exception` without re-raise or typed wrap.
- Logging: `structlog` only; every log line carries `server`/`session`/`tool` context ids where applicable; arguments are sanitized (no secrets, truncated payloads).
- **stdio MCP server processes must never write to stdout except protocol traffic — all logs go to stderr.**
- Secrets only via environment/`.env` (`.env` is git-ignored; `.env.example` is the committed template). Never in prompts, source, or logs; redact secret-shaped values.
- SQL: parameterized, allowlisted query templates only. The model never authors SQL in the MVP.
- Scripts are cross-platform (Windows dev machine; POSIX CI/deploy) — no bash-only assumptions.

## 7. Testing expectations

- Unit: schemas, policy engine, retry/backoff (fake clock), verifier grounding rules.
- Integration: each MCP server against its backing store (Postgres via Docker; GitHub via respx mocks; monitoring via fixtures); MCP client round-trips (in-process + subprocess); API via httpx ASGI.
- E2E: one scripted canon investigation with real stdio MCP subprocesses and scripted/recorded LLM responses.
- Evaluation (Phase 9+): scenario benchmark incl. anti-causation cases (deployment without incident; incident without deployment; no-anomaly calibration) and injected tool-failure recovery.
- **Phase gate:** `python scripts/dev_check.py` (pytest + demo) must pass before a phase is called done. No phase-jumping: complete and test the current phase before starting the next, even when a later feature looks impressive.

## 8. Safety constraints

- Read-only DB role for postgres-mcp; row/limit caps on query tools.
- Every tool is statically classified `read_only` or `write` at registration; in the default mode write tools are not even advertised to the model.
- The single MVP write tool is `acknowledge_alert` on monitoring-mcp (writes to the fixture store only), gated behind the full approval flow: ActionProposal → awaiting_approval → human approve/reject → execute → verify → audit.
- Audit records are append-only and link who asked, what ran, what was approved, and what happened.
- GitHub token: fine-grained, read-only scopes only.
- Rate limits respected (per-server token bucket; honor `Retry-After`); bounded loop (max steps, wall-clock, LLM calls) with a circuit breaker.

## 9. Phase discipline and current status

1. MCP fundamentals (toy server + client) — **done**
2. postgres-mcp (5 read-only tools vs seeded business data) — **done**
3. Agent runtime (FastAPI + Ollama adapter + MCP client; postgres-only investigations; tool-call smoke eval) — **done**
4. Multi-server (monitoring-mcp fixtures, then github-mcp; cross-system correlation) ← **current (implemented, pending review)**
5. Planning & state (explicit plans, persisted investigation state, evidence memory)
6. Reliability (validation, retries, timeouts, rate limits, partial-failure recovery)
7. Human-in-the-loop (read/write policy split; `acknowledge_alert` approval flow)
8. Observability (logs, metrics, traces, audit, UI trace view)
9. Evaluation (benchmark incl. recovery scenarios)
10. Deployment & portfolio (containers, docs, demo)

## 10. Environment notes

- Windows 11 dev machine, Git Bash shell; paths must stay cross-platform.
- Ollama is installed locally with `qwen3:4b-instruct` (the default `OLLAMA_MODEL`). Local-model tool-calling is the top technical risk: rely on JSON-schema-constrained outputs, Pydantic validation + one repair retry, and the Phase 3 smoke eval. CPU inference is slow (~3 s per prompt token measured): keep prompts small, cap `max_tokens`, and budget `LLM_TIMEOUT_SECONDS`/`MAX_INVESTIGATION_SECONDS` accordingly.
- Docker Desktop hosts Postgres for dev/tests; it may need to be started manually (`docker compose up -d postgres`).
- Python deps are resolved by uv (`uv sync`); run tools via `uv run ...`.
