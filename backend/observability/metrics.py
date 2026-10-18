"""Prometheus metrics for the agent runtime (Phase 8, Gate B).

Contract (Plan Rev 3, Part VI): 14 metrics, all prefixed `ops_agent_`, all
labels bounded by construction — `server` is one of the three configured MCP
servers, `tool` a base tool name from the fixed registry, `purpose` one of the
six loop LLM call sites, `outcome`/`status` fixed enumerations. No
investigation ids, request ids, alert ids, or free-form text ever becomes a
label value (cardinality guard truncates defensively at 80 chars regardless).

Non-invasive by rule: every observation goes through a `_safe`-wrapped helper
that is synchronous (no await points), never raises, and never mutates the
observed value. A metrics failure degrades to a debug log line — it can never
change an investigation, tool call, or approval outcome.

All collectors live on a private module-level `REGISTRY` (never the global
prometheus default), so test processes and the `/metrics` endpoint share one
deterministic registry and imports are idempotent.
"""

from __future__ import annotations

from prometheus_client import (
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
)

import structlog
log = structlog.get_logger(__name__)

REGISTRY = CollectorRegistry()

_LABEL_MAX = 80

# Fixed enumerations — keep these in sync with the wire points listed below.
INVESTIGATION_STATUSES = ("complete", "failed")
LLM_PURPOSES = (
    "understand", "plan", "select_tool", "review_plan", "synthesize", "propose_action",
)
LLM_OUTCOMES = (
    "ok", "timeout", "unavailable", "model_not_available", "malformed", "error",
)
TOOL_OUTCOMES = ("ok", "circuit_open", "timeout", "transport", "error")
BUDGET_CLASSES = ("wall_clock", "steps", "llm_calls")
BREAKER_STATES = ("closed", "half_open", "open")
_BREAKER_STATE_VALUE = {"closed": 0, "half_open": 1, "open": 2}

# -- collectors ---------------------------------------------------------------

investigations_total = Counter(
    "ops_agent_investigations_total",
    "Terminal investigation outcomes (counted exactly once per investigation).",
    ["status"],
    registry=REGISTRY,
)
investigation_duration_seconds = Histogram(
    "ops_agent_investigation_duration_seconds",
    "Wall-clock duration of terminal investigations.",
    buckets=(0.5, 1, 2.5, 5, 10, 30, 60, 120, 300, 600, 1200),
    registry=REGISTRY,
)
investigation_budget_exhausted_total = Counter(
    "ops_agent_investigation_budget_exhausted_total",
    "Times a bounded budget ran out (wall clock, steps, or LLM calls).",
    ["budget_class"],
    registry=REGISTRY,
)
llm_calls_total = Counter(
    "ops_agent_llm_calls_total",
    "LLM calls made by the loop, by call-site purpose and outcome.",
    ["purpose", "outcome"],
    registry=REGISTRY,
)
llm_call_duration_seconds = Histogram(
    "ops_agent_llm_call_duration_seconds",
    "Duration of successful LLM calls, by purpose.",
    ["purpose"],
    buckets=(0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 120, 300),
    registry=REGISTRY,
)
llm_retries_total = Counter(
    "ops_agent_llm_retries_total",
    "Transient-failure retry attempts inside the LLM adapter, by purpose.",
    ["purpose"],
    registry=REGISTRY,
)
mcp_tool_calls_total = Counter(
    "ops_agent_mcp_tool_calls_total",
    "MCP tool executions attempted by the executor, by outcome.",
    ["server", "tool", "outcome"],
    registry=REGISTRY,
)
mcp_tool_duration_seconds = Histogram(
    "ops_agent_mcp_tool_duration_seconds",
    "Duration of MCP tool executions, by server and tool.",
    ["server", "tool"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10),
    registry=REGISTRY,
)
circuit_breaker_state = Gauge(
    "ops_agent_circuit_breaker_state",
    "Current per-server circuit breaker state (0=closed, 1=half_open, 2=open).",
    ["server"],
    registry=REGISTRY,
)
circuit_breaker_transitions_total = Counter(
    "ops_agent_circuit_breaker_transitions_total",
    "Per-server circuit breaker state transitions.",
    ["server", "from_state", "to_state"],
    registry=REGISTRY,
)
rate_limit_waits_total = Counter(
    "ops_agent_rate_limit_waits_total",
    "Tool calls that had to wait on the per-server token bucket.",
    ["server"],
    registry=REGISTRY,
)
rate_limit_wait_seconds = Histogram(
    "ops_agent_rate_limit_wait_seconds",
    "Time spent waiting on the per-server token bucket.",
    ["server"],
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30),
    registry=REGISTRY,
)
approvals_total = Counter(
    "ops_agent_approvals_total",
    "Approval lifecycle transitions into each status, by write action.",
    ["tool", "status"],
    registry=REGISTRY,
)
approval_verification_total = Counter(
    "ops_agent_approval_verification_total",
    "Post-write read-back verification outcomes, by write action.",
    ["tool", "ok"],
    registry=REGISTRY,
)

