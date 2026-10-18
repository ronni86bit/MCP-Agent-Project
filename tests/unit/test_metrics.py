"""Phase 8 Gate B: metrics module contract and instrumentation safety.

Pure unit tests over the private registry — no Postgres, no subprocesses.
All assertions use before/after deltas so tests are order-independent.
"""

from prometheus_client import generate_latest

from backend.observability import metrics
from backend.observability.metrics import REGISTRY


def _sample(name: str, labels: dict | None = None) -> float | None:
    return REGISTRY.get_sample_value(name, labels or {})


def _delta(name: str, labels: dict | None, fn, *args, **kwargs) -> float:
    before = _sample(name, labels) or 0.0
    fn(*args, **kwargs)
    after = _sample(name, labels) or 0.0
    return after - before


# -- contract -----------------------------------------------------------------


def test_all_fourteen_metric_families_are_registered():
    text = generate_latest(REGISTRY).decode()
    expected = {
        "ops_agent_investigations_total",
        "ops_agent_investigation_duration_seconds",
        "ops_agent_investigation_budget_exhausted_total",
        "ops_agent_llm_calls_total",
        "ops_agent_llm_call_duration_seconds",
        "ops_agent_llm_retries_total",
        "ops_agent_mcp_tool_calls_total",
        "ops_agent_mcp_tool_duration_seconds",
        "ops_agent_circuit_breaker_state",
        "ops_agent_circuit_breaker_transitions_total",
        "ops_agent_rate_limit_waits_total",
        "ops_agent_rate_limit_wait_seconds",
        "ops_agent_approvals_total",
        "ops_agent_approval_verification_total",
    }
    for family in expected:
        assert family in text, f"metric family {family} missing from registry"


def test_metric_names_use_ops_agent_prefix_exclusively():
    text = generate_latest(REGISTRY).decode()
    families = {
        line.split()[0].split("{")[0]
        for line in text.splitlines()
        if line and not line.startswith("#")
    }
    assert families, "registry exported nothing"
    assert all(name.startswith("ops_agent_") for name in families)


# -- bounded labels -----------------------------------------------------------


def test_unknown_investigation_status_coerces_to_failed():
    delta = _delta(
        "ops_agent_investigations_total", {"status": "failed"},
        metrics.observe_investigation, "bogus-status", 0.5,
    )
    assert delta == 1.0


def test_unknown_llm_outcome_coerces_to_error():
    delta = _delta(
        "ops_agent_llm_calls_total",
        {"purpose": "plan", "outcome": "error"},
        metrics.observe_llm_call, "plan", "catastrophic-meltdown", None,
    )
    assert delta == 1.0
    assert _sample("ops_agent_llm_calls_total", {"purpose": "plan", "outcome": "catastrophic-meltdown"}) is None


def test_unknown_tool_outcome_and_budget_class_coerce():
    tool_delta = _delta(
        "ops_agent_mcp_tool_calls_total",
        {"server": "postgres-mcp", "tool": "get_daily_sales", "outcome": "error"},
        metrics.observe_mcp_tool_call, "postgres-mcp", "get_daily_sales", "weird", 1.0,
    )
    budget_delta = _delta(
        "ops_agent_investigation_budget_exhausted_total", {"budget_class": "llm_calls"},
        metrics.observe_budget_exhausted, "mystery_budget",
    )
    assert tool_delta == 1.0
    assert budget_delta == 1.0


def test_label_values_are_truncated_defensively():
    assert len(metrics._label("x" * 500)) == 80
    assert "\n" not in metrics._label("bad\nlabel")
    server = "s" * 300
    tool = "t" * 300
    metrics.observe_mcp_tool_call(server, tool, "ok", 1.0)
    assert _sample(
        "ops_agent_mcp_tool_calls_total",
        {"server": "s" * 80, "tool": "t" * 80, "outcome": "ok"},
    ) is not None


def test_no_free_text_ever_becomes_an_investigation_status_label():
    metrics.observe_investigation("running", 0.1)
    metrics.observe_investigation("failed: wall clock", 0.1)
    assert _sample("ops_agent_investigations_total", {"status": "running"}) is None
    assert _sample("ops_agent_investigations_total", {"status": "failed: wall clock"}) is None


# -- breaker gauge encoding -----------------------------------------------------


def test_circuit_state_gauge_encoding():
    server = "gauge-srv"
    metrics.set_circuit_state(server, "closed")
    assert _sample("ops_agent_circuit_breaker_state", {"server": server}) == 0.0
    metrics.set_circuit_state(server, "half_open")
    assert _sample("ops_agent_circuit_breaker_state", {"server": server}) == 1.0
    metrics.set_circuit_state(server, "open")
    assert _sample("ops_agent_circuit_breaker_state", {"server": server}) == 2.0
    metrics.set_circuit_state(server, "garbage")
    assert _sample("ops_agent_circuit_breaker_state", {"server": server}) == 2.0


def test_same_state_transition_records_nothing():
    metrics.observe_circuit_transition("noop-srv", "closed", "closed")
    assert _sample(
        "ops_agent_circuit_breaker_transitions_total",
        {"server": "noop-srv", "from_state": "closed", "to_state": "closed"},
    ) is None


def test_transition_also_refreshes_the_gauge():
    server = "trans-srv"
    metrics.observe_circuit_transition(server, "half_open", "closed")
    assert _sample(
        "ops_agent_circuit_breaker_transitions_total",
        {"server": server, "from_state": "half_open", "to_state": "closed"},
    ) == 1.0
    assert _sample("ops_agent_circuit_breaker_state", {"server": server}) == 0.0


# -- non-invasive safety --------------------------------------------------------


def test_helpers_swallow_internal_failures(monkeypatch):
    class _Exploding:
        def labels(self, *a, **kw):
            raise RuntimeError("collector exploded")

        def observe(self, *a, **kw):
            raise RuntimeError("collector exploded")

    monkeypatch.setattr(metrics, "investigations_total", _Exploding())
    monkeypatch.setattr(metrics, "investigation_duration_seconds", _Exploding())
    # neither observation may raise, and later helpers keep working
    metrics.observe_investigation("complete", 1.0)
    metrics.observe_llm_call("plan", "ok", 0.25)
    assert _delta(
        "ops_agent_llm_calls_total", {"purpose": "plan", "outcome": "ok"},
        metrics.observe_llm_call, "plan", "ok", 0.25,
    ) == 1.0


def test_observation_does_not_mutate_the_reported_value():
    payload = {"duration_ms": 12.5}
    metrics.observe_mcp_tool_call("postgres-mcp", "get_daily_sales", "ok", payload["duration_ms"])
    assert payload == {"duration_ms": 12.5}


def test_negative_durations_are_clamped_not_rejected():
    metrics.observe_investigation("complete", -5.0)
    metrics.observe_rate_limit_wait("postgres-mcp", -1.0)
    # reaching here without ValueError from prometheus-client is the assertion
