WITH params AS (
    SELECT
        (now() - INTERVAL '30 day')::date AS start_date,
        (now() - INTERVAL '1 day')::date AS end_date,
        :company_name::text AS company
)
SELECT
    p.company || ' - ' || concat_ws('.', split_part(eest.fk_seller_used_id, '.', 1), split_part(eest.fk_seller_used_id, '.', 2), split_part(eest.fk_seller_used_id, '.', 3)) AS company_seller_used_id,
    eest.day AS day,
    SUM(eest.unique_visitor) AS seller_visitor
FROM ecommerce_export_seller_traffic eest
CROSS JOIN params p
WHERE eest.day BETWEEN p.start_date AND p.end_date
GROUP BY 1, 2;