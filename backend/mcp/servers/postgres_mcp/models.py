"""Typed inputs for the postgres-mcp tools.

Validation happens here, before any SQL runs: strict ISO dates, ordered
windows, bounded window lengths, and hard row-limit caps. Extra/unknown
fields are rejected.
"""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

MAX_RANGE_DAYS = 180
MAX_COMPARE_WINDOW_DAYS = 92

Metric = Literal["revenue", "orders", "units"]
GroupBy = Literal["none", "product", "region", "channel"]


def _iso(value: str, field: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise ValueError(f"{field} must be an ISO date (YYYY-MM-DD), got {value!r}") from None


class _FilteredRange(BaseModel):
    model_config = ConfigDict(extra="forbid")

    start_date: str
    end_date: str
    product: str | None = Field(default=None, max_length=80)
    region: str | None = Field(default=None, max_length=80)
    channel: str | None = Field(default=None, max_length=80)

    @model_validator(mode="after")
    def _ordered_window(self) -> "_FilteredRange":
        start, end = _iso(self.start_date, "start_date"), _iso(self.end_date, "end_date")
        if end < start:
            raise ValueError(f"end_date {self.end_date!r} precedes start_date {self.start_date!r}")
        if (end - start).days + 1 > MAX_RANGE_DAYS:
            raise ValueError(f"date range exceeds {MAX_RANGE_DAYS} days")
        return self

    @property
    def start(self) -> date:
        return _iso(self.start_date, "start_date")

    @property
    def end(self) -> date:
        return _iso(self.end_date, "end_date")


class DailySalesInputs(_FilteredRange):
    limit: int = Field(default=90, ge=1, le=400)


class ProductSalesInputs(_FilteredRange):
    limit: int = Field(default=20, ge=1, le=100)


class RegionSalesInputs(_FilteredRange):
    limit: int = Field(default=10, ge=1, le=100)


class TrendInputs(_FilteredRange):
    granularity: Literal["day", "week"] = "day"
    limit: int = Field(default=200, ge=1, le=400)


class CompareInputs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    window_a_start: str
    window_a_end: str
    window_b_start: str
    window_b_end: str
    metric: Metric = "revenue"
    group_by: GroupBy = "none"
    product: str | None = Field(default=None, max_length=80)
    region: str | None = Field(default=None, max_length=80)
    channel: str | None = Field(default=None, max_length=80)

    @model_validator(mode="after")
    def _windows(self) -> "CompareInputs":
        for prefix in ("window_a", "window_b"):
            start = _iso(getattr(self, f"{prefix}_start"), f"{prefix}_start")
            end = _iso(getattr(self, f"{prefix}_end"), f"{prefix}_end")
            if end < start:
                raise ValueError(f"{prefix} end precedes its start")
            if (end - start).days + 1 > MAX_COMPARE_WINDOW_DAYS:
                raise ValueError(f"{prefix} window exceeds {MAX_COMPARE_WINDOW_DAYS} days")
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
