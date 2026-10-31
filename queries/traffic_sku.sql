WITH params AS (
    SELECT
        (now() - INTERVAL '30 day')::date AS start_date,
        (now() - INTERVAL '1 day')::date AS end_date,
        :company_name::text AS company
)
SELECT
    p.company || ' - ' || concat_ws('.', split_part(eest.fk_sku_used_id, '.', 1), split_part(eest.fk_sku_used_id, '.', 2), split_part(eest.fk_sku_used_id, '.', 3)) AS company_seller_used_id,
    eest.day AS day,
    SUM(eest.page_view) AS pageview__sku
FROM ecommerce_export_sku_traffic eest
CROSS JOIN params p
WHERE eest.day BETWEEN p.start_date AND p.end_date
GROUP BY 1, 2;