WITH params AS (
    SELECT
        (now() - INTERVAL '30 day')::date AS start_date,
        (now() - INTERVAL '1 day')::date AS end_date,
        :company_name::text AS company
)
SELECT
    p.company || ' - ' || concat_ws('.', split_part(eess.fk_seller_used_id, '.', 1), split_part(eess.fk_seller_used_id, '.', 2), split_part(eess.fk_seller_used_id, '.', 3)) AS company_seller_used_id,
    eess.day,
    SUM(eess.quantity) AS seller_quantity,
    SUM(eess.revenue) AS seller_revenue
FROM ecommerce_export_seller_sales eess
CROSS JOIN params p
WHERE eess.day BETWEEN p.start_date AND p.end_date
GROUP BY 1, 2;