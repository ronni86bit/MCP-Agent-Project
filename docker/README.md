# Deployment & operations notes (Phase 10)

Operational reference for the Compose stack defined in `docker-compose.yml`. The
user-facing quickstart lives in the repository README; this file records the
deployment-specific behavior and limitations an operator needs.

## Image layout

`docker/Dockerfile.backend` builds ONE image that carries both the FastAPI API and the
three MCP servers: the investigation loop spawns them as **stdio subprocesses of the API
process** (`sys.executable -m backend.mcp.servers.*`), so no per-server containers or
network exposure exist for MCP — tool traffic stays on local pipes inside the container.
The venv is on PATH, which makes `sys.executable` resolve to the venv interpreter with all
runtime dependencies. `PYTHONPATH` is intentionally **not** set in the image:
`backend/mcp/client_manager.py` injects the repo root (`/app`) via `setdefault`, preserving
local-dev semantics. Runtime user: non-root `appuser`.

`docker/Dockerfile.frontend` builds the React/Vite console (`npm run build`) and serves the
static bundle with nginx, which also reverse-proxies `/api`, `/health`, `/ready`, and
`/metrics` to the `backend` service. `proxy_buffering` is off with 1-hour read timeouts so
server-sent events stream unbuffered.

## Configuration

All configuration is environment-only (`.env` at runtime, compose `environment:` blocks).
Never bake `.env` or tokens into images (`.dockerignore` excludes `.env`; the build context
is lockfile + `backend/` + `scripts/` + `frontend/` sources).

| Variable | Used by | Default / compose value |
|---|---|---|
| `OLLAMA_HOST` | backend (LLM calls) | compose: `http://host.docker.internal:11434` (host Ollama; `host-gateway` mapping added for Linux) |
| `OLLAMA_MODEL` | backend | `qwen3:4b-instruct` (pull on the host first) |
| `POSTGRES_MCP_DSN` | postgres-mcp subprocess | compose: read-only role at `postgres:5432` |
| `OPS_ADMIN_DSN` | seeder / state store | compose: admin role at `postgres:5432` |
| `ACTION_MODE_ENABLED` | backend | `false` — the write path is off unless explicitly enabled |
| `FRONTEND_HOST_PORT` | compose publish | `8090` (loopback only) |

Published ports (loopback only, by design — see Security): postgres `127.0.0.1:5433` +
`[::1]:5433`, backend `127.0.0.1:18080`, frontend `127.0.0.1:${FRONTEND_HOST_PORT:-8090}`.
Host ports 8000/8080 were avoided because WSL relays commonly occupy them on Windows.

## Startup, seeding, shutdown, recovery

- Ordering: `postgres` (healthcheck) → `seeder` (one-shot, must exit 0) → `backend`
  (healthcheck) → `frontend`.
- The seeder (`python -m scripts.seed_db`) is **idempotent**: it drops/recreates only the
  `sales_data` schema with deterministic rows and re-asserts the read-only role (re-run
  safe). It never touches `agent_state`.
- Backend startup applies the `agent_state` schema (v2) and runs the recovery sweep:
  investigations left `running` by a previous shutdown become `interrupted`; in-flight
  approval writes become `uncertain` (read-back recovery, never blind replay).
- `docker stop` → uvicorn graceful shutdown (30 s compose grace): investigations are
  cancelled, the pool closes, approval writes are drained. Anything that does not drain is
  handled by the next boot's sweep. Do not `docker kill` mid-write if you can avoid it.
- A postgres container **recreate** invalidates the backend's pooled connections — restart
  the `backend` service after recreating the database container.

## Security limitations (read this)

- The API is **unauthenticated**: the stack is designed as a local, single-operator demo.
  All publishes are loopback-only; do not expose it to a network. Do not enable
  `ACTION_MODE_ENABLED` unless you understand the approval workflow.
- Dev database credentials in compose/`.env.example` (`ops_admin_dev`, `ro_dev_password`)
  are documented defaults for a local container, overridable via environment.
- `/metrics` exposes only bounded operational counters — no payloads, ids, or prompts.

## Known limitations

- The LLM runtime is CPU-only Ollama on the reference laptop: investigations take minutes,
  and a small model currently fails several benchmark contracts (recorded honestly under
  `evaluation/results/`).
- `github-mcp` runs unauthenticated without `GITHUB_TOKEN` (60 req/hr per IP).
- SSE snapshots are periodic reads (1 s) — rapid internal transitions coalesce.
- A hard `docker kill` leaves an incomplete run directory if a benchmark was running;
  normal compose shutdown is graceful.
