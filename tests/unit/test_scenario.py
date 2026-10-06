"""Scenario spec validation and seed-generator determinism (pure, no DB)."""

import statistics
from datetime import date

import pytest
from pydantic import ValidationError

from backend.mcp.fixtures.loader import ScenarioSpec, load_scenario
from backend.mcp.fixtures.sales_seed import build_sales_rows, checksum


def test_default_scenario_loads():
    spec = load_scenario()
    assert spec.name == "checkout_incident_v1"
    assert spec.seed == 42
    assert spec.days == 90
    assert spec.incident.days == 3
    assert spec.baseline.end_date < spec.incident.start_date
    assert spec.incident.end_date == spec.end_date


def _respec(**overrides) -> ScenarioSpec:
    spec = load_scenario()
    raw = spec.model_dump()
    raw.update(overrides)
    return ScenarioSpec.model_validate(raw)


def test_incident_must_end_on_scenario_end():
    with pytest.raises(ValidationError):
        _respec(end_date=date(2026, 10, 4))


def test_baseline_must_end_before_incident():
    spec = load_scenario()
    with pytest.raises(ValidationError):
        _respec(
            baseline={
                "start_date": date(2026, 10, 3),
                "end_date": date(2026, 10, 4),
            }
        )


def test_multiplier_count_must_match_incident_days():
    spec = load_scenario()
    impact = spec.impact.model_dump()
    impact["channel_multipliers"]["mobile"] = [0.5]
    with pytest.raises(ValidationError):
        _respec(impact=impact)


def test_generator_is_deterministic():
    spec = load_scenario()
    first, second = build_sales_rows(spec), build_sales_rows(spec)
    assert checksum(first) == checksum(second)
    assert first == second


def test_generator_shape():
    spec = load_scenario()
    rows = build_sales_rows(spec)
    assert len(rows) == 90 * 8 * 4 * 2  # days x products x regions x channels
    dates = {row["sale_date"] for row in rows}
    assert min(dates) == spec.start_date
    assert max(dates) == spec.end_date
    assert {row["channel"] for row in rows} == {"mobile", "desktop"}
    assert all(0 <= row["orders"] <= row["units"] for row in rows)


def _daily_revenue(rows, start, end, **match):
    daily: dict[date, float] = {}
    for row in rows:
        if not (start <= row["sale_date"] <= end):
            continue
        if any(row[key] != value for key, value in match.items()):
            continue
        daily[row["sale_date"]] = daily.get(row["sale_date"], 0.0) + float(row["revenue"])
    return daily


def test_storyline_in_generated_data():
    spec = load_scenario()
    rows = build_sales_rows(spec)
    b = spec.baseline
    inc = spec.incident

    baseline_daily = _daily_revenue(rows, b.start_date, b.end_date).values()
    cv = statistics.stdev(baseline_daily) / statistics.mean(baseline_daily)
    assert cv < 0.12, "baseline must be a stable, healthy period"

    baseline_avg = statistics.mean(baseline_daily)
    incident_daily = _daily_revenue(rows, inc.start_date, inc.end_date).values()
    incident_avg = statistics.mean(incident_daily)
    total_decline = incident_avg / baseline_avg - 1
    assert total_decline <= -0.20, "the decline must be clearly measurable"

    mobile = _daily_revenue(rows, inc.start_date, inc.end_date, channel="mobile")
    desktop = _daily_revenue(rows, inc.start_date, inc.end_date, channel="desktop")
    mobile_base = _daily_revenue(rows, b.start_date, b.end_date, channel="mobile")
    desktop_base = _daily_revenue(rows, b.start_date, b.end_date, channel="desktop")
    mobile_decline = statistics.mean(mobile.values()) / statistics.mean(mobile_base.values()) - 1
    desktop_decline = statistics.mean(desktop.values()) / statistics.mean(desktop_base.values()) - 1
    assert mobile_decline <= -0.40, "mobile (affected channel) must collapse"
    assert -0.12 <= desktop_decline < 0, "desktop must stay near-healthy"
    assert abs(mobile_decline) >= 5 * abs(desktop_decline), "decline must concentrate in mobile"

    for region_id in (1, 2, 3, 4):
        region_inc = _daily_revenue(rows, inc.start_date, inc.end_date, region_id=region_id)
        region_base = _daily_revenue(rows, b.start_date, b.end_date, region_id=region_id)
        decline = statistics.mean(region_inc.values()) / statistics.mean(region_base.values()) - 1
        assert decline <= -0.15, f"region {region_id} must also decline (global checkout impact)"
