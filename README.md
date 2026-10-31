# Daily QC Accuracy Dashboard & Data Reconciliation System

An end-to-end Quality Control (QC) monitoring and data reconciliation platform designed for multi-tenant e-commerce analytics.

The system connects to multiple PostgreSQL client databases, extracts daily performance metrics (Sales, GMV, Orders, SKU Traffic, Seller Traffic), benchmarks collected data against raw marketplace feeds, stores point-in-time snapshots in embedded [DuckDB](https://duckdb.org/), and provides an interactive [Streamlit](https://streamlit.io/) audit dashboard with Google-Sheet-compatible Excel export capabilities.

---

## Table of Contents

- [Daily QC Accuracy Dashboard \& Data Reconciliation System](#daily-qc-accuracy-dashboard--data-reconciliation-system)
  - [Table of Contents](#table-of-contents)
  - [Key Features](#key-features)
  - [Directory Structure](#directory-structure)
    - [Component Overview](#component-overview)
  - [QC Status Evaluation Logic](#qc-status-evaluation-logic)
  - [Supported Metrics](#supported-metrics)
  - [Installation \& Environment Setup](#installation--environment-setup)
    - [1. Prerequisites](#1-prerequisites)
    - [2. Clone and Setup Virtual Environment](#2-clone-and-setup-virtual-environment)
    - [3. Install Dependencies](#3-install-dependencies)
  - [Configuration](#configuration)
    - [1. Environment Variables (`.env`)](#1-environment-variables-env)
    - [2. Client Database Registry (`clients.json`)](#2-client-database-registry-clientsjson)
    - [3. Client Seller Mapping (`clients/`)](#3-client-seller-mapping-clients)
  - [Usage Guide](#usage-guide)
    - [1. Running Data Ingestion \& Snapshotting](#1-running-data-ingestion--snapshotting)
    - [2. Launching the Streamlit Dashboard](#2-launching-the-streamlit-dashboard)
    - [3. Running CSV \& DuckDB Reconciliation](#3-running-csv--duckdb-reconciliation)
  - [Snapshot Retention \& Maintenance](#snapshot-retention--maintenance)
  - [Troubleshooting \& FAQs](#troubleshooting--faqs)
    - [1. `ValueError: ❌ Thiếu DB_USER hoặc DB_PASS trong file .env!`](#1-valueerror--thiếu-db_user-hoặc-db_pass-trong-file-env)
    - [2. `⚠️ No snapshot files found! Please run the ETL pipeline first.`](#2-️-no-snapshot-files-found-please-run-the-etl-pipeline-first)
    - [3. PostgreSQL Connection Timeouts](#3-postgresql-connection-timeouts)
    - [4. DuckDB File Lock (`duckdb.IOException: Could not set lock on file`)](#4-duckdb-file-lock-duckdbioexception-could-not-set-lock-on-file)
  - [License \& Authors](#license--authors)


---

## Key Features

1. **Multi-Tenant PostgreSQL Data Pipeline (`fetch_data.py`)**:
   - Queries historical records (default: past 30 days) across multiple clients defined in `clients.json`.
   - Filters only active sellers per client via CSV mapping files, reducing load and query execution time.
   - Handles marketplace-specific sales attribution rules (e.g., Lazada COD vs Non-COD, Shopee v4 transition date, TikTok Shop order statuses and fund filters).
   - Prioritizes source types (`SC` vs `BP` vs `unknown`) to eliminate duplicate metrics.

2. **Embedded Analytical Storage with DuckDB**:
   - Zero-dependency local analytics using DuckDB tables (`qc_accuracy_daily`).
   - Snapshot archiving with automatic rolling retention (prunes snapshots older than 7 days or beyond 10 files).
   - Atomic snapshot replacement (`qc_local_cache_temp.duckdb` -> `qc_local_cache.duckdb`).

3. **Interactive Dark-Themed Audit Dashboard (`app.py`)**:
   - **Snapshot Auditing**: Audit historical snapshots or switch to the latest sync in real time.
   - **On-Demand Sync**: Trigger `fetch_data.py` directly from the dashboard UI.
   - **Granular Multi-Filters**: Filter by Client Company, Metric, and Seller ID (with live search, Shop Name display, and "Select All" toggle).
   - **Lateral Matrix Reindexing**: Generates a full grid of dates $\times$ selected sellers to visually expose days missing data (`NO_DATA`).
   - **Interactive Scatter Heatmap**: Color-coded status map with rich hover cards and dynamic height calculation based on seller count.
   - **Dual-Sheet Excel Export**: Generates `.xlsx` files with `automated_input` and `database_input` tabs formatted identically to Google Sheets (custom headers, cell borders, freeze panes, auto-width columns).
   - **Critical Issues Table**: Quick inspection grid for sellers with accuracy anomalies.

4. **Automated Reconciliation Tool (`reconcile_csv.py`)**:
   - Parses Google Sheets exported raw data (`sales_traffic_daily.csv`).
   - Performs a 4-dimensional audit against DuckDB data: Collected Value, Raw Benchmark Value, Accuracy %, and QC Flag.
   - Generates detailed discrepancy logs (`reconciliation_discrepancies.csv`) for auditing.

---

## Directory Structure

```cmd
|   .env                                    --Credentials database file stored (file to ignore)
|   .env.example                            --Template for .env file
|   .gitignore                              
|   app.py                                  --Streamlit analytics dashboard
|   clients.json                            --Host database information file stored (file to ignore)
|   clients.json.example                    --Template for clients.json file
|   fetch_data.py                           --Fetch data from database file stored
|   qc_local_cache.duckdb                   --Local analytics database file stored (file to ignore)
|   README.md                               --README file for project dependencies and version constraints
|   reconcile_csv.py                        --Reconcile data from database file stored
|   requirements.txt                        --Requirements file for project dependencies and version constraints
+---clients
|       {company_name}.csv                  --Host database active sellers and names file stored (file to ignore)
|       
\---snapshots
        qc_cache_{timestamp}.duckdb           --Snapshots of the local analytics database
```

### Component Overview

| File / Folder | Type | Description |
| :--- | :--- | :--- |
| **`app.py`** | Script | Streamlit analytics dashboard with interactive heatmap, snapshot switcher, and dual-sheet Excel export. |
| **`fetch_data.py`** | Script | ETL ingestion engine connecting to PostgreSQL client databases, benchmarking metrics, and generating DuckDB snapshots. |
| **`reconcile_csv.py`** | Script | Audit utility checking Google Sheet exports (`sales_traffic_daily.csv`) against DuckDB records. |
| **`clients/`** | Directory | Client active seller mapping CSVs (e.g. `{company_name}.csv`). Maps `seller_used_id` to human-readable `seller_name`. |
| **`snapshots/`** | Directory | Timestamped analytical database snapshots (`qc_cache_%Y%m%d_%H%M%S.duckdb`) subject to retention pruning. |
| **`qc_local_cache.duckdb`** | Database | Active local DuckDB cache file serving queries for `app.py`. |
| **`clients.json`** | Config | Registry of client PostgreSQL connection strings (host, port, db name). Git ignored. |
| **`clients.json.example`** | Config | Example template for configuring `clients.json`. |
| **`.env`** | Secrets | Global database credentials (`DB_USER`, `DB_PASS`). Git ignored. |
| **`.env.example`** | Secrets | Template for `.env` file configuration. |
| **`requirements.txt`** | Config | Python package dependencies and version constraints. |

---

## QC Status Evaluation Logic

Accuracy is calculated by comparing collected data from operational tables against raw benchmark inputs:

$$\text{Accuracy (\%)} = \left( \frac{\text{collected\_val}}{\text{raw\_val}} \right) \times 100$$

Status evaluation priority:

| Status | Condition | Description | Color Code |
| :--- | :--- | :--- | :--- |
| **`NO_DATA`** | $\text{raw\_val} \text{ is NULL } \land \text{collected\_val} \text{ is NULL}$ | Neither source has recorded data for the date | Dark Gray (`#21262d`) |
| **`HAS_DATA`** | $\text{raw\_val} \text{ is NOT NULL } \land \text{collected\_val} \text{ is NULL}$ | Data appears in only one source | Blue (`#1f6beb`) |
| **`GOOD`** | Both $> 0$ and $0.95 \le Accuracy \le 1.05$ | Accuracy is within tolerance ($\pm 5\%$) | Green (`#2ea043`) |
| **`CRITICAL`** | Both $> 0$, but accuracy $< 95\%$ or $> 105\%$ | Discrepancy detected between sources | Red (`#da3633`) |

---

## Supported Metrics

The system monitors and unpivots operational data into standard metric names:

| Metric Name | Source Type | Raw Benchmark Source | Collected Source |
| :--- | :--- | :--- | :--- |
| **`Sales API V4 - Quantity`** | `item` | `raw_seller_metrics.quantity` | `ecommerce_item.quantity` (v4 rules) |
| **`Sales API V4 - Revenue`** | `item` | `raw_seller_metrics.revenue` | `ecommerce_item.revenue` (v4 rules) |
| **`Sales SKU - Quantity`** | `export_sku_sales` | `raw_seller_metrics.quantity` | `ecommerce_export_sku_sales.quantity` |
| **`Sales SKU - Revenue`** | `export_sku_sales` | `raw_seller_metrics.revenue` | `ecommerce_export_sku_sales.s_onsite_selling` |
| **`Sales Seller - Quantity`** | `export_seller_sales` | `raw_seller_metrics.quantity` | `ecommerce_export_seller_sales.quantity` |
| **`Sales Seller - Revenue`** | `export_seller_sales` | `raw_seller_metrics.revenue` | `ecommerce_export_seller_sales.revenue` |
| **`Traffic SKU - Page View`** | `export_sku_traffic` | `raw_seller_metrics.page_view` | `ecommerce_export_sku_traffic.page_view` |
| **`Traffic SKU - Visitor`** | `export_sku_traffic` | N/A | `ecommerce_export_sku_traffic.unique_visitor` |
| **`Traffic Seller - Page View`** | `export_seller_traffic` | `raw_seller_metrics.page_view` | `ecommerce_export_seller_traffic.page_view` |

---

## Installation & Environment Setup

### 1. Prerequisites
- Network access to client PostgreSQL database hosts on designated ports.

### 2. Clone and Setup Virtual Environment
```bash
# Navigate to project root
cd daily-qc-dashboard

# Create Python virtual environment
python -m venv venv

# Activate virtual environment (terminal)
\venv\Scripts\Activate
```

### 3. Install Dependencies
```bash
pip install -r requirements.txt
```

---

## Configuration

### 1. Environment Variables (`.env`)
Create a `.env` file from the example template:
```bash
cp .env.example .env
```
Populate the database credentials used for connecting to client PostgreSQL instances:
```env
DB_USER=   #Credentials's Username database
DB_PASS=   #Credentials's Password database
```

### 2. Client Database Registry (`clients.json`)
Create `clients.json` in the root directory defining the target databases:
```bash
cp clients.json.example clients.json
```



### 3. Client Seller Mapping (`clients/`)
To map seller IDs to human-readable shop names and restrict queries only to active stores, place CSV files named after the `company_name` in the `clients/` folder:
- Path format: `clients/{company_name}.csv`
- Header format:
  ```csv
  seller_used_id,seller_name
  VN.LAZ.1000001001,Official Store VN
  PH.SHP.238345847,Supermarket PH
  ```
- If a CSV is missing or empty, `fetch_data.py` falls back to querying all sellers available in the database for that client.
- ***TBD: will be remove this by-pass using this folder in order to make the config more simple.***

---

## Usage Guide

### 1. Running Data Ingestion & Snapshotting
Fetch the latest metrics across all configured clients:
```bash
python fetch_data.py
```
**Execution Steps:**
1. Connects to each client database in `clients.json`.
2. Reads seller list from `clients/{company}.csv` and constructs optimized SQL filters.
3. Pulls data in batches (`CHUNK_SIZE = 1000`) using SQLAlchemy and pandas.
4. Saves full dataset into a new DuckDB snapshot (`snapshots/qc_cache_YYYYMMDD_HHMMSS.duckdb`).
5. Overwrites `qc_local_cache.duckdb` for immediate dashboard display.
6. Prunes old snapshots based on retention policies.

### 2. Launching the Streamlit Dashboard
Start the web interface:
```bash
streamlit run app.py
```
Open your browser at `http://localhost:8501`.

**Dashboard Features:**
- **Sidebar Audit Sync**: Select any past snapshot to audit historical states.
- **Trigger Sync Button**: Click to run `fetch_data.py` directly from the UI without terminal access.
- **Filters**: Choose Company, Metric, and Seller IDs (format: `Store Name ...Last4Digits`).
- **Lateral Matrix Heatmap**: Visualize daily data status for each seller across all available days.
- **Download Excel (.xlsx)**:
  - Select between **Filtered Data Only** or **Full Company Data**.
  - Generates a styled workbook with 2 tabs (`automated_input` and `database_input`).
- **Critical Issues Table**: View detailed records of items failing accuracy thresholds.

### 3. Running CSV & DuckDB Reconciliation
Reconcile an exported Google Sheet (`sales_traffic_daily.csv`) against a local DuckDB cache:
```bash
python reconcile_csv.py
```
**Output:**
- Summary statistics of matching vs mismatched records.
- Generates `reconciliation_discrepancies.csv` containing diffs for values, accuracy, and flags.

---

## Snapshot Retention & Maintenance

Configurable retention settings in `fetch_data.py`:

```python
MAX_RETENTION_DAYS = 7     # Automatically removes snapshots older than 7 days
MAX_SNAPSHOT_FILES = 10    # Retains a maximum of 10 snapshot files
```

Snapshots are stored in `snapshots/` using the naming pattern `qc_cache_%Y%m%d_%H%M%S.duckdb`.

---

## Troubleshooting & FAQs

### 1. `ValueError: ❌ Thiếu DB_USER hoặc DB_PASS trong file .env!`
- **Solution**: Ensure `.env` exists in the project root and defines `DB_USER` and `DB_PASS`.

### 2. `⚠️ No snapshot files found! Please run the ETL pipeline first.`
- **Solution**: The dashboard requires at least one snapshot. Run `python fetch_data.py` once to build `qc_local_cache.duckdb` and the initial snapshot.

### 3. PostgreSQL Connection Timeouts
- **Solution**: Check firewall permissions and VPN access to target database hosts. The default connection timeout is set to 15 seconds in `create_engine()`.

### 4. DuckDB File Lock (`duckdb.IOException: Could not set lock on file`)
- **Solution**: DuckDB locks files during writes. Avoid running `python fetch_data.py` simultaneously from multiple terminal sessions. `app.py` accesses snapshots with `read_only=True` to minimize lock contention.

---

## License & Authors

Maintained for internal Quality Control and Data Operations.

