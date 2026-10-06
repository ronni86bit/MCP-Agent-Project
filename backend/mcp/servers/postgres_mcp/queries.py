"""The ONLY module allowed to contain SQL for postgres-mcp.

Every template is static: fixed tables/columns, `$n` placeholders only, no
string formatting, no identifiers composed at runtime. Optional filters arrive
as explicit NULL-able parameters (`$n::text IS NULL OR col = $n`) so the SQL
never has to be rebuilt. The unit test suite enforces this module's shape.

Parameter layout shared by the range tools:
    $1 start_date, $2 end_date, $3 product, $4 region, $5 channel, $6 limit
The compare tools use $1..$2 (window) and $3..$5 (filters) without a limit.
"""

DAILY_SALES_SQL = """
SELECT ds.sale_date::text AS sale_date,
       SUM(ds.orders)::bigint AS orders,
       SUM(ds.units)::bigint AS units,
       SUM(ds.revenue)::float8 AS revenue
FROM sales_data.daily_sales ds
JOIN sales_data.products p ON p.product_id = ds.product_id
JOIN sales_data.regions r ON r.region_id = ds.region_id
WHERE ds.sale_date BETWEEN $1 AND $2
  AND ($3::text IS NULL OR p.name = $3)
  AND ($4::text IS NULL OR r.name = $4)
  AND ($5::text IS NULL OR ds.channel = $5)
GROUP BY ds.sale_date
ORDER BY ds.sale_date
LIMIT $6
"""

PRODUCT_SALES_SQL = """
SELECT p.product_id,
       p.name,
       p.category,
       SUM(ds.orders)::bigint AS orders,
       SUM(ds.units)::bigint AS units,
       SUM(ds.revenue)::float8 AS revenue
FROM sales_data.daily_sales ds
JOIN sales_data.products p ON p.product_id = ds.product_id
JOIN sales_data.regions r ON r.region_id = ds.region_id
WHERE ds.sale_date BETWEEN $1 AND $2
  AND ($3::text IS NULL OR p.name = $3)
  AND ($4::text IS NULL OR r.name = $4)
  AND ($5::text IS NULL OR ds.channel = $5)
GROUP BY p.product_id, p.name, p.category
ORDER BY revenue DESC
LIMIT $6
"""

REGION_SALES_SQL = """
SELECT r.region_id,
       r.name,
       r.country,
       SUM(ds.orders)::bigint AS orders,
       SUM(ds.units)::bigint AS units,
       SUM(ds.revenue)::float8 AS revenue
FROM sales_data.daily_sales ds
JOIN sales_data.products p ON p.product_id = ds.product_id
JOIN sales_data.regions r ON r.region_id = ds.region_id
WHERE ds.sale_date BETWEEN $1 AND $2
  AND ($3::text IS NULL OR p.name = $3)
  AND ($4::text IS NULL OR r.name = $4)
  AND ($5::text IS NULL OR ds.channel = $5)
GROUP BY r.region_id, r.name, r.country
ORDER BY revenue DESC
LIMIT $6
"""

TREND_DAILY_SQL = """
SELECT ds.sale_date::text AS period,
       SUM(ds.orders)::bigint AS orders,
       SUM(ds.units)::bigint AS units,
       SUM(ds.revenue)::float8 AS revenue
FROM sales_data.daily_sales ds
JOIN sales_data.products p ON p.product_id = ds.product_id
JOIN sales_data.regions r ON r.region_id = ds.region_id
WHERE ds.sale_date BETWEEN $1 AND $2
  AND ($3::text IS NULL OR p.name = $3)
  AND ($4::text IS NULL OR r.name = $4)
  AND ($5::text IS NULL OR ds.channel = $5)
GROUP BY ds.sale_date
ORDER BY ds.sale_date
LIMIT $6
"""

