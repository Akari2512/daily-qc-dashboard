from datetime import datetime
import glob
import json
import os
import shutil
import sys
import time
import duckdb
from dotenv import load_dotenv
import pandas as pd
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(line_buffering=True)

load_dotenv()

DEFAULT_DB_USER = os.getenv("DB_USER")
DEFAULT_DB_PASS = os.getenv("DB_PASS")
CLIENTS_FILE = "clients.json"
SNAPSHOT_DIR = "snapshots"
LATEST_CACHE_FILE = "qc_local_cache.duckdb"

CLIENTS_SELLER_DIR = "clients" if os.path.exists("clients") else "client"

MAX_RETENTION_DAYS = 7
MAX_SNAPSHOT_FILES = 10
CHUNK_SIZE = 1000

if not DEFAULT_DB_USER or not DEFAULT_DB_PASS:
    raise ValueError("❌ Thiếu DB_USER hoặc DB_PASS trong file .env!")

if not os.path.exists(CLIENTS_FILE):
    raise FileNotFoundError(f"❌ Không tìm thấy file {CLIENTS_FILE}.")

with open(CLIENTS_FILE, "r", encoding="utf-8") as f:
    clients = json.load(f)

SQL_QUERY_TEMPLATE = """
WITH params AS (
    SELECT
        (now() - INTERVAL '30 day')::date AS start_date,
        (now() - INTERVAL '1 day')::date AS end_date,
        '{company_name}'::text AS company
),
agg AS (
    SELECT
        p.company || ' - ' || rsm.seller_used_id AS company_seller_used_id,
        rsm."day",
        count(distinct rsm."day") AS day_has_data,
        SUM(CASE WHEN rsm.metric_type = 'item_count'        THEN rsm.metric_value END) AS quantity,
        SUM(CASE WHEN rsm.metric_type = 'gmv'               THEN rsm.metric_value END) AS revenue,
        SUM(CASE WHEN rsm.metric_type = 'page_view__sku'    THEN rsm.metric_value END) AS pageview__sku,
        SUM(CASE WHEN rsm.metric_type = 'page_view__seller' THEN rsm.metric_value END) AS pageview__seller,
        COALESCE(NULLIF(split_part(rsm.platform_system_source, '_', 2), ''), 'unknown') AS source_type
    FROM raw_seller_metrics rsm
    CROSS JOIN params p
    WHERE rsm."day" BETWEEN p.start_date AND p.end_date
        AND rsm.time_dimension = 'daily'
        and rsm.metric_type in ('gmv', 'item_count', 'page_view__sku', 'page_view__seller')
    GROUP BY 1, 2, 8
),
filtered_agg AS (
    SELECT *
    FROM agg a
    WHERE
        a.source_type = 'SC'
        OR (
            a.source_type = 'BP'
            AND NOT EXISTS (
                SELECT 1
                FROM agg x
                WHERE x.source_type = 'SC'
                  AND x.company_seller_used_id = a.company_seller_used_id
                  AND x."day" = a."day"
            )
        )
        OR (
            a.source_type = 'unknown'
            AND NOT EXISTS (
                SELECT 1
                FROM agg x
                WHERE x.source_type IN ('SC', 'BP')
                  AND x.company_seller_used_id = a.company_seller_used_id
                  AND x."day" = a."day"
            )
        )
),
raw_benchmark AS (
    SELECT 
        company_seller_used_id,
        "day",
        SUM(quantity) AS raw_quantity,
        SUM(revenue) AS raw_revenue,
        SUM(COALESCE(pageview__sku, 0) + COALESCE(pageview__seller, 0)) AS raw_page_view
    FROM filtered_agg
    GROUP BY 1, 2
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
),
api_sales_agg AS (
    SELECT 
        company_seller_used_id, day,
        SUM(CASE WHEN version = 'v4' THEN quantity ELSE NULL END) AS api_v4_quantity,
        SUM(CASE WHEN version = 'v4' THEN revenue ELSE NULL END) AS api_v4_revenue
    FROM api_sales_union
    GROUP BY 1, 2
),
traffic_sku_agg AS (
    SELECT
        p.company || ' - ' || concat_ws('.', split_part(eest.fk_sku_used_id, '.', 1), split_part(eest.fk_sku_used_id, '.', 2), split_part(eest.fk_sku_used_id, '.', 3)) AS company_seller_used_id,
        eest.day, 
        SUM(eest.page_view) AS sku_page_view,
        SUM(eest.unique_visitor) AS sku_visitor
    FROM ecommerce_export_sku_traffic eest CROSS JOIN params p
    WHERE eest.day BETWEEN p.start_date AND p.end_date GROUP BY 1, 2
),
sales_sku_agg AS (
    SELECT
        p.company || ' - ' || concat_ws('.', split_part(eess.fk_sku_used_id, '.', 1), split_part(eess.fk_sku_used_id, '.', 2), split_part(eess.fk_sku_used_id, '.', 3)) AS company_seller_used_id,
        eess.day, SUM(eess.quantity) AS sku_quantity, SUM(eess.s_onsite_selling) AS sku_revenue
    FROM ecommerce_export_sku_sales eess CROSS JOIN params p
    WHERE eess.day BETWEEN p.start_date AND p.end_date GROUP BY 1, 2
),
traffic_seller_agg AS (
    SELECT
        p.company || ' - ' || concat_ws('.', split_part(eest.fk_seller_used_id, '.', 1), split_part(eest.fk_seller_used_id, '.', 3)) AS company_seller_used_id,
        eest.day, 
        SUM(eest.page_view) AS seller_page_view
    FROM ecommerce_export_seller_traffic eest CROSS JOIN params p
    WHERE eest.day BETWEEN p.start_date AND p.end_date GROUP BY 1, 2
),
sales_seller_agg AS (
    SELECT
        p.company || ' - ' || concat_ws('.', split_part(eess.fk_seller_used_id, '.', 1), split_part(eess.fk_seller_used_id, '.', 2), split_part(eess.fk_seller_used_id, '.', 3)) AS company_seller_used_id,
        eess.day, 
        SUM(eess.quantity) AS seller_quantity,
        SUM(eess.revenue) AS seller_revenue
    FROM ecommerce_export_seller_sales eess CROSS JOIN params p
    WHERE eess.day BETWEEN p.start_date AND p.end_date GROUP BY 1, 2
),
all_keys_raw AS (
    SELECT company_seller_used_id, day FROM raw_benchmark
    UNION SELECT company_seller_used_id, day FROM api_sales_agg
    UNION SELECT company_seller_used_id, day FROM sales_sku_agg
    UNION SELECT company_seller_used_id, day FROM traffic_sku_agg
    UNION SELECT company_seller_used_id, day FROM sales_seller_agg
    UNION SELECT company_seller_used_id, day FROM traffic_seller_agg
),
all_keys AS (
    SELECT company_seller_used_id, day 
    FROM all_keys_raw
    {active_seller_filter}
),
merged_all AS (
    SELECT
        k.company_seller_used_id, k.day,
        rb.raw_quantity, rb.raw_revenue, rb.raw_page_view,
        api.api_v4_quantity, api.api_v4_revenue,
        ss.sku_quantity, ss.sku_revenue, 
        ts.sku_page_view, ts.sku_visitor,
        sel_s.seller_quantity, sel_s.seller_revenue, 
        sel_t.seller_page_view
    FROM all_keys k
    LEFT JOIN raw_benchmark rb ON k.company_seller_used_id = rb.company_seller_used_id AND k.day = rb.day
    LEFT JOIN api_sales_agg api ON k.company_seller_used_id = api.company_seller_used_id AND k.day = api.day
    LEFT JOIN sales_sku_agg ss ON k.company_seller_used_id = ss.company_seller_used_id AND k.day = ss.day
    LEFT JOIN traffic_sku_agg ts ON k.company_seller_used_id = ts.company_seller_used_id AND k.day = ts.day
    LEFT JOIN sales_seller_agg sel_s ON k.company_seller_used_id = sel_s.company_seller_used_id AND k.day = sel_s.day
    LEFT JOIN traffic_seller_agg sel_t ON k.company_seller_used_id = sel_t.company_seller_used_id AND k.day = sel_t.day
),
unpivoted AS (
    SELECT company_seller_used_id, day, 'item' AS source, 'Sales API V4 - Quantity' AS metric_name, api_v4_quantity AS collected_val, raw_quantity AS raw_val FROM merged_all
    UNION ALL
    SELECT company_seller_used_id, day, 'item' AS source, 'Sales API V4 - Revenue' AS metric_name, api_v4_revenue AS collected_val, raw_revenue AS raw_val FROM merged_all
    UNION ALL
    SELECT company_seller_used_id, day, 'export_sku_sales' AS source, 'Sales SKU - Quantity' AS metric_name, sku_quantity AS collected_val, raw_quantity AS raw_val FROM merged_all
    UNION ALL
    SELECT company_seller_used_id, day, 'export_sku_sales' AS source, 'Sales SKU - Revenue' AS metric_name, sku_revenue AS collected_val, raw_revenue AS raw_val FROM merged_all
    UNION ALL
    SELECT company_seller_used_id, day, 'export_seller_sales' AS source, 'Sales Seller - Quantity' AS metric_name, seller_quantity AS collected_val, raw_quantity AS raw_val FROM merged_all
    UNION ALL
    SELECT company_seller_used_id, day, 'export_seller_sales' AS source, 'Sales Seller - Revenue' AS metric_name, seller_revenue AS collected_val, raw_revenue AS raw_val FROM merged_all
    UNION ALL
    SELECT company_seller_used_id, day, 'export_sku_traffic' AS source, 'Traffic SKU - Page View' AS metric_name, sku_page_view AS collected_val, raw_page_view AS raw_val FROM merged_all
    UNION ALL
    SELECT company_seller_used_id, day, 'export_sku_traffic' AS source, 'Traffic SKU - Visitor' AS metric_name, sku_visitor AS collected_val, NULL::numeric AS raw_val FROM merged_all
    UNION ALL
    SELECT company_seller_used_id, day, 'export_seller_traffic' AS source, 'Traffic Seller - Page View' AS metric_name, seller_page_view AS collected_val, raw_page_view AS raw_val FROM merged_all
)
SELECT 
    split_part(company_seller_used_id, ' - ', 1) AS company_name,
    company_seller_used_id,
    day,
    source,
    metric_name,
    collected_val,
    raw_val,
    CASE 
        WHEN raw_val IS NOT NULL AND raw_val > 0 AND collected_val IS NOT NULL THEN (collected_val / raw_val) * 100
        ELSE NULL
    END AS accuracy_pct,
    CASE 
        WHEN COALESCE(raw_val, 0) <= 0 AND COALESCE(collected_val, 0) <= 0 THEN 'NO_DATA'
        WHEN (raw_val IS NULL OR raw_val <= 0) AND collected_val > 0 THEN 'HAS_DATA'
        WHEN raw_val > 0 AND (collected_val IS NULL OR collected_val <= 0) THEN 'HAS_DATA'
        WHEN raw_val > 0 AND collected_val > 0 AND (collected_val / raw_val) BETWEEN 0.95 AND 1.05 THEN 'GOOD'
        ELSE 'CRITICAL'
    END AS qc_status
FROM unpivoted;
"""


