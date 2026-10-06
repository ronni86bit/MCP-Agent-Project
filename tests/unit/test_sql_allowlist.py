"""Static guarantees over the postgres-mcp SQL allowlist."""

import re
from pathlib import Path

from backend.mcp.servers.postgres_mcp import queries

FORBIDDEN = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|TRUNCATE|GRANT|REVOKE|COPY|VACUUM)\b", re.I
)
EXPECTED_TEMPLATES = {
    "get_daily_sales",
    "get_product_sales",
    "get_region_sales",
    "get_sales_trend.day",
    "get_sales_trend.week",
    "compare_sales.total",
    "compare_sales.by_product",
    "compare_sales.by_region",
    "compare_sales.by_channel",
}


def test_registry_covers_exactly_the_expected_templates():
    assert set(queries.QUERY_REGISTRY) == EXPECTED_TEMPLATES


def test_every_template_is_a_single_select():
    for name, sql in queries.QUERY_REGISTRY.items():
        assert re.match(r"\s*(SELECT|WITH)\b", sql, re.I), name
        assert not FORBIDDEN.search(sql), name
        assert sql.count(";") <= 1, name


def test_placeholders_are_numbered_parameters_only():
    for name, sql in queries.QUERY_REGISTRY.items():
        assert not re.search(r"\{\}|\%[sd]|\.format\(", sql), name
        for token in re.findall(r"\$\w+", sql):
            assert re.fullmatch(r"\$\d+", token), (name, token)
            assert 1 <= int(token[1:]) <= 6


def test_sql_lives_only_in_the_queries_module():
    # match SQL-shaped statements (SELECT ... FROM), not prose mentions in docstrings
    sql_statement = re.compile(r"\bSELECT\b[^;]*\bFROM\b", re.I)
    package_dir = Path(queries.__file__).parent
    offenders = [
        path.name
        for path in package_dir.glob("*.py")
        if path.name != "queries.py" and sql_statement.search(path.read_text(encoding="utf-8"))
    ]
    assert offenders == []
