"""Typed loader for backend/mcp/fixtures/scenario.yaml."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, model_validator

DEFAULT_SCENARIO_PATH = Path(__file__).resolve().parent / "scenario.yaml"


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


class ScenarioSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    seed: int
    start_date: date
    end_date: date
    baseline: Window
    incident: Incident
    impact: Impact

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
        return self

    @property
    def days(self) -> int:
        return (self.end_date - self.start_date).days + 1

    def incident_day_multipliers(self, channel: str, day_offset: int) -> float:
        return self.impact.channel_multipliers[channel][day_offset]


def load_scenario(path: str | Path | None = None) -> ScenarioSpec:
    source = Path(path) if path is not None else DEFAULT_SCENARIO_PATH
    raw: dict[str, Any] = yaml.safe_load(source.read_text(encoding="utf-8"))["scenario"]
    return ScenarioSpec.model_validate(raw)
