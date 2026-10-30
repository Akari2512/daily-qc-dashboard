import duckdb
import pandas as pd

CSV_FILE_PATH = "sales_traffic_daily.csv"
DUCKDB_PATH = "snapshots/qc_cache_20261007_141731.duckdb"


def clean_num(v):
    """Chuyển đổi các giá trị số từ Sheet sang float."""
    if pd.isna(v) or str(v).strip().lower() in ['-', 'no input', 'none', 'no data', '']:
        return None
    try:
        return float(str(v).replace(',', '').strip())
    except ValueError:
        return None


def clean_acc(v):
    """Chuyển đổi phần trăm accuracy từ Sheet sang dạng float (0.0 - 1.0)."""
    if pd.isna(v) or str(v).strip().lower() in ['-', 'no input', 'none', 'no data', '']:
        return None
    s = str(v).strip().replace(',', '')
    if s.endswith('%'):
        try:
            return float(s.rstrip('%')) / 100.0
        except ValueError:
            return None
    try:
        v_float = float(s)
        return v_float if v_float <= 1.0 else v_float / 100.0
    except ValueError:
        return None


def derive_flag(collected, raw, accuracy):
    """Xác định trạng thái flag theo quy chuẩn QC."""
    if raw is None:
        return 'NO_BENCHMARK'
    if collected is None:
        return 'NO_DATA'
    if accuracy is not None:
        return 'MATCH' if abs(accuracy - 1.0) < 0.0001 else 'MISMATCH'
    return 'MATCH' if abs(collected - raw) < 0.0001 else 'MISMATCH'


def check_num_mismatch(val1, val2, tol=0.001):
    """Kiểm tra lệch số học giữa 2 giá trị (xử lý an toàn với NaN / None)."""
    if pd.isna(val1) and pd.isna(val2):
        return False
    if pd.isna(val1) or pd.isna(val2):
        return True
    return abs(val1 - val2) > tol


