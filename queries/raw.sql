WITH params AS (
    SELECT
        (now() - INTERVAL '30 day')::date AS start_date,
        (now() - INTERVAL '1 day')::date AS end_date,
        :company_name::text AS company
),
agg AS (
    SELECT
        p.company || ' - ' || rsm.seller_used_id AS company_seller_used_id,
        rsm."day",
        SUM(CASE WHEN rsm.metric_type = 'item_count'        THEN rsm.metric_value END) AS quantity,
        SUM(CASE WHEN rsm.metric_type = 'gmv'               THEN rsm.metric_value END) AS revenue,
        SUM(CASE WHEN rsm.metric_type = 'page_view__sku'    THEN rsm.metric_value END) AS pageview__sku,
        SUM(CASE WHEN rsm.metric_type = 'page_view__seller' THEN rsm.metric_value END) AS pageview__seller,
        COALESCE(NULLIF(split_part(rsm.platform_system_source, '_', 2), ''), 'unknown') AS source_type
    FROM raw_seller_metrics rsm
    CROSS JOIN params p
    WHERE rsm."day" BETWEEN p.start_date AND p.end_date
        AND rsm.time_dimension = 'daily'
        AND rsm.metric_type IN ('gmv', 'item_count', 'page_view__sku', 'page_view__seller')
    GROUP BY 1, 2, 7
),
filtered_agg AS (
    SELECT *
    FROM agg a
    WHERE
        a.source_type = 'SC'
        OR (
            a.source_type = 'BP'
            AND NOT EXISTS (
                SELECT 1 FROM agg x
                WHERE x.source_type = 'SC'
                  AND x.company_seller_used_id = a.company_seller_used_id
                  AND x."day" = a."day"
            )
        )
        OR (
            a.source_type = 'unknown'
            AND NOT EXISTS (
                SELECT 1 FROM agg x
                WHERE x.source_type IN ('SC', 'BP')
                  AND x.company_seller_used_id = a.company_seller_used_id
                  AND x."day" = a."day"
            )
        )
)
SELECT
    company_seller_used_id,
    "day",
    SUM(quantity) AS raw_quantity,
    SUM(revenue) AS raw_revenue,
    SUM(pageview__sku) AS raw_pageview__sku,
    SUM(pageview__seller) AS raw_pageview__seller
FROM filtered_agg
GROUP BY 1, 2;