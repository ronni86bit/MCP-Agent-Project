"""Typed loader for backend/mcp/fixtures/scenario.yaml.

The spec is the single source of truth for the canonical storyline timeline
across PostgreSQL, monitoring-mcp, and github-mcp; the validators here keep
the three systems' windows mutually coherent.
"""

from __future__ import annotations

import os
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, model_validator

DEFAULT_SCENARIO_PATH = Path(__file__).resolve().parent / "scenario.yaml"

# Evaluation scenario-variant override (Phase 9 Gate 4). When set, it points
# at an alternate spec file; unset means the canonical scenario, unchanged.
# Subprocess MCP servers receive it through their per-process environment.
VARIANT_ENV_VAR = "OPS_SCENARIO_PATH"


class Window(BaseModel):
    model_config = ConfigDict(extra="forbid")

    start_date: date
    end_date: date

    @model_validator(mode="after")
    def _ordered(self) -> Window:
        if self.end_date < self.start_date:
            raise ValueError(f"window end {self.end_date} precedes start {self.start_date}")
        return self

    @property
    def days(self) -> int:
        return (self.end_date - self.start_date).days + 1


class Incident(Window):
    model_config = ConfigDict(extra="forbid")

    affected_channel: str
    description: str


class Impact(BaseModel):
    model_config = ConfigDict(extra="forbid")

    channel_multipliers: dict[str, list[float]]


class Deployment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repository: str
    service: str
    environment: str
    deployed_at: datetime
    commit_message: str

    @property
    def deployed_at_utc(self) -> datetime:
        return self.deployed_at.astimezone(timezone.utc)


class MonitoringAlert(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    service: str
    metric: str
    severity: str
    fired_at: datetime
    message: str

    @property
    def fired_at_utc(self) -> datetime:
        return self.fired_at.astimezone(timezone.utc)


class MonitoringIncident(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    service: str
    severity: str
    started_at: datetime
    resolved_at: datetime | None
    title: str
    summary: str

    @property
    def started_at_utc(self) -> datetime:
        return self.started_at.astimezone(timezone.utc)

    @property
    def resolved_at_utc(self) -> datetime | None:
        return self.resolved_at.astimezone(timezone.utc) if self.resolved_at else None


class Monitoring(BaseModel):
    model_config = ConfigDict(extra="forbid")

    services: list[str]
    # Optional for evaluation scenario variants (e.g. "deployment without
    # incident"): the canonical scenario always defines both, so canonical
    # parsing is unchanged. Generators treat None as "no such record".
    alert: MonitoringAlert | None = None
    incident: MonitoringIncident | None = None


class ScenarioSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    seed: int
    start_date: date
    end_date: date
    baseline: Window
    incident: Incident
    impact: Impact
    # Optional for evaluation variants ("incident without deployment",
    # "no anomaly"); the canonical scenario always defines it.
    deployment: Deployment | None = None
    monitoring: Monitoring

    @model_validator(mode="after")
    def _coherent(self) -> ScenarioSpec:
        if self.end_date < self.start_date:
            raise ValueError("scenario end_date precedes start_date")
        if self.baseline.start_date < self.start_date or self.baseline.end_date >= self.incident.start_date:
            raise ValueError("baseline window must lie inside the range and before the incident")
        if self.incident.end_date != self.end_date:
            raise ValueError("the incident window must end on the scenario end_date ('today')")
        for channel, multipliers in self.impact.channel_multipliers.items():
            if len(multipliers) != self.incident.days:
                raise ValueError(
                    f"channel_multipliers[{channel!r}] has {len(multipliers)} entries "
                    f"but the incident window is {self.incident.days} days"
                )
        if not self.monitoring.services:
            raise ValueError("monitoring.services must not be empty")
        services = self.monitoring.services
        if self.deployment is not None:
            if self.deployment.service not in services:
                raise ValueError("deployment.service must be one of monitoring.services")
        if self.monitoring.alert is not None:
            if self.monitoring.alert.service not in services:
                raise ValueError("monitoring.alert.service must be one of monitoring.services")
        if self.monitoring.incident is not None:
            if self.monitoring.incident.service not in services:
                raise ValueError("monitoring.incident.service must be one of monitoring.services")

        range_start = datetime.combine(self.start_date, datetime.min.time(), tzinfo=timezone.utc)
        incident_start = datetime.combine(self.incident.start_date, datetime.min.time(), tzinfo=timezone.utc)
        if self.deployment is not None:
            deployed = self.deployment.deployed_at_utc
            if not range_start <= deployed < incident_start:
                raise ValueError("deployment must fall inside the scenario range and before the incident window")
        # the canonical deployment -> alert -> incident ordering is enforced
        # whenever the involved records exist; variants may drop earlier links
        if self.deployment is not None and self.monitoring.alert is not None:
            if not self.deployment.deployed_at_utc < self.monitoring.alert.fired_at_utc:
                raise ValueError("timeline must run deployment -> alert -> incident open")
        if self.monitoring.alert is not None and self.monitoring.incident is not None:
            if not self.monitoring.alert.fired_at_utc <= self.monitoring.incident.started_at_utc:
                raise ValueError("timeline must run deployment -> alert -> incident open")
        if self.monitoring.incident is not None:
            resolved = self.monitoring.incident.resolved_at_utc
            if resolved is not None and resolved <= self.monitoring.incident.started_at_utc:
                raise ValueError("incident resolution must follow the incident start")
        return self

    @property
    def days(self) -> int:
        return (self.end_date - self.start_date).days + 1

    @property
    def incident_start_at_utc(self) -> datetime:
        return datetime.combine(self.incident.start_date, datetime.min.time(), tzinfo=timezone.utc)

    def incident_day_multipliers(self, channel: str, day_offset: int) -> float:
        return self.impact.channel_multipliers[channel][day_offset]


def load_scenario(path: str | Path | None = None) -> ScenarioSpec:
    """Load a scenario spec. Resolution order: explicit `path` argument, then
    the OPS_SCENARIO_PATH environment variable (evaluation variants; read at
    call time so scoped overrides work in-process and subprocess servers
    inherit it via their environment), then the canonical scenario.yaml."""
    if path is None:
        path = os.environ.get(VARIANT_ENV_VAR) or DEFAULT_SCENARIO_PATH
    source = Path(path)
    raw: dict[str, Any] = yaml.safe_load(source.read_text(encoding="utf-8"))["scenario"]
    return ScenarioSpec.model_validate(raw)
