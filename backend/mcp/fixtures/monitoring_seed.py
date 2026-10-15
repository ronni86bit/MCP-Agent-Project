"""Deterministic monitoring fixtures generated from scenario.yaml.

`build_monitoring_fixtures` is a pure function of the scenario spec: hourly
metric points per service for the whole scenario range, plus the alert and
incident records. The storyline it encodes (same timeline as the sales data):

    healthy checkout baseline
        -> checkout deployment (spec.deployment.deployed_at)
        -> checkout error rate ramps up, latency degrades
        -> alert fires (spec.monitoring.alert.fired_at)
        -> incident opens (spec.monitoring.incident.started_at)
        -> degradation overlaps the sales-decline window

Other services stay healthy so cross-service contrast is visible.
`scripts/seed_monitoring.py` writes the fixtures as JSON for inspection; the
server regenerates the identical data in memory from the spec.
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone
from typing import Any

from pydantic import BaseModel

from backend.mcp.fixtures.loader import ScenarioSpec, load_scenario

METRICS = ("error_rate", "latency_p95_ms", "requests_per_min")

# per-service baseline profile: (base requests_per_min, base latency_p95_ms, base error pct)
_SERVICE_PROFILE: dict[str, tuple[int, float, float]] = {
    "checkout": (900, 180.0, 0.55),
    "payment": (700, 140.0, 0.35),
    "catalog": (1200, 90.0, 0.20),
    "web": (2500, 120.0, 0.25),
}


class MonitoringFixtures(BaseModel):
    services: list[str]
    series: dict[str, list[dict[str, Any]]]
    alerts: list[dict[str, Any]]
    incidents: list[dict[str, Any]]


def _hourly_points(spec: ScenarioSpec, service: str, rng: random.Random) -> list[dict[str, Any]]:
    profile = _SERVICE_PROFILE.get(service, (600, 120.0, 0.3))
    deployed = spec.deployment.deployed_at_utc if service == spec.deployment.service else None
    points: list[dict[str, Any]] = []
    current = datetime.combine(spec.start_date, datetime.min.time(), tzinfo=timezone.utc)
    end = datetime.combine(spec.end_date, datetime.min.time(), tzinfo=timezone.utc) + timedelta(days=1)
    while current < end:
        hour = current.hour
        # diurnal traffic shape: quiet nights, busy afternoons/evenings
        traffic_factor = 0.55 + 0.45 * (0.5 + 0.5 * _bump(hour))
        rpm = int(profile[0] * traffic_factor * rng.uniform(0.94, 1.06))
        latency = profile[1] * rng.uniform(0.9, 1.1)
        error_pct = profile[2] * rng.uniform(0.6, 1.5)

        if deployed is not None and current >= deployed:
            hours_since = (current - deployed).total_seconds() / 3600
            ramp = min(1.0, hours_since / 6.0)          # full degradation within ~6h
            error_pct = 0.55 + (9.5 + rng.uniform(-1.2, 1.8)) * ramp
            latency = profile[1] + (420.0 + rng.uniform(-40, 70)) * ramp
            rpm = int(rpm * (1.0 - 0.10 * ramp))         # failed checkouts depress traffic

        points.append(
            {
                "timestamp": current.isoformat().replace("+00:00", "Z"),
                "error_rate": round(max(0.01, error_pct), 3),
                "latency_p95_ms": round(max(5.0, latency), 1),
                "requests_per_min": max(0, rpm),
            }
        )
        current += timedelta(hours=1)
    return points


def _bump(hour: int) -> float:
    """0..1 diurnal curve peaking around 19:00 UTC."""
    return max(0.0, 1.0 - abs(hour - 19) / 10.0)


def build_monitoring_fixtures(spec: ScenarioSpec) -> MonitoringFixtures:
    series: dict[str, list[dict[str, Any]]] = {}
    for index, service in enumerate(spec.monitoring.services):
        rng = random.Random(spec.seed + 1000 + index)
        series[service] = _hourly_points(spec, service, rng)

    alert = spec.monitoring.alert
    incident = spec.monitoring.incident
    def _alert(record_id, service, metric, severity, fired_at, message):
        # canonical fixtures always start unacknowledged; acknowledgement is a
        # runtime write against the in-memory store and never mutates these files
        return {
            "id": record_id,
            "service": service,
            "metric": metric,
            "severity": severity,
            "fired_at": fired_at,
            "message": message,
            "acknowledged": False,
            "acknowledged_at": None,
            "acknowledged_by": None,
        }

    alerts = [
        _alert("ALT-1044", "catalog", "latency_p95_ms", "warning",
               "2026-09-15T09:12:00Z", "catalog latency_p95_ms above 150ms for 20m"),
        _alert(alert.id, alert.service, alert.metric, alert.severity,
               alert.fired_at_utc.isoformat().replace("+00:00", "Z"), alert.message),
    ]
    incidents = [
        {
            "id": "INC-0987",
            "service": "catalog",
            "severity": "low",
            "started_at": "2026-09-15T09:20:00Z",
            "resolved_at": "2026-09-15T13:40:00Z",
            "title": "Catalog latency elevated",
            "summary": "Sustained catalog latency after a cache flush; resolved after warmup.",
        },
        {
            "id": incident.id,
            "service": incident.service,
            "severity": incident.severity,
            "started_at": incident.started_at_utc.isoformat().replace("+00:00", "Z"),
            "resolved_at": (
                incident.resolved_at_utc.isoformat().replace("+00:00", "Z")
                if incident.resolved_at_utc else None
            ),
            "title": incident.title,
            "summary": incident.summary,
        },
    ]
    return MonitoringFixtures(
        services=list(spec.monitoring.services), series=series, alerts=alerts, incidents=incidents
    )


def load_fixtures(spec: ScenarioSpec | None = None) -> MonitoringFixtures:
    return build_monitoring_fixtures(spec or load_scenario())
