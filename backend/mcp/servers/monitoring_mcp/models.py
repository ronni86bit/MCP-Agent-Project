"""Typed inputs/outputs for the monitoring-mcp tools."""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

MAX_SERIES_WINDOW_DAYS = 14      # hourly time series
MAX_RECORDS_WINDOW_DAYS = 31     # daily rollups, alerts, incidents
MAX_COMPARE_WINDOW_DAYS = 31

Metric = Literal["error_rate", "latency_p95_ms", "requests_per_min"]
Severity = Literal["info", "warning", "high", "critical"]


def _iso(value: str, field: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise ValueError(f"{field} must be an ISO date (YYYY-MM-DD), got {value!r}") from None


class _Window(BaseModel):
    model_config = ConfigDict(extra="forbid")

    window_start: str
    window_end: str
    max_days: int = MAX_RECORDS_WINDOW_DAYS

    @model_validator(mode="after")
    def _ordered(self) -> "_Window":
        start, end = _iso(self.window_start, "window_start"), _iso(self.window_end, "window_end")
        if end < start:
            raise ValueError(f"window_end {self.window_end!r} precedes window_start {self.window_start!r}")
        if (end - start).days + 1 > self.max_days:
            raise ValueError(f"window exceeds {self.max_days} days")
        return self

    @property
    def start(self) -> date:
        return _iso(self.window_start, "window_start")

    @property
    def end(self) -> date:
        return _iso(self.window_end, "window_end")


class ServiceMetricsInputs(_Window):
    max_days: int = MAX_SERIES_WINDOW_DAYS
    service: str = Field(min_length=1, max_length=60)
    limit: int = Field(default=200, ge=1, le=400)


class ErrorRateInputs(_Window):
    service: str = Field(min_length=1, max_length=60)


class IncidentsInputs(_Window):
    service: str | None = Field(default=None, max_length=60)


class AlertsInputs(_Window):
    service: str | None = Field(default=None, max_length=60)
    severity: Severity | None = None
    limit: int = Field(default=50, ge=1, le=100)


class AcknowledgeAlertInputs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    alert_id: str = Field(min_length=1, max_length=40)
    acknowledged_by: str = Field(min_length=1, max_length=80)
    note: str | None = Field(default=None, max_length=200)

    @model_validator(mode="after")
    def _strip_identity(self) -> "AcknowledgeAlertInputs":
        if not self.acknowledged_by.strip():
            raise ValueError("acknowledged_by must be a non-blank operator label")
        self.acknowledged_by = self.acknowledged_by.strip()
        return self


class CompareMetricsInputs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    service: str = Field(min_length=1, max_length=60)
    metric: Metric
    window_a_start: str
    window_a_end: str
    window_b_start: str
    window_b_end: str

    @model_validator(mode="after")
    def _windows(self) -> "CompareMetricsInputs":
        lengths: dict[str, int] = {}
        for prefix in ("window_a", "window_b"):
            start = _iso(getattr(self, f"{prefix}_start"), f"{prefix}_start")
            end = _iso(getattr(self, f"{prefix}_end"), f"{prefix}_end")
            if end < start:
                raise ValueError(f"{prefix} end precedes its start")
            length = (end - start).days + 1
            if length > MAX_COMPARE_WINDOW_DAYS:
                raise ValueError(f"{prefix} window exceeds {MAX_COMPARE_WINDOW_DAYS} days")
            lengths[prefix] = length
        if lengths["window_a"] != lengths["window_b"]:
            raise ValueError(
                "compare_metric_windows requires equal-duration windows: window_a spans "
                f"{lengths['window_a']} day(s) but window_b spans {lengths['window_b']} day(s). "
                "Metric averages are only comparable between windows of the same length."
            )
        return self

    @property
    def a_start(self) -> date:
        return _iso(self.window_a_start, "window_a_start")

    @property
    def a_end(self) -> date:
        return _iso(self.window_a_end, "window_a_end")

    @property
    def b_start(self) -> date:
        return _iso(self.window_b_start, "window_b_start")

    @property
    def b_end(self) -> date:
        return _iso(self.window_b_end, "window_b_end")


class MetricPoint(BaseModel):
    timestamp: str
    error_rate: float
    latency_p95_ms: float
    requests_per_min: int


class DailyErrorRate(BaseModel):
    date: str
    avg_pct: float
    peak_pct: float
    peak_at: str


class IncidentRecord(BaseModel):
    id: str
    service: str
    severity: str
    started_at: str
    resolved_at: str | None
    title: str
    summary: str


class AlertRecord(BaseModel):
    id: str
    service: str
    metric: str
    severity: str
    fired_at: str
    message: str
    acknowledged: bool = False
    acknowledged_at: str | None = None
    acknowledged_by: str | None = None


class WindowStat(BaseModel):
    avg: float
    peak: float
    peak_at: str