TREND_WEEKLY_SQL = """
SELECT date_trunc('week', ds.sale_date)::date::text AS period,
       SUM(ds.orders)::bigint AS orders,
       SUM(ds.units)::bigint AS units,
       SUM(ds.revenue)::float8 AS revenue
FROM sales_data.daily_sales ds
JOIN sales_data.products p ON p.product_id = ds.product_id
JOIN sales_data.regions r ON r.region_id = ds.region_id
WHERE ds.sale_date BETWEEN $1 AND $2
  AND ($3::text IS NULL OR p.name = $3)
  AND ($4::text IS NULL OR r.name = $4)
  AND ($5::text IS NULL OR ds.channel = $5)
GROUP BY 1
ORDER BY 1
LIMIT $6
"""

COMPARE_TOTAL_SQL = """
SELECT COALESCE(SUM(ds.revenue), 0)::float8 AS revenue,
       COALESCE(SUM(ds.orders), 0)::bigint AS orders,
       COALESCE(SUM(ds.units), 0)::bigint AS units
FROM sales_data.daily_sales ds
JOIN sales_data.products p ON p.product_id = ds.product_id
JOIN sales_data.regions r ON r.region_id = ds.region_id
WHERE ds.sale_date BETWEEN $1 AND $2
  AND ($3::text IS NULL OR p.name = $3)
  AND ($4::text IS NULL OR r.name = $4)
  AND ($5::text IS NULL OR ds.channel = $5)
"""

COMPARE_BY_PRODUCT_SQL = """
SELECT p.name AS grp,
       COALESCE(SUM(ds.revenue), 0)::float8 AS revenue,
       COALESCE(SUM(ds.orders), 0)::bigint AS orders,
       COALESCE(SUM(ds.units), 0)::bigint AS units
FROM sales_data.daily_sales ds
JOIN sales_data.products p ON p.product_id = ds.product_id
JOIN sales_data.regions r ON r.region_id = ds.region_id
WHERE ds.sale_date BETWEEN $1 AND $2
  AND ($3::text IS NULL OR p.name = $3)
  AND ($4::text IS NULL OR r.name = $4)
  AND ($5::text IS NULL OR ds.channel = $5)
GROUP BY p.name
"""

COMPARE_BY_REGION_SQL = """
SELECT r.name AS grp,
       COALESCE(SUM(ds.revenue), 0)::float8 AS revenue,
       COALESCE(SUM(ds.orders), 0)::bigint AS orders,
       COALESCE(SUM(ds.units), 0)::bigint AS units
FROM sales_data.daily_sales ds
JOIN sales_data.products p ON p.product_id = ds.product_id
JOIN sales_data.regions r ON r.region_id = ds.region_id
WHERE ds.sale_date BETWEEN $1 AND $2
  AND ($3::text IS NULL OR p.name = $3)
  AND ($4::text IS NULL OR r.name = $4)
  AND ($5::text IS NULL OR ds.channel = $5)
GROUP BY r.name
"""

COMPARE_BY_CHANNEL_SQL = """
SELECT ds.channel AS grp,
       COALESCE(SUM(ds.revenue), 0)::float8 AS revenue,
       COALESCE(SUM(ds.orders), 0)::bigint AS orders,
       COALESCE(SUM(ds.units), 0)::bigint AS units
FROM sales_data.daily_sales ds
JOIN sales_data.products p ON p.product_id = ds.product_id
JOIN sales_data.regions r ON r.region_id = ds.region_id
WHERE ds.sale_date BETWEEN $1 AND $2
  AND ($3::text IS NULL OR p.name = $3)
  AND ($4::text IS NULL OR r.name = $4)
  AND ($5::text IS NULL OR ds.channel = $5)
GROUP BY ds.channel
"""

QUERY_REGISTRY = {
    "get_daily_sales": DAILY_SALES_SQL,
    "get_product_sales": PRODUCT_SALES_SQL,
    "get_region_sales": REGION_SALES_SQL,
    "get_sales_trend.day": TREND_DAILY_SQL,
    "get_sales_trend.week": TREND_WEEKLY_SQL,
    "compare_sales.total": COMPARE_TOTAL_SQL,
    "compare_sales.by_product": COMPARE_BY_PRODUCT_SQL,
    "compare_sales.by_region": COMPARE_BY_REGION_SQL,
    "compare_sales.by_channel": COMPARE_BY_CHANNEL_SQL,
}
