"""Input-model validation for the monitoring-mcp tools (no fixtures needed)."""

import pytest
from pydantic import ValidationError

from backend.mcp.servers.monitoring_mcp.models import (
    AlertsInputs,
    CompareMetricsInputs,
    ServiceMetricsInputs,
)


def test_valid_service_metrics_inputs():
    inp = ServiceMetricsInputs(
        service="checkout", window_start="2026-10-03", window_end="2026-10-05"
    )
    assert inp.limit == 200


def test_service_metrics_window_capped_at_14_days():
    with pytest.raises(ValidationError):
        ServiceMetricsInputs(
            service="checkout", window_start="2026-09-01", window_end="2026-09-30"
        )


def test_bad_dates_and_reversed_windows_rejected():
    with pytest.raises(ValidationError):
        ServiceMetricsInputs(service="checkout", window_start="junk", window_end="2026-10-05")
    with pytest.raises(ValidationError):
        AlertsInputs(window_start="2026-10-05", window_end="2026-10-01")


def test_severity_enum_enforced():
    with pytest.raises(ValidationError):
        AlertsInputs(
            window_start="2026-10-03", window_end="2026-10-05", severity="catastrophic"
        )
    assert AlertsInputs(
        window_start="2026-10-03", window_end="2026-10-05", severity="high"
    ).severity == "high"


def test_compare_requires_equal_duration_windows():
    with pytest.raises(ValidationError) as excinfo:
        CompareMetricsInputs(
            service="checkout", metric="error_rate",
            window_a_start="2026-09-06", window_a_end="2026-10-02",  # 27 days
            window_b_start="2026-10-03", window_b_end="2026-10-05",  # 3 days
        )
    assert "equal-duration" in str(excinfo.value)


def test_compare_accepts_equal_windows_and_known_metric():
    inp = CompareMetricsInputs(
        service="checkout", metric="latency_p95_ms",
        window_a_start="2026-09-30", window_a_end="2026-10-02",
        window_b_start="2026-10-03", window_b_end="2026-10-05",
    )
    assert inp.metric == "latency_p95_ms"


def test_compare_rejects_unknown_metric():
    with pytest.raises(ValidationError):
        CompareMetricsInputs(
            service="checkout", metric="happiness",
            window_a_start="2026-09-30", window_a_end="2026-10-02",
            window_b_start="2026-10-03", window_b_end="2026-10-05",
        )


def test_unknown_fields_rejected():
    with pytest.raises(ValidationError):
        ServiceMetricsInputs(
            service="checkout", window_start="2026-10-03", window_end="2026-10-05",
            query="DROP TABLE",
        )