def get_seller_mapping_for_client(company_name):
    if not os.path.exists(CLIENTS_SELLER_DIR):
        return {}

    possible_paths = [
        os.path.join(CLIENTS_SELLER_DIR, f"{company_name}.csv"),
        os.path.join(CLIENTS_SELLER_DIR, f"{company_name.lower()}.csv"),
    ]

    csv_path = None
    for p in possible_paths:
        if os.path.exists(p):
            csv_path = p
            break

    if not csv_path:
        return {}

    try:
        df_sellers = pd.read_csv(csv_path)

        id_col = None
        for col in df_sellers.columns:
            if col.strip().lower() in ["seller_id", "company_seller_used_id", "seller_used_id", "seller"]:
                id_col = col
                break
        if id_col is None and len(df_sellers.columns) > 0:
            id_col = df_sellers.columns[0]

        name_col = None
        for col in df_sellers.columns:
            if col.strip().lower() in ["seller_name", "name", "shop_name", "store_name"]:
                name_col = col
                break

        mapping = {}
        prefix = f"{company_name} - "
        for _, row in df_sellers.iterrows():
            raw_id = str(row[id_col]).strip() if pd.notna(row[id_col]) else ""
            if not raw_id:
                continue
            full_id = raw_id if raw_id.startswith(prefix) else f"{prefix}{raw_id}"
            name = str(row[name_col]).strip() if name_col and pd.notna(row[name_col]) else ""
            mapping[full_id] = name

        return mapping
    except Exception as e:
        print(f"⚠️ Lỗi khi đọc file CSV active sellers cho {company_name}: {e}", flush=True)
        return {}


