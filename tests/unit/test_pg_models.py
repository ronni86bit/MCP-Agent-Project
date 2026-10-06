"""Input-model validation for the postgres-mcp tools (no DB required)."""

import pytest
from pydantic import ValidationError

from backend.mcp.servers.postgres_mcp.models import (
    CompareInputs,
    DailySalesInputs,
    ProductSalesInputs,
    TrendInputs,
)


def test_valid_daily_inputs_pass_with_defaults():
    inp = DailySalesInputs(start_date="2026-09-01", end_date="2026-09-30")
    assert inp.limit == 90
    assert inp.product is None and inp.region is None and inp.channel is None


def test_bad_date_format_rejected():
    with pytest.raises(ValidationError):
        DailySalesInputs(start_date="10/01/2026", end_date="2026-10-05")


def test_reversed_range_rejected():
    with pytest.raises(ValidationError):
        DailySalesInputs(start_date="2026-10-05", end_date="2026-10-01")


def test_range_over_180_days_rejected():
    with pytest.raises(ValidationError):
        DailySalesInputs(start_date="2025-01-01", end_date="2025-12-31")


def test_limit_bounds_enforced():
    for bad in (0, -1, 401):
        with pytest.raises(ValidationError):
            DailySalesInputs(start_date="2026-09-01", end_date="2026-09-30", limit=bad)
    assert DailySalesInputs(start_date="2026-09-01", end_date="2026-09-30", limit=400).limit == 400


def test_product_limit_caps_at_100():
    with pytest.raises(ValidationError):
        ProductSalesInputs(start_date="2026-09-01", end_date="2026-09-30", limit=101)


def test_unknown_fields_rejected():
    with pytest.raises(ValidationError):
        DailySalesInputs(
            start_date="2026-09-01", end_date="2026-09-30", sql="DROP TABLE sales_data.products"
        )


def test_trend_granularity_is_limited_to_day_or_week():
    with pytest.raises(ValidationError):
        TrendInputs(start_date="2026-09-01", end_date="2026-09-30", granularity="month")
    assert TrendInputs(start_date="2026-09-01", end_date="2026-09-30", granularity="week")


def test_compare_window_over_92_days_rejected():
    with pytest.raises(ValidationError):
        CompareInputs(
            window_a_start="2025-01-01", window_a_end="2025-12-31",
            window_b_start="2026-09-06", window_b_end="2026-10-02",
        )


def test_compare_reversed_window_rejected():
    with pytest.raises(ValidationError):
        CompareInputs(
            window_a_start="2026-10-05", window_a_end="2026-10-01",
            window_b_start="2026-09-06", window_b_end="2026-10-02",
        )


def test_compare_bad_metric_rejected():
    with pytest.raises(ValidationError):
        CompareInputs(
            window_a_start="2026-09-28", window_a_end="2026-09-30",
            window_b_start="2026-10-03", window_b_end="2026-10-05",
            metric="sales",
        )