def run_reconciliation():
    print("⏳ Đang đọc và bóc tách dữ liệu từ Google Sheet...")
    df_raw = pd.read_csv(CSV_FILE_PATH, header=None, low_memory=False)

    # 1. Trích xuất đầy đủ 4 chỉ số từ Google Sheet
    records = []
    for row_idx in range(5, len(df_raw)):
        seller_id = df_raw.iloc[row_idx, 0]
        if pd.isna(seller_id) or not str(seller_id).strip():
            continue
        seller_id = str(seller_id).strip()

        # Quét qua 27 ngày (Khối Accuracy: Cols 12..38 | Collected: Cols 40..66 | Raw BP: Cols 68..94)
        for col_idx in range(12, 39):
            day = df_raw.iloc[4, col_idx]
            if pd.isna(day) or not str(day).strip():
                continue

            val_acc = df_raw.iloc[row_idx, col_idx]
            val_collected = df_raw.iloc[row_idx, col_idx + 28]  # Offset +28 sang khối Raw API
            val_raw = df_raw.iloc[row_idx, col_idx + 56]  # Offset +56 sang khối BP Benchmark

            c_val = clean_num(val_collected)
            r_val = clean_num(val_raw)
            a_val = clean_acc(val_acc)
            flg = derive_flag(c_val, r_val, a_val)

            records.append({
                'company_seller_used_id': seller_id,
                'day': str(day).strip(),
                'sheet_collected_val': c_val,
                'sheet_raw_val': r_val,
                'sheet_accuracy': a_val,
                'sheet_flag': flg
            })

    df_sheet = pd.DataFrame(records)
    active_sellers = df_sheet['company_seller_used_id'].unique().tolist()
    print(f"📊 Tìm thấy {len(active_sellers)} Active Sellers trên Google Sheet với {len(df_sheet):,} bản ghi ngày.")

    # 2. Truy vấn DuckDB linh hoạt (Đã có cơ chế tự kiểm tra tên cột)
    print("⏳ Đang kết nối DuckDB và kiểm tra cấu trúc cột thực tế...")
    con = duckdb.connect(DUCKDB_PATH, read_only=True)

    # Tự động đọc danh sách cột thực tế trong bảng DuckDB
    table_info = con.execute("DESCRIBE qc_accuracy_daily").fetchall()
    db_cols = [row[0] for row in table_info]

    # Map tên cột an toàn
    collected_col = "collected_val" if "collected_val" in db_cols else "NULL"
    raw_col = "raw_val" if "raw_val" in db_cols else "NULL"
    acc_col = "accuracy_pct" if "accuracy_pct" in db_cols else ("accuracy" if "accuracy" in db_cols else "NULL")
    flag_col = "flag" if "flag" in db_cols else (
        "qc_flag" if "qc_flag" in db_cols else ("status" if "status" in db_cols else "NULL"))

    placeholders = ', '.join(['?'] * len(active_sellers))
    query = f"""
        SELECT 
            company_seller_used_id,
            CAST(day AS VARCHAR) AS day,
            {collected_col} AS db_collected_val,
            {raw_col} AS db_raw_val,
            {acc_col} AS db_accuracy,
            {flag_col} AS db_flag
        FROM qc_accuracy_daily
        WHERE company_seller_used_id IN ({placeholders})
    """
    df_db = con.execute(query, active_sellers).df()
    con.close()

    # Nếu DuckDB không lưu sẵn cột flag, tự động tính toán db_flag trong Python
    if df_db['db_flag'].isna().all():
        print("ℹ️ DuckDB không chứa cột `flag`, tự động tính toán `db_flag` dựa trên dữ liệu DuckDB...")
        df_db['db_flag'] = df_db.apply(
            lambda r: derive_flag(
                r['db_collected_val'] if pd.notna(r['db_collected_val']) else None,
                r['db_raw_val'] if pd.notna(r['db_raw_val']) else None,
                r['db_accuracy'] if pd.notna(r['db_accuracy']) else None
            ),
            axis=1
        )

    print(f"📦 Đã lấy {len(df_db):,} bản ghi từ DuckDB.")

    # 3. Tiến hành đối soát LEFT JOIN
    print("🔍 Đang tiến hành đối soát 4 chỉ số (Collected, Raw, Accuracy, Flag)...")
    merged = pd.merge(
        df_sheet,
        df_db,
        on=['company_seller_used_id', 'day'],
        how='left',
        indicator=True
    )

    # Đánh giá độ lệch cho từng chỉ số
    merged['mismatch_collected'] = merged.apply(
        lambda r: check_num_mismatch(r['sheet_collected_val'], r['db_collected_val']), axis=1)
    merged['mismatch_raw'] = merged.apply(lambda r: check_num_mismatch(r['sheet_raw_val'], r['db_raw_val']), axis=1)
    merged['mismatch_acc'] = merged.apply(lambda r: check_num_mismatch(r['sheet_accuracy'], r['db_accuracy']), axis=1)
    merged['mismatch_flag'] = merged.apply(
        lambda r: (str(r['sheet_flag']).strip() != str(r['db_flag']).strip()) if (
                    pd.notna(r['sheet_flag']) or pd.notna(r['db_flag'])) else False,
        axis=1
    )

    # Phân loại bản ghi lỗi
    missing_in_db = merged[merged['_merge'] == 'left_only']
    value_mismatches = merged[
        (merged['_merge'] == 'both') & (
                merged['mismatch_collected'] |
                merged['mismatch_raw'] |
                merged['mismatch_acc'] |
                merged['mismatch_flag']
        )
        ]

    total_discrepancies = len(missing_in_db) + len(value_mismatches)

    # 4. Báo cáo kết quả
    print("\n" + "=" * 60)
    print("📌 KẾT QUẢ ĐỐI SOÁT TOÀN DIỆN (ACTIVE SELLERS: GG SHEET VS DUCKDB)")
    print("=" * 60)
    print(f"Tổng bản ghi trên GG Sheet: {len(df_sheet):,}")
    print(f"✅ Khớp hoàn toàn (cả 4 chỉ số): {len(df_sheet) - total_discrepancies:,}")
    print(f"⚠️ Thiếu hoàn toàn trong DuckDB: {len(missing_in_db):,} bản ghi")
    print(f"❌ Lệch chỉ số (Collected / Raw / Accuracy / Flag): {len(value_mismatches):,} bản ghi")

    if len(value_mismatches) > 0:
        print("\n🔍 Chi tiết số lượng lệch từng chỉ số:")
        print(f"  - Lệch Collected Val: {len(merged[(merged['_merge'] == 'both') & merged['mismatch_collected']]):,}")
        print(f"  - Lệch Raw Benchmark Val: {len(merged[(merged['_merge'] == 'both') & merged['mismatch_raw']]):,}")
        print(f"  - Lệch Accuracy: {len(merged[(merged['_merge'] == 'both') & merged['mismatch_acc']]):,}")
        print(f"  - Lệch Flag: {len(merged[(merged['_merge'] == 'both') & merged['mismatch_flag']]):,}")

    if total_discrepancies > 0:
        all_issues = pd.concat([missing_in_db, value_mismatches])

        # Thêm các cột tính toán độ lệch chênh lệch số học để dễ theo dõi
        all_issues['diff_collected'] = all_issues['sheet_collected_val'].fillna(0) - all_issues[
            'db_collected_val'].fillna(0)
        all_issues['diff_raw'] = all_issues['sheet_raw_val'].fillna(0) - all_issues['db_raw_val'].fillna(0)
        all_issues['diff_accuracy'] = all_issues['sheet_accuracy'].fillna(0) - all_issues['db_accuracy'].fillna(0)

        output_cols = [
            'company_seller_used_id', 'day',
            'sheet_collected_val', 'db_collected_val', 'diff_collected',
            'sheet_raw_val', 'db_raw_val', 'diff_raw',
            'sheet_accuracy', 'db_accuracy', 'diff_accuracy',
            'sheet_flag', 'db_flag', '_merge'
        ]
        all_issues[output_cols].to_csv("reconciliation_discrepancies.csv", index=False)
        print("\n📄 Chi tiết các dòng bị lệch đã được lưu vào file: `reconciliation_discrepancies.csv`")
    else:
        print("\n🎉 CHÚC MỪNG! Tất cả 4 chỉ số của Active Sellers trên Google Sheet và DuckDB khớp nhau 100%!")


if __name__ == "__main__":
    run_reconciliation()