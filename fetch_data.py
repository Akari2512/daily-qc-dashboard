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
from sqlalchemy.exc import OperationalError

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(line_buffering=True)

load_dotenv()

DEFAULT_DB_USER = os.getenv("DB_USER")
DEFAULT_DB_PASS = os.getenv("DB_PASS")
CLIENTS_FILE = "clients.json"
SNAPSHOT_DIR = "snapshots"
LATEST_CACHE_FILE = "qc_local_cache.duckdb"
QUERY_DIR = "queries"

CLIENTS_SELLER_DIR = "clients" if os.path.exists("clients") else "client"

MAX_RETENTION_DAYS = 7
MAX_SNAPSHOT_FILES = 10
CHUNK_SIZE = 1000
MAX_RETRIES = 5
RETRY_DELAY = 5

if not DEFAULT_DB_USER or not DEFAULT_DB_PASS:
    raise ValueError("❌ Thiếu DB_USER hoặc DB_PASS trong file .env!")

if not os.path.exists(CLIENTS_FILE):
    raise FileNotFoundError(f"❌ Không tìm thấy file {CLIENTS_FILE}.")

with open(CLIENTS_FILE, "r", encoding="utf-8") as f:
    clients = json.load(f)

SQL_FILES = {
    "raw": "raw.sql",
    "api": "api.sql",
    "sales_sku": "sales_sku.sql",
    "traffic_sku": "traffic_sku.sql",
    "visitor_sku": "visitor_sku.sql",
    "sales_seller": "sales_seller.sql",
    "traffic_seller": "traffic_seller.sql",
    "visitor_seller": "visitor_seller.sql"
}


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


