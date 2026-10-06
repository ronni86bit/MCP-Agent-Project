# AI Operations Investigation Agent (MCP)

A production-oriented, tool-using agent that dynamically discovers and invokes tools through
the Model Context Protocol to investigate operational questions across **PostgreSQL**,
**GitHub**, and a first-party **Monitoring/Incident** telemetry system — producing
evidence-grounded conclusions with human-approved actions.

Canonical scenario: *"Why did today's sales drop?"* — answered by correlating sales data,
a recent checkout deployment, rising checkout error rates, and the resulting alert/incident.

**Status: Phase 1 — MCP fundamentals (toy server + client).**

- Engineering rules & architecture decisions: see [AGENTS.md](AGENTS.md).
- Project guide: `MCP_Tool_Using_Production_Agent_Project_Guide.docx`.

## Quickstart

```bash
uv sync                                   # create .venv and install deps
uv run python scripts/toy_client_demo.py  # discover + invoke tools on the toy MCP server
uv run python scripts/dev_check.py        # phase gate: full test suite + demo
```

## Layout

`backend/` (api, agent, mcp servers + client, security, observability) · `frontend/` (React/Vite console, later phases) · `tests/` (unit, integration, e2e, evaluation) · `scripts/` · `docker/`.