_ALL_COLLECTORS = (
    investigations_total,
    investigation_duration_seconds,
    investigation_budget_exhausted_total,
    llm_calls_total,
    llm_call_duration_seconds,
    llm_retries_total,
    mcp_tool_calls_total,
    mcp_tool_duration_seconds,
    circuit_breaker_state,
    circuit_breaker_transitions_total,
    rate_limit_waits_total,
    rate_limit_wait_seconds,
    approvals_total,
    approval_verification_total,
)


def _label(value: str) -> str:
    """Cardinality guard: bounded, single-line, defensively truncated."""
    return str(value).replace("\n", " ")[:_LABEL_MAX]


def _safe(fn, *args, **kwargs) -> None:
    """Observe without any possibility of changing the observed outcome."""
    try:
        fn(*args, **kwargs)
    except Exception as exc:  # observability must never break the loop
        log.debug("metric_observe_failed", metric=getattr(fn, "__name__", "?"), error=str(exc)[:120])


# -- observation helpers (the only surface other modules should call) ----------

def observe_investigation(status: str, duration_seconds: float) -> None:
    status = status if status in INVESTIGATION_STATUSES else "failed"
    _safe(lambda: investigations_total.labels(status=status).inc())
    _safe(lambda: investigation_duration_seconds.observe(max(float(duration_seconds), 0.0)))


def observe_budget_exhausted(budget_class: str) -> None:
    budget_class = budget_class if budget_class in BUDGET_CLASSES else "llm_calls"
    _safe(lambda: investigation_budget_exhausted_total.labels(budget_class=budget_class).inc())


def observe_llm_call(purpose: str, outcome: str, duration_seconds: float | None) -> None:
    purpose = _label(purpose)
    outcome = outcome if outcome in LLM_OUTCOMES else "error"
    _safe(lambda: llm_calls_total.labels(purpose=purpose, outcome=outcome).inc())
    if duration_seconds is not None:
        _safe(lambda: llm_call_duration_seconds.labels(purpose=purpose).observe(max(float(duration_seconds), 0.0)))


def observe_llm_retry(purpose: str) -> None:
    _safe(lambda: llm_retries_total.labels(purpose=_label(purpose)).inc())


def observe_mcp_tool_call(server: str, tool: str, outcome: str, duration_ms: float) -> None:
    outcome = outcome if outcome in TOOL_OUTCOMES else "error"
    server, tool = _label(server), _label(tool)
    duration_s = max(float(duration_ms), 0.0) / 1000.0
    _safe(lambda: mcp_tool_calls_total.labels(server=server, tool=tool, outcome=outcome).inc())
    _safe(lambda: mcp_tool_duration_seconds.labels(server=server, tool=tool).observe(duration_s))


def set_circuit_state(server: str, state: str) -> None:
    state = state if state in BREAKER_STATES else "open"
    _safe(lambda: circuit_breaker_state.labels(server=_label(server)).set(_BREAKER_STATE_VALUE[state]))


def observe_circuit_transition(server: str, from_state: str, to_state: str) -> None:
    if from_state == to_state:
        return
    _safe(lambda: circuit_breaker_transitions_total.labels(
        server=_label(server), from_state=_label(from_state), to_state=_label(to_state),
    ).inc())
    set_circuit_state(server, to_state)


def observe_rate_limit_wait(server: str, waited_seconds: float) -> None:
    server = _label(server)
    waited = max(float(waited_seconds), 0.0)
    _safe(lambda: rate_limit_waits_total.labels(server=server).inc())
    _safe(lambda: rate_limit_wait_seconds.labels(server=server).observe(waited))


def observe_approval_transition(tool: str, status: str) -> None:
    _safe(lambda: approvals_total.labels(tool=_label(tool), status=_label(status)).inc())


def observe_approval_verification(tool: str, ok: bool) -> None:
    _safe(lambda: approval_verification_total.labels(tool=_label(tool), ok="true" if ok else "false").inc())