def execute_query_with_retry(engine, sql_text, company_name, query_key):
    """Thực thi query với cơ chế retry khi chạm trần session credential."""
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            chunks = []
            with engine.connect() as conn:
                for chunk in pd.read_sql_query(
                        text(sql_text),
                        conn,
                        chunksize=CHUNK_SIZE
                ):
                    chunks.append(chunk)
            if chunks:
                return pd.concat(chunks, ignore_index=True)
            return pd.DataFrame()
        except OperationalError as db_err:
            print(
                f"\n⚠️ [CẢNH BÁO] [{company_name}] ({query_key}) Gặp lỗi kết nối / Chạm trần session credential (Lần {attempt}/{MAX_RETRIES}):",
                flush=True)
            print(f"   Chi tiết: {db_err}", flush=True)
            print(f"   👉 Hãy ngắt bớt các kết nối thủ công (DBeaver, query cũ) để tiếp tục!", flush=True)
            if attempt == MAX_RETRIES:
                raise db_err
            print(f"   ⏳ Đang chờ {RETRY_DELAY} giây trước khi tự động thử lại...", flush=True)
            time.sleep(RETRY_DELAY)
    return pd.DataFrame()


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
            print(f"  📌 Đã áp dụng bộ lọc: {len(active_sellers)} active sellers từ CSV", flush=True)
        else:
            print("  ℹ️ Không tìm thấy file CSV active sellers, lấy tất cả sellers.", flush=True)

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

            dfs_dict = {}
            for key, filename in SQL_FILES.items():
                file_path = os.path.join(QUERY_DIR, filename)
                if not os.path.exists(file_path):
                    raise FileNotFoundError(f"❌ Không tìm thấy file query: {file_path}")

                with open(file_path, "r", encoding="utf-8") as f:
                    sql_content = f.read()

                final_sql = sql_content.replace(":company_name", f"'{company_name}'")

                print(f"  📥 Đang tải dữ liệu [{key}]...", flush=True)
                dfs_dict[key] = execute_query_with_retry(engine, final_sql, company_name, key)

            df_raw = dfs_dict["raw"]
            df_api = dfs_dict["api"]
            df_sales_sku = dfs_dict["sales_sku"]
            df_traffic_sku = dfs_dict["traffic_sku"]
            df_visitor_sku = dfs_dict["visitor_sku"]
            df_sales_seller = dfs_dict["sales_seller"]
            df_traffic_seller = dfs_dict["traffic_seller"]
            df_visitor_seller = dfs_dict["visitor_seller"]

            key_frames = [
                df_raw[['company_seller_used_id', 'day']] if not df_raw.empty else pd.DataFrame(
                    columns=['company_seller_used_id', 'day']),
                df_api[['company_seller_used_id', 'day']] if not df_api.empty else pd.DataFrame(
                    columns=['company_seller_used_id', 'day']),
                df_sales_sku[['company_seller_used_id', 'day']] if not df_sales_sku.empty else pd.DataFrame(
                    columns=['company_seller_used_id', 'day']),
                df_traffic_sku[['company_seller_used_id', 'day']] if not df_traffic_sku.empty else pd.DataFrame(
                    columns=['company_seller_used_id', 'day']),
                df_sales_seller[['company_seller_used_id', 'day']] if not df_sales_seller.empty else pd.DataFrame(
                    columns=['company_seller_used_id', 'day']),
                df_traffic_seller[['company_seller_used_id', 'day']] if not df_traffic_seller.empty else pd.DataFrame(
                    columns=['company_seller_used_id', 'day'])
            ]

            all_keys = pd.concat(key_frames, ignore_index=True).drop_duplicates() if any(
                not f.empty for f in key_frames) else pd.DataFrame(columns=['company_seller_used_id', 'day'])

            # Lọc active sellers trực tiếp chuẩn xác tại đây
            if active_sellers and not all_keys.empty:
                all_keys = all_keys[all_keys['company_seller_used_id'].isin(active_sellers)]

            if all_keys.empty:
                print(f"  ⚠️ Không có dữ liệu cho Client [{company_name}].", flush=True)
                continue

            merged = all_keys.copy()
            for df_part in [df_raw, df_api, df_sales_sku, df_traffic_sku, df_visitor_sku, df_sales_seller,
                            df_traffic_seller, df_visitor_seller]:
                if not df_part.empty:
                    merged = merged.merge(df_part, on=['company_seller_used_id', 'day'], how='left')

            unpivoted_list = [
                merged[['company_seller_used_id', 'day']].assign(source='item', metric_name='Sales API V4 - Quantity',
                                                                 collected_val=merged.get('api_v4_quantity'),
                                                                 raw_val=merged.get('raw_quantity')),
                merged[['company_seller_used_id', 'day']].assign(source='item', metric_name='Sales API V4 - Revenue',
                                                                 collected_val=merged.get('api_v4_revenue'),
                                                                 raw_val=merged.get('raw_revenue')),
                merged[['company_seller_used_id', 'day']].assign(source='export_sku_sales',
                                                                 metric_name='Sales SKU - Quantity',
                                                                 collected_val=merged.get('sku_quantity'),
                                                                 raw_val=merged.get('raw_quantity')),
                merged[['company_seller_used_id', 'day']].assign(source='export_sku_sales',
                                                                 metric_name='Sales SKU - Revenue',
                                                                 collected_val=merged.get('sku_revenue'),
                                                                 raw_val=merged.get('raw_revenue')),
                merged[['company_seller_used_id', 'day']].assign(source='export_seller_sales',
                                                                 metric_name='Sales Seller - Quantity',
                                                                 collected_val=merged.get('seller_quantity'),
                                                                 raw_val=merged.get('raw_quantity')),
                merged[['company_seller_used_id', 'day']].assign(source='export_seller_sales',
                                                                 metric_name='Sales Seller - Revenue',
                                                                 collected_val=merged.get('seller_revenue'),
                                                                 raw_val=merged.get('raw_revenue')),
                merged[['company_seller_used_id', 'day']].assign(source='export_sku_traffic',
                                                                 metric_name='Traffic SKU - Page View',
                                                                 collected_val=merged.get('pageview__sku'),
                                                                 raw_val=merged.get('raw_pageview__sku')),
                merged[['company_seller_used_id', 'day']].assign(source='export_seller_traffic',
                                                                 metric_name='Traffic Seller - Page View',
                                                                 collected_val=merged.get('pageview__seller'),
                                                                 raw_val=merged.get('raw_pageview__seller')),
            ]
            df_unpivoted = pd.concat(unpivoted_list, ignore_index=True)

            df_unpivoted['company_name'] = company_name
            df_unpivoted['accuracy_pct'] = df_unpivoted.apply(
                lambda r: (r['collected_val'] / r['raw_val']) * 100 if pd.notna(r['raw_val']) and r[
                    'raw_val'] > 0 and pd.notna(r['collected_val']) else None,
                axis=1
            )

            def calculate_qc_status(row):
                rv = row['raw_val']
                cv = row['collected_val']
                r_zero = pd.isna(rv) or rv <= 0
                c_zero = pd.isna(cv) or cv <= 0

                if r_zero and c_zero:
                    return 'NO_DATA'
                elif (pd.isna(rv) or rv <= 0) and (not pd.isna(cv) and cv > 0):
                    return 'HAS_DATA'
                elif (not pd.isna(rv) and rv > 0) and (pd.isna(cv) or cv <= 0):
                    return 'HAS_DATA'
                elif not pd.isna(rv) and rv > 0 and not pd.isna(cv) and cv > 0:
                    ratio = cv / rv
                    if 0.95 <= ratio <= 1.05:
                        return 'GOOD'
                    else:
                        return 'CRITICAL'
                return 'CRITICAL'

            df_unpivoted['qc_status'] = df_unpivoted.apply(calculate_qc_status, axis=1)

            final_client_df = df_unpivoted[[
                'company_name', 'company_seller_used_id', 'day', 'source',
                'metric_name', 'collected_val', 'raw_val', 'accuracy_pct', 'qc_status'
            ]]

            final_client_df['seller_name'] = final_client_df['company_seller_used_id'].map(seller_mapping).fillna("")
            all_dfs.append(final_client_df)

        except Exception as e:
            print(f"❌ Lỗi khi xử lý Client [{company_name}]: {e}", flush=True)

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