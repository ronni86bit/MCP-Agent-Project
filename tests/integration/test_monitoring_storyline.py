"""Cross-system storyline coherence: scenario.yaml drives PostgreSQL, the
monitoring fixtures, and the GitHub deployment, and the timelines must agree.

Pure fixture/spec tests (no MCP round-trips); the MCP-level behavior is
covered in test_monitoring_mcp.py.
"""

import statistics
from datetime import datetime, timezone

from backend.mcp.fixtures.loader import load_scenario
from backend.mcp.fixtures.monitoring_seed import build_monitoring_fixtures


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def _fixtures():
    return build_monitoring_fixtures(load_scenario())


def test_deployment_exists_in_spec_and_precedes_degradation():
    spec = load_scenario()
    deployed = spec.deployment.deployed_at_utc
    assert spec.deployment.service == "checkout"
    assert deployed < spec.monitoring.alert.fired_at_utc <= spec.monitoring.incident.started_at_utc
    # "shortly before": same day as the sales-decline window start or the day before
    gap = spec.incident_start_at_utc - deployed
    assert timedelta_hours(gap) <= 30


def timedelta_hours(delta) -> float:
    return delta.total_seconds() / 3600


def test_checkout_baseline_is_healthy_before_deployment():
    spec = load_scenario()
    fixtures = _fixtures()
    deployed = spec.deployment.deployed_at_utc
    pre = [
        point["error_rate"]
        for point in fixtures.series["checkout"]
        if _parse(point["timestamp"]) < deployed
    ]
    assert statistics.mean(pre) < 2.0, "checkout must be healthy before the deployment"


def test_error_rate_rises_after_the_deployment():
    spec = load_scenario()
    fixtures = _fixtures()
    deployed = spec.deployment.deployed_at_utc
    pre = [p["error_rate"] for p in fixtures.series["checkout"] if _parse(p["timestamp"]) < deployed]
    post = [p["error_rate"] for p in fixtures.series["checkout"] if _parse(p["timestamp"]) >= deployed]
    assert min(post) > statistics.mean(pre) * 2, "degradation must start immediately after deployment"
    assert statistics.mean(post) > statistics.mean(pre) * 8


def test_other_services_stay_healthy():
    spec = load_scenario()
    fixtures = _fixtures()
    deployed = spec.deployment.deployed_at_utc
    for service in fixtures.services:
        if service == spec.deployment.service:
            continue
        post = [p["error_rate"] for p in fixtures.series[service] if _parse(p["timestamp"]) >= deployed]
        assert statistics.mean(post) < 2.0, f"{service} must stay healthy"


def test_alert_fires_after_degradation_and_incident_opens():
    spec = load_scenario()
    fixtures = _fixtures()
    deployed = spec.deployment.deployed_at_utc
    canonical_alert = next(a for a in fixtures.alerts if a["id"] == spec.monitoring.alert.id)
    assert _parse(canonical_alert["fired_at"]) > deployed, "alert must follow the degradation"
    canonical_incident = next(i for i in fixtures.incidents if i["id"] == spec.monitoring.incident.id)
    assert _parse(canonical_incident["started_at"]) >= _parse(canonical_alert["fired_at"])


def test_incident_overlaps_the_sales_decline_window():
    spec = load_scenario()
    fixtures = _fixtures()
    canonical_incident = next(i for i in fixtures.incidents if i["id"] == spec.monitoring.incident.id)
    started = _parse(canonical_incident["started_at"])
    resolved = _parse(canonical_incident["resolved_at"]) if canonical_incident["resolved_at"] else None
    window_start = datetime.combine(spec.incident.start_date, datetime.min.time(), tzinfo=timezone.utc)
    window_end = window_start + timedelta_days(spec.incident.days)
    # overlap: incident began inside (or before) the sales window and is not
    # resolved before the window opens
    assert started <= window_end
    assert resolved is None or resolved >= window_start


def timedelta_days(n: int):
    from datetime import timedelta

    return timedelta(days=n)


def test_fixture_generation_is_deterministic():
    first, second = _fixtures(), _fixtures()
    assert first.model_dump() == second.model_dump()
