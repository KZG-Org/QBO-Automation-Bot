# QBO Automation Bot — Full Documentation Guide

This document is the complete reference for the **QBO (QuickBooks Online) Automation Bot**: a multi-client accounting pipeline that reads raw data from Google Sheets, transforms it into journals, expenses, and transfers, writes results back to Sheets, syncs to QuickBooks Online, and runs reconciliation checks.

---

## Table of Contents

1. [Overview](#1-overview)
2. [Architecture](#2-architecture)
3. [Project Structure](#3-project-structure)
4. [Configuration](#4-configuration)
5. [Setup & Installation](#5-setup--installation)
6. [Running the Bot](#6-running-the-bot)
7. [Pipeline Flow (Ingestion)](#7-pipeline-flow-ingestion)
8. [Syncing to QBO](#8-syncing-to-qbo)
9. [Reconciliation](#9-reconciliation)
10. [Webhook Server](#10-webhook-server)
11. [Bulk Delete Utility](#11-bulk-delete-utility)
12. [Scheduling & Automation](#12-scheduling--automation)
13. [Troubleshooting](#13-troubleshooting)
14. [Extending the Bot](#14-extending-the-bot)

---

## 1. Overview

### What It Does

- **Ingestion (Transform):** Reads raw accounting data from a **Source** Google Sheet (monthly tab), applies business rules, and produces three outputs: **Journals**, **Expenses**, and **Transfers**. Results are written to a **Transform** spreadsheet (per client/country/month).
- **QBO Sync:** Pushes rows marked "Ready to sync" from the Transform file into QuickBooks Online (Journal Entries, Purchases/Expenses, Transfers). Writes back QBO IDs and links to the sheet.
- **Reconciliation:** Compares Transform data (and optionally raw data) with QBO and writes status (Matched / Mismatch / Missing) into the Transform file.
- **Multi-Client:** A single **Master Sheet** lists clients (Client Name, Spreadsheet ID, Realm ID, Refresh Token, Status). The bot loops over active clients and processes each one in turn.

### Key Concepts

| Term | Description |
|------|-------------|
| **Master Sheet** | Central Google Sheet listing all clients; columns include Client Name, Spreadsheet ID, Realm ID, Status, Refresh Token. |
| **Control Sheet** | Per-client spreadsheet with a "JOBS CONTROL" tab; each row is a job (Country, Source File, Transform File, Tab Name, Month, Transform, QBO Sync, QBO Reconcile). |
| **Source File** | Raw data spreadsheet; contains a monthly tab (e.g. `2025-10`) with columns like Date, Category, Type, USD - QBO, QBO Method, etc. |
| **Transform File** | Output spreadsheet created/updated by ingestion; contains tabs like `{Country} {Month} - Journals`, `- Expenses`, `- Transfers`. |
| **Realm ID** | QuickBooks company identifier; used to target the correct QBO company. |

---

## 2. Architecture

```
┌─────────────────┐     ┌──────────────────┐     ┌─────────────────┐
│  Master Sheet   │────▶│  Per-Client     │────▶│  Source File    │
│  (clients list) │     │  Control Sheet  │     │  (raw data)     │
└─────────────────┘     └────────┬───────┘     └────────┬────────┘
                                  │                      │
                                  │    run_ingestion     │
                                  │◀────────────────────┘
                                  ▼
                         ┌──────────────────┐
                         │  Transform File  │  (Journals, Expenses, Transfers)
                         └────────┬─────────┘
                                  │
              ┌───────────────────┼───────────────────┐
              │ run_syncing       │ run_reconciliation │
              ▼                   ▼                    ▼
       ┌──────────────┐   ┌──────────────┐   ┌──────────────────┐
       │ QuickBooks   │   │ Reconcile    │   │ Sheet status      │
       │ Online (QBO) │   │ vs QBO/Raw   │   │ (Reconcile Status)│
       └──────────────┘   └──────────────┘   └──────────────────┘
```

- **Connectors:** `GSheetsClient` (Google Sheets + Drive), `QBOClient` (QuickBooks API, token from Master Sheet).
- **Logic:** `transformer.py` (raw → journals/expenses/transfers), `syncing.py` (push to QBO), `reconciler.py` (compare sheet vs QBO and raw vs transform).

---

## 3. Project Structure

```
QBO-Automation-Bot/
├── config/
│   ├── settings.py          # All config constants (sheet IDs, column names, env defaults)
│   ├── secrets.env          # Not committed; env vars (QBO keys, paths)
│   ├── oauth_client_secret.json  # Optional: Google OAuth client secret
│   ├── token.json           # Optional: Google OAuth token (after first login)
│   └── service_account.json # Optional: Google service account key
├── src/
│   ├── connectors/
│   │   ├── gsheets_client.py # Google Sheets & Drive API wrapper
│   │   └── qbo_client.py     # QuickBooks API client (auth, query, post)
│   ├── logic/
│   │   ├── transformer.py    # Raw → Journals, Expenses, Transfers
│   │   ├── syncing.py        # Push to QBO (journal, expense, transfer)
│   │   └── reconciler.py     # Reconcile sheet vs QBO and raw vs transform
│   └── utils/
│       └── logger.py         # Rotating file + console logger
├── logs/                    # Created at runtime; pipeline logs
├── run_ingestion.py         # Main entry: read Master → run transform per client
├── run_syncing.py           # Sync "Ready to sync" rows to QBO
├── run_reconciliation.py    # Reconcile and write status to sheets
├── server.py                # Flask webhook server (trigger ingestion/sync/reconcile)
├── bulk_del_qbo.py          # Bulk delete QBO entities by ID (JournalEntry, Purchase, Transfer)
├── requirements.txt         # Python dependencies
├── README.md
└── DOCUMENTATION.md         # This file
```

---

## 4. Configuration

### Environment Variables (`config/secrets.env`)

Create `config/secrets.env` (do not commit). Example:

```env
# QuickBooks Online (Intuit)
QBO_CLIENT_ID=your_client_id
QBO_CLIENT_SECRET=your_client_secret

# Google (optional overrides)
GSHEETS_AUTH_MODE=oauth
GOOGLE_OAUTH_TOKEN_PATH=config/token.json
GOOGLE_SERVICE_ACCOUNT_PATH=config/service_account.json

# Optional: override defaults from settings.py
# CONTROL_SHEET_ID=...
# MASTER_SHEET_ID=...
# QBO_BASE_URL=https://quickbooks.api.intuit.com
# QBO_TOKEN_URL=https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer
# QBO_MINOR_VERSION=65
# DEFAULT_BATCH_SIZE=5000
# RAW_COL_NO=No
# OUTPUT_TAB_JOURNALS=Journals
# OUTPUT_TAB_EXPENSES=Expenses
# OUTPUT_TAB_WITHDRAW=Transfers
```

### Config Constants (`config/settings.py`)

| Constant | Purpose |
|----------|---------|
| `MASTER_SHEET_ID`, `MASTER_TAB_NAME` | Master Sheet that lists clients. |
| `CONTROL_SHEET_ID`, `CONTROL_TAB_NAME` | Default control tab name (e.g. `JOBS CONTROL`); per-client sheet ID comes from Master. |
| `CTRL_COL_*` | Control sheet column names: Country, Source File, Transform File, Tab Name, Month, Transform, QBO Sync, Last Run At, Last Processed Row, etc. |
| `MST_COL_*` | Master sheet columns: Client Name, Spreadsheet ID, Realm ID, Status, Refresh Token, etc. |
| `OUTPUT_TAB_JOURNALS/EXPENSES/WITHDRAW` | Tab name suffixes (e.g. Journals, Expenses, Transfers). |
| `QBO_BASE_URL`, `QBO_TOKEN_URL`, `QBO_MINOR_VERSION` | QBO API endpoints and version. |
| `GSHEETS_AUTH_MODE` | `oauth` or `service_account`. |

### Google Sheets Authentication

- **OAuth (recommended):** Place `config/oauth_client_secret.json` and run once to generate `config/token.json` (browser login). Token is refreshed automatically when expired.
- **Service account:** Place `config/service_account.json` and share each spreadsheet with the service account email.

---

## 5. Setup & Installation

### Prerequisites

- Python 3.10+ (recommended)
- Google Cloud project with Sheets API and Drive API enabled
- Intuit Developer account and QBO app (Client ID, Client Secret)
- QuickBooks company (Realm ID) and OAuth refresh token per company

### Steps

1. **Clone and enter project:**
   ```bash
   cd d:\Projects\KZG\QBO-Automation-Bot
   ```

2. **Create virtual environment and install dependencies:**
   ```bash
   python -m venv .venv
   .venv\Scripts\activate   # Windows
   # source .venv/bin/activate   # Linux/macOS
   pip install -r requirements.txt
   ```

3. **Create `config/secrets.env`** with `QBO_CLIENT_ID`, `QBO_CLIENT_SECRET`, and any overrides (see [Configuration](#4-configuration)).

4. **Google auth:** Add `config/oauth_client_secret.json` (and run once to get `config/token.json`) or `config/service_account.json`.

5. **Master Sheet:** Create or use an existing Google Sheet with columns: Client Name, Spreadsheet ID, Realm ID, Status, Refresh Token (and any others your scripts expect). Set `MASTER_SHEET_ID` / `MASTER_TAB_NAME` in `settings.py` or env.

6. **Per-client Control Sheet:** Each client row in the Master Sheet should have a Spreadsheet ID pointing to a sheet that has a tab named `JOBS CONTROL` (or `CONTROL_TAB_NAME`) with columns: Country, Source File, Transform File, Tab Name, Month, Transform, QBO Sync, QBO Reconcile, Last Run At, Last Processed Row, Last Journal No, Last Expense No, Last Transfer No, etc.

7. **Optional (Windows certs):** If you see SSL/certificate errors, you can install `pip_system_certs` and the run scripts will wrap `requests` to use system certs (see top of `run_ingestion.py`).

---

## 6. Running the Bot

All entry points load `config/secrets.env` via `python-dotenv`.

| Script | Command | Purpose |
|--------|---------|---------|
| **Ingestion** | `python run_ingestion.py` | For each active client: read Control Sheet, process rows with Transform = READY, run transform, write to Transform File. |
| **Syncing** | `python run_syncing.py` | For each active client: process rows with QBO Sync = SYNC NOW; push Ready-to-sync rows to QBO; update Remarks, QBO ID, QBO Link. |
| **Reconciliation** | `python run_reconciliation.py` | For each active client: process rows with QBO Reconcile = RECONCILE NOW; compare sheet vs QBO and raw vs transform; write Reconcile Status / Raw Reconcile. |
| **Webhook server** | `python server.py` | Starts Flask on port 8000; accepts POSTs to `/webhook` to trigger ingestion, sync, or reconcile (optionally for a given client). |
| **Bulk delete** | `python bulk_del_qbo.py` | Script-configurable: set `TARGET_REALM_ID`, `ids_to_delete`, `ENTITY_TYPE` and run to delete those QBO entities. |

---

## 7. Pipeline Flow (Ingestion)

`run_ingestion.py` does the following:

1. **Read Master Sheet** and filter rows where Status = Active.
2. For each client:
   - **Set QBO context:** `qbo_client.set_company(realm_id)` (loads refresh token from Master Sheet).
   - **Fetch QBO mappings** (accounts, locations, classes, vendors, payment methods) via `QBOSync(qbo_client).mappings`.
   - **Read Control Sheet** (JOBS CONTROL tab).
3. For each Control row where **Transform** = `READY`:
   - **Create Transform File** if Transform File URL is missing (and copy permissions from Control Sheet).
   - **Retry handling:** Detect rows with Remarks containing "ERROR" or "Unbalance" in Journals/Expenses/Transfers tabs; collect row numbers to delete and existing IDs to preserve.
   - **Read raw data** from Source File, specified Tab Name (header row 1 for KZO, unformatted values), then normalize it to the canonical raw schema. KZO (all countries), KZP, KZDW, S5, and UMBER use header-based mappings; only unrecognizable legacy tabs fall back to positional mapping.
   - **Filters:** Exclude rows where "Check (Internal use)" contains "exclude"; filter by month (from Month column) using date range.
   - **Selection:** New rows (No > Last Processed Row) plus retry rows; then **delete** bad rows from output tabs and **run** `transform_raw(...)` with `last_jv`, `last_exp`, `last_tr`, `qbo_mappings`, and `existing_ids`.
   - **Write outputs** to Transform File: append or create tabs `{Country} {Month} - Journals`, `- Expenses`, `- Transfers` (using template tabs from Control Sheet if present).
   - **Update Control row:** Last Processed Row, Last Journal/Expense/Transfer No, Last Run At, Transform = DONE (or DONE (Empty), DONE (No Data)), and QBO Journal/Expense/Transfer status (READY TO SYNC or ERROR).

### Transformer (`src/logic/transformer.py`)

- **Input:** Raw DataFrame with columns such as No, Date, Category, Type, Item Description, USD - QBO, QBO Method, If Journal/Expense Method, QBO Transfer Fr/To, CO, etc.
- **Output:** `TransformResult`: three DataFrames (journals, expenses, withdraw/transfers) and updated running numbers.
- **Journals:** Rows with QBO Method containing "Journal" or "Reclass"; produces debit/credit lines with Journal No, Account, Location, Amount, Memo; validates accounts/locations against QBO mappings and balances; sets Remarks to "Ready to sync" or "ERROR | …".
- **Expenses:** Rows with QBO Method containing "Expense"; generates Exp Ref. No, Account (Cr), Expense Account (Dr), Payment Date, Expense Line Amount; validates and sets Remarks.
- **Transfers:** Rows with QBO Method containing "Transfer"; generates Ref No, Transfer Funds From/To, Transfer Amount; validates and sets Remarks.
- **ID generation:** Journals use prefix `KZO-JV` and running number; expenses `KZO{country}{mm yy}E…`; transfers `KZO{country}{mm yy}T…`. Retry rows keep existing IDs from `existing_ids`.

### KZO source layout (all countries)

Every KZO country tab (BR, TH, …) shares one header set, so `standardize_raw_df` maps KZO by **normalized header name**, never by column position. The 2026 layout spans `A:AA`:

| Col | Header | Col | Header |
| --- | --- | --- | --- |
| A | `CO` | O | `Actual USD Transacted` |
| B | `COY` | P | `Realised Loss` |
| C | `Date` | Q | `Transacted Amount Check` |
| D | `Category` | R | `Variance Check` |
| E | `Sub Category` | S | `USD - QBO` |
| F | `Item Description` | T | `Reclass/To check` |
| G | `TrxHash` | U | `QBO Import Method (Journal/Expenses/Transfer)` |
| H | `From Account` | V | `If Journal/Expense method: Another records` |
| I | `To Account` | W | `Transfer from` |
| J | `Currency From` | X | `Transfer to` |
| K | `Amount From` | Y | *(reserved)* |
| L | `Currency To` | Z | `Checking ( For our use only )` |
| M | `Amount To` | AA | row number (**header cell is blank**) |
| N | `Budget Rate` / `USD` | | |

Two properties of this layout drive the adapter:

- `Transacted Amount Check` (Q) and `Variance Check` (R) were inserted mid-sheet in 2026. Header-based mapping means they do **not** shift `USD - QBO`, the QBO method/account fields, or the row number.
- The row-number column at `AA` normally ships with an **empty header cell**, so it cannot be found by name. `_resolve_kzo_no_col()` takes the column immediately right of `Checking ( For our use only )` when no `No` / `No.` header exists; a real `No.` header still wins when present. Columns from `AB` onward hold dropdown-helper values and are ignored.

Pre-2026 KZO tabs (same headers, without `Transacted Amount Check` / `Variance Check`) go through the same header-based path. Tabs whose headers are not recognizable at all still fall back to the legacy positional mapping (first 26 columns → `RAW_STANDARD_COLUMNS`).

---

## 8. Syncing to QBO

`run_syncing.py` and `src/logic/syncing.py`:

1. For each active client, set QBO context and read Control Sheet.
2. For each row with **QBO Sync** = `SYNC NOW`:
   - Read Transform File tabs: `{Country} {Month} - Journals`, `- Expenses`, `- Transfers`.
   - For each tab, filter rows where Remarks contains "Ready to sync".
   - **Duplicate check:** Query QBO for existing DocNumber (Journals/Expenses) or PrivateNote (Transfers); skip rows already in QBO.
   - **Push:** Journals → `push_journal()` (grouped by Journal No); Expenses → `push_expense()` per row; Transfers → `push_transfer()` per row.
   - **Batch updates:** Every 50 rows (configurable), write back to the sheet: Remarks, QBO ID, QBO Link.
   - Set Control row: QBO Sync = DONE or PARTIAL ERROR; Last Sync At; QBO Journal/Expense/Transfer status (SYNCED / SYNC FAIL).

Mapping from sheet names to QBO entities uses `QBOSync.find_id()` (accounts, locations, classes, vendors, payment methods) with exact, leaf (post-`:`) and fuzzy (80%) matching.

---

## 9. Reconciliation

`run_reconciliation.py` and `src/logic/reconciler.py`:

1. For each active client, for each Control row with **QBO Reconcile** = `RECONCILE NOW`:
   - Optionally **fetch raw data** from Source File (same tab as ingestion) for raw-vs-transform comparison.
   - **Journals:** Fetch QBO JournalEntry for the month; match by QBO ID or DocNumber; compare date, memo; line-by-line match (account fuzzy + amount); write "Reconcile Status" per row. Also run **raw vs transform** (by No and amount) and write "Raw Reconcile".
   - **Expenses:** Same idea for Purchase (date, amount, payment account); plus raw vs transform.
   - **Transfers:** Same for Transfer (amount, date); plus raw vs transform.
   - Update Control: QBO Reconcile = DONE / DONE (Issues Found); Last Sync At; QBO Journal/Expense/Transfer = SYNCED or QBO MISMATCH.

Reconciler uses the same account-matching logic as syncing (exact, leaf, fuzzy) for consistent comparison.

---

## 10. Webhook Server

`server.py` runs a Flask app on `0.0.0.0:8000`.

- **Endpoint:** `POST /webhook`
- **Security:** Header `X-My-Secret-Token` must match `SECRET_TOKEN` in the script (change before use).
- **Body (JSON):** `event` (see below), optional `country` (client name to restrict run).
- **Events:**
  - `pipeline_trigger` → run `run_ingestion.py` (optionally with `--client <name>`).
  - `sync_trigger` → run `run_syncing.py`.
  - `reconcile_trigger` → run `run_reconciliation.py`.

Scripts are started in the background via `subprocess.Popen`. For `--client` support, the scripts must implement that argument (current code may not; you can add it with `argparse`).

---

## 11. Bulk Delete Utility

`bulk_del_qbo.py`:

- **Config (in script):** `TARGET_REALM_ID`, `ids_to_delete` (list of QBO IDs), `ENTITY_TYPE` (`JournalEntry`, `Purchase`, `Transfer`, `Deposit`).
- **Behavior:** Loads client from Master Sheet (or uses realm directly), fetches SyncToken for each ID, then sends batch delete requests to QBO. Results are printed and optionally saved to `deletion_log.csv`.

Use for one-off cleanup; ensure IDs and entity type are correct.

---

## 12. Scheduling & Automation

- **Cron (e.g. reconciliation every 5 minutes):**
  ```bash
  */5 * * * * /path/to/.venv/bin/python /path/to/run_reconciliation.py >> /path/to/logs/cron.log 2>&1
  ```
- **Webhook:** Use Google Apps Script or another scheduler to POST to your server’s `/webhook` with the desired `event` and optional `country` to trigger ingestion, sync, or reconcile.
- **New month:** Add a new row in the client’s Control Sheet: same Source File, new Tab Name (e.g. new month), Transform File can be blank (bot will create it), set Last Processed Row = 0, Transform = READY. No code change needed.

---

## 13. Troubleshooting

| Issue | What to check |
|-------|----------------|
| **Token / auth errors** | `config/secrets.env` present; QBO_CLIENT_ID/SECRET correct; Master Sheet has valid Refresh Token for that Realm ID; token.json exists and is valid for Sheets. |
| **"Realm ID not found"** | Master Sheet has a row with that Realm ID and column name matches `MST_COL_REALM_ID`. |
| **"Account not found" / mapping errors** | Accounts: exact or suffix-path name, or a row in the client's Account Map tab (README "Account Map tab"); never fuzzy. Locations/classes/vendors: exact, leaf or fuzzy. Add a Manual row to the Account Map tab for a renamed or differently written account. |
| **Unbalanced journal** | Transformer auto-adjusts small rounding (≤0.50); if larger, fix source data or rules in `process_journals`. |
| **Sync 401/403** | Refresh token expired or revoked; re-authorize app and update Refresh Token in Master Sheet. |
| **Sheets quota / 429** | GSheets client uses retry with backoff; reduce frequency of runs or batch size. |
| **Missing columns** | Control/Master/raw tabs must have the column names defined in `config/settings.py` (or aliases in transformer). |

Logs go to `logs/` (e.g. `pipeline.log`, `ingestion`, `reconciliation_runner`, `syncing_runner`) and to console.

---

## 14. Extending the Bot

- **Custom transform rules:** Edit `src/logic/transformer.py`: `transform_raw`, `process_journals`, `process_expenses`, `process_transfers`. Keep column names and ID formats consistent with syncing and reconciler.
- **New QBO entity types:** Add push methods in `src/logic/syncing.py` and call them from `run_syncing.py`; add reconciliation in `src/logic/reconciler.py` and `run_reconciliation.py`.
- **Client filter:** Add `argparse` to `run_ingestion.py`, `run_syncing.py`, `run_reconciliation.py` (e.g. `--client "Client Name"`) and filter the Master Sheet loop to that client; then in `server.py` pass `target_client` from the webhook body.
- **Plugging existing notebooks:** As in README: move cleaning/splitting from `Accounting_Clean.ipynb` into `transform_raw`; move QBO payload logic from `API_QBO_flow.ipynb` into a dedicated module (e.g. `src/logic/qbo_mapper.py`) and call from ingestion/syncing as needed.

---

## Quick Reference: Control Sheet Columns

| Column | Purpose |
|--------|---------|
| Country | Label for the job (e.g. country code). |
| Source File | URL or ID of raw data spreadsheet. |
| Transform File | URL or ID of output spreadsheet (created if blank). |
| Tab Name | Sheet name of raw data (e.g. `2025-10`). |
| Month | Month value for date filter and tab naming (e.g. `Oct 2025`). |
| Transform | READY → run ingestion; PROCESSING/DONE/ERROR set by bot. |
| QBO Sync | SYNC NOW → run sync; PROCESSING/DONE set by bot. |
| QBO Reconcile | RECONCILE NOW → run reconcile; RUNNING…/DONE set by bot. |
| Last Run At, Last Processed Row | Updated after ingestion. |
| Last Journal No, Last Expense No, Last Transfer No | Running counters. |
| QBO Journal, QBO Expense, QBO Transfer | Status per type (e.g. READY TO SYNC, SYNCED, QBO MISMATCH). |

---

*End of documentation.*
