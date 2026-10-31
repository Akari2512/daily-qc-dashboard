WITH params AS (
    SELECT
        (now() - INTERVAL '30 day')::date AS start_date,
        (now() - INTERVAL '1 day')::date AS end_date,
        :company_name::text AS company
),
country_tz AS (
    SELECT used_id, COALESCE(timezone, 'UTC') AS timezone
    FROM user_management_country
),
base_api_items AS (
    SELECT
        ei.id, ei.day, ei.quantity, ei.s_net, ei.s_paid, ei.fees_paid, ei.shipping_fee_paid,
        ei.mkp_shipping_sub, ei.mkp_fees_sub, ei.mkp_onsite, ei.fk_status_used_id, ei.package_id, ei.fk_order_id,
        CASE WHEN ei.source ILIKE 'v3%' THEN 'v3' ELSE 'v4' END AS version,
        split_part(ei.fk_sku_used_id, '.', 2) AS marketplace,
        p.company || ' - ' || concat_ws('.', split_part(ei.fk_sku_used_id, '.', 1), split_part(ei.fk_sku_used_id, '.', 2), split_part(ei.fk_sku_used_id, '.', 3)) AS company_seller_used_id,
        split_part(ei.seller_used_id, '.', 1) AS seller_country,
        eo.payment_method, eo.paid_time, eo.order_type, eo.day AS order_day,
        COALESCE(ctz.timezone, 'UTC') AS timezone, p.start_date, p.end_date
    FROM ecommerce_item ei
    CROSS JOIN params p
    LEFT JOIN ecommerce_order eo ON split_part(ei.seller_used_id, '.', 2) IN ('LAZ', 'TTK') AND ei.fk_order_id = eo.id AND eo.day BETWEEN p.start_date AND p.end_date
    LEFT JOIN country_tz ctz ON split_part(ei.seller_used_id, '.', 2) IN ('LAZ', 'TTK') AND split_part(ei.seller_used_id, '.', 1) = ctz.used_id
    WHERE ei.day BETWEEN p.start_date AND p.end_date
),
api_sales_union AS (
    SELECT company_seller_used_id, day, version, SUM(quantity) AS quantity, SUM(COALESCE(s_net,0)+COALESCE(fees_paid,0)+COALESCE(shipping_fee_paid,0)+COALESCE(mkp_shipping_sub,0)+COALESCE(mkp_fees_sub,0)) AS revenue FROM base_api_items WHERE marketplace = 'LAZ' AND version = 'v3' GROUP BY 1,2,3
    UNION ALL
    SELECT company_seller_used_id, CASE WHEN payment_method <> 'COD' AND paid_time IS NOT NULL THEN (paid_time AT TIME ZONE timezone)::date ELSE day END AS day, version, SUM(quantity) AS quantity, SUM(COALESCE(s_net,0)+COALESCE(fees_paid,0)+COALESCE(shipping_fee_paid,0)+COALESCE(mkp_shipping_sub,0)+COALESCE(mkp_fees_sub,0)) AS revenue FROM base_api_items WHERE marketplace = 'LAZ' AND version = 'v4' AND payment_method IS NOT NULL AND (COALESCE(fk_status_used_id, '') NOT ILIKE '%canceled.marketplace%' AND COALESCE(fk_status_used_id, '') NOT ILIKE '%cancelled.marketplace%') AND NOT (payment_method <> 'COD' AND paid_time IS NULL) AND (CASE WHEN payment_method <> 'COD' AND paid_time IS NOT NULL THEN (paid_time AT TIME ZONE timezone)::date ELSE day END) BETWEEN start_date AND end_date GROUP BY 1,2,3
    UNION ALL
    SELECT company_seller_used_id, day, version, SUM(quantity) AS quantity, SUM(s_paid) AS revenue FROM base_api_items WHERE marketplace = 'TTK' AND version = 'v3' GROUP BY 1,2,3
    UNION ALL
    SELECT company_seller_used_id, CASE WHEN order_type IS NULL THEN day WHEN payment_method = 'Cash on delivery' THEN order_day WHEN paid_time IS NOT NULL THEN (paid_time AT TIME ZONE timezone)::date ELSE day END AS day, version, SUM(quantity) AS quantity, SUM(s_paid) AS revenue FROM base_api_items WHERE marketplace = 'TTK' AND version = 'v4' AND package_id IS NOT NULL AND COALESCE(order_type, '') <> 'SELLER_FUND_FREE_SAMPLE' AND (CASE WHEN order_type IS NULL THEN day WHEN payment_method = 'Cash on delivery' THEN order_day WHEN paid_time IS NOT NULL THEN (paid_time AT TIME ZONE timezone)::date ELSE day END) BETWEEN start_date AND end_date GROUP BY 1,2,3
    UNION ALL
    SELECT company_seller_used_id, day, version, SUM(quantity) AS quantity, SUM(s_paid) AS revenue FROM base_api_items WHERE marketplace = 'SHP' AND version = 'v3' GROUP BY 1,2,3
    UNION ALL
    SELECT company_seller_used_id, day, version, SUM(quantity) AS quantity, SUM(CASE WHEN day >= DATE '2026-01-01' THEN COALESCE(s_net, 0) + COALESCE(mkp_onsite, 0) ELSE COALESCE(s_paid, 0) END) AS revenue FROM base_api_items WHERE marketplace = 'SHP' AND version = 'v4' GROUP BY 1,2,3
    UNION ALL
    SELECT company_seller_used_id, day, version, SUM(quantity) AS quantity, SUM(s_paid) AS revenue FROM base_api_items WHERE marketplace NOT IN ('SHP', 'LAZ', 'TTK') GROUP BY 1,2,3
)
SELECT
    company_seller_used_id, day,
    SUM(CASE WHEN version = 'v4' THEN quantity ELSE NULL END) AS api_v4_quantity,
    SUM(CASE WHEN version = 'v4' THEN revenue ELSE NULL END) AS api_v4_revenue
FROM api_sales_union
GROUP BY 1, 2;