def apply_retention_policy():
    if not os.path.exists(SNAPSHOT_DIR):
        return
    now_ts = time.time()
    files = sorted(glob.glob(os.path.join(SNAPSHOT_DIR, "qc_cache_*.duckdb")), key=os.path.getmtime)
    if not files:
        return

    remaining_files = []
    for fpath in files:
        age_days = (now_ts - os.path.getmtime(fpath)) / (24 * 3600)
        if age_days > MAX_RETENTION_DAYS:
            try:
                os.remove(fpath)
            except Exception as e:
                print(f"⚠️ Lỗi xóa file {fpath}: {e}", flush=True)
        else:
            remaining_files.append(fpath)

    while len(remaining_files) > MAX_SNAPSHOT_FILES:
        oldest_fpath = remaining_files.pop(0)
        try:
            os.remove(oldest_fpath)
        except Exception as e:
            print(f"⚠️ Lỗi xóa file {oldest_fpath}: {e}", flush=True)


def fetch_all_clients_data():
    all_dfs = []
    start_all = time.time()

    for client in clients:
        company_name = client["company_name"]
        host = client["db_host"]
        port = client.get("db_port", 5432)
        dbname = client["db_name"]
        db_user = client.get("db_user", DEFAULT_DB_USER)
        db_pass = client.get("db_pass", DEFAULT_DB_PASS)

        print(f"\n🔄 Đang kết nối Client: [{company_name}]...", flush=True)

        seller_mapping = get_seller_mapping_for_client(company_name)
        active_sellers = list(seller_mapping.keys())

        if active_sellers:
            clean_sellers = [s.replace("'", "''") for s in active_sellers]
            sellers_sql = ", ".join(f"'{s}'" for s in clean_sellers)
            active_seller_filter = f"WHERE company_seller_used_id IN ({sellers_sql})"
            print(f"  📌 Đã áp dụng bộ lọc: {len(active_sellers)} active sellers từ CSV", flush=True)
        else:
            active_seller_filter = ""
            print("  ℹ️ Không tìm thấy file CSV active sellers hoặc file trống, lấy tất cả sellers.", flush=True)

        try:
            connection_url = URL.create(
                drivername="postgresql+psycopg2",
                username=db_user,
                password=db_pass,
                host=host,
                port=int(port),
                database=dbname,
            )
            engine = create_engine(connection_url, connect_args={"connect_timeout": 15})

            formatted_sql = SQL_QUERY_TEMPLATE.format(
                company_name=company_name,
                active_seller_filter=active_seller_filter
            )
            escaped_sql = text(formatted_sql.replace("%", "%%"))

            client_chunks = []
            with engine.connect() as conn:
                for chunk in pd.read_sql_query(escaped_sql, conn, chunksize=CHUNK_SIZE):
                    client_chunks.append(chunk)

            if client_chunks:
                df_client = pd.concat(client_chunks, ignore_index=True)
                df_client["seller_name"] = df_client["company_seller_used_id"].map(seller_mapping).fillna("")
                all_dfs.append(df_client)

        except Exception as e:
            print(f"❌ Lỗi khi truy vấn Client [{company_name}]: {e}", flush=True)

    if not all_dfs:
        print("\n⚠️ Không lấy được dữ liệu từ bất kỳ Client nào!", flush=True)
        return

    final_df = pd.concat(all_dfs, ignore_index=True)
    os.makedirs(SNAPSHOT_DIR, exist_ok=True)
    timestamp_str = datetime.now().strftime("%Y%m%d_%H%M%S")
    snapshot_path = os.path.join(SNAPSHOT_DIR, f"qc_cache_{timestamp_str}.duckdb")

    con = duckdb.connect(snapshot_path)
    con.execute("CREATE OR REPLACE TABLE qc_accuracy_daily AS SELECT * FROM final_df")
    con.close()

    temp_latest_path = "qc_local_cache_temp.duckdb"
    shutil.copy2(snapshot_path, temp_latest_path)
    os.replace(temp_latest_path, LATEST_CACHE_FILE)
    apply_retention_policy()

    print(f"\n🎉 Hoàn tất sync dữ liệu trong {time.time() - start_all:.2f}s!", flush=True)


if __name__ == "__main__":
    fetch_all_clients_data()