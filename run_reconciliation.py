from __future__ import annotations
import argparse
import os
from contextlib import nullcontext
try:
    import pip_system_certs.wrappers
    pip_system_certs.wrappers.wrap_requests()
except ImportError:
    pass

from dotenv import load_dotenv
load_dotenv("config/secrets.env")

import pandas as pd
import calendar
from datetime import datetime
from config import settings
from src.connectors.gsheets_client import GSheetsClient
from src.connectors.qbo_client import QBOClient
from src.logic.reconciler import Reconciler
from src.logic.month_end import is_month_end_row
from src.utils.logger import setup_logger
from src.logic.raw_adapter import standardize_raw_df, read_s5_raw_df, RAW_STANDARD_COLUMNS
from src.utils.run_lock import single_instance_lock

logger = setup_logger("reconciliation_runner")
RECONCILE_ENABLE_RAW_CHECK = str(os.getenv("RECONCILE_ENABLE_RAW_CHECK", "1")).strip().lower() not in {
    "0", "false", "no", "off"
}

def _batch_update_control(gs, sheet_id, tab_name, row_num, columns, updates_dict):
    """Updates the Control Sheet."""
    headers = list(columns)
    batch_data = []
    for col_name, val in updates_dict.items():
        if col_name in headers:
            col_idx = headers.index(col_name) + 1
            batch_data.append({'row': row_num, 'col': col_idx, 'val': str(val)})
    if batch_data:
        gs.batch_update_cells(sheet_id, tab_name, batch_data)

def write_reconcile_results(gs, spreadsheet_url, tab_name, df, updates_list):
    """Writes status to the detailed Transform file."""
    if not updates_list: return
    target_col_name = "Reconcile Status"
    if target_col_name in df.columns:
        col_idx = df.columns.get_loc(target_col_name) + 1
    else:
        col_idx = len(df.columns) + 1
        gs.update_cell(spreadsheet_url, tab_name, 1, col_idx, target_col_name)
    
    batch_payload = []
    for item in updates_list:
        batch_payload.append({"row": item["row_idx"]+2, "col": col_idx, "val": item["status"]})
    gs.batch_update_cells(spreadsheet_url, tab_name, batch_payload)

def write_raw_check_results(gs, spreadsheet_url, tab_name, df, updates_list):
    """Writes 'Raw Status' to the Transform file."""
    if not updates_list: return
    target_col_name = "Raw Reconcile"
    
    # Check if column exists, if not add it
    if target_col_name in df.columns:
        col_idx = df.columns.get_loc(target_col_name) + 1
    else:
        col_idx = len(df.columns) + 1
        gs.update_cell(spreadsheet_url, tab_name, 1, col_idx, target_col_name)
    
    batch_payload = []
    for item in updates_list:
        # +2 offset for 0-index + header row
        batch_payload.append({"row": item["row_idx"]+2, "col": col_idx, "val": item["status"]})
    
    # print("Add Raw Reconcile")
    
    gs.batch_update_cells(spreadsheet_url, tab_name, batch_payload)


def _filter_raw_df_by_month(raw_df: pd.DataFrame, month_str: str, client_name: str) -> pd.DataFrame:
    """
    Keep only raw rows within the reconciliation month.
    This prevents cross-month duplicate 'No' values from affecting raw reconcile.
    """
    if raw_df is None or raw_df.empty or "Date" not in raw_df.columns:
        return raw_df

    month_dt = pd.to_datetime(month_str, errors="coerce")
    if pd.isna(month_dt):
        logger.warning(f"   ⚠️ [{client_name}] Raw month filter skipped: invalid month '{month_str}'")
        return raw_df

    last_day = calendar.monthrange(int(month_dt.year), int(month_dt.month))[1]
    start = pd.Timestamp(year=int(month_dt.year), month=int(month_dt.month), day=1)
    end = pd.Timestamp(year=int(month_dt.year), month=int(month_dt.month), day=last_day)

    # Handle duplicate "Date" headers by taking the first usable one.
    date_candidates = raw_df.loc[:, raw_df.columns == "Date"]
    if date_candidates.empty:
        return raw_df
    date_raw = date_candidates.iloc[:, 0]

    # Parse common text formats first.
    date_text = date_raw.astype(str).str.strip()
    slash_like = date_text.str.match(r"^\d{1,2}/\d{1,2}/\d{2,4}$", na=False)

    # For slash dates, enforce m/d/yyyy (or m/d/yy).
    parsed_slash_mdy = pd.to_datetime(date_text.where(slash_like), format="%m/%d/%Y", errors="coerce")
    parsed_slash_mdy_short = pd.to_datetime(date_text.where(slash_like), format="%m/%d/%y", errors="coerce")

    parsed_mdy = pd.to_datetime(date_text, errors="coerce", dayfirst=False)
    parsed_dmy = pd.to_datetime(date_text, errors="coerce", dayfirst=True)
    parsed = parsed_slash_mdy.fillna(parsed_slash_mdy_short).fillna(parsed_mdy).fillna(parsed_dmy)

    # Numeric date strategies:
    # 1) Excel serial days
    # 2) yyyymmdd integers
    # 3) Unix epoch seconds / milliseconds
    numeric_dates = pd.to_numeric(date_raw, errors="coerce")

    excel_base = numeric_dates.where((numeric_dates >= 20000) & (numeric_dates <= 90000))
    excel_dates = pd.to_datetime(excel_base, unit="D", origin="1899-12-30", errors="coerce")
    parsed = parsed.fillna(excel_dates)

    yyyymmdd_base = numeric_dates.where((numeric_dates >= 19000101) & (numeric_dates <= 20991231))
    yyyymmdd_int = yyyymmdd_base.round().astype("Int64")
    yyyymmdd_dates = pd.to_datetime(yyyymmdd_int.astype(str), format="%Y%m%d", errors="coerce")
    parsed = parsed.fillna(yyyymmdd_dates)

    epoch_s_base = numeric_dates.where((numeric_dates >= 946684800) & (numeric_dates <= 4102444800))
    epoch_s_dates = pd.to_datetime(epoch_s_base, unit="s", errors="coerce")
    parsed = parsed.fillna(epoch_s_dates)

    epoch_ms_base = numeric_dates.where((numeric_dates >= 946684800000) & (numeric_dates <= 4102444800000))
    epoch_ms_dates = pd.to_datetime(epoch_ms_base, unit="ms", errors="coerce")
    parsed = parsed.fillna(epoch_ms_dates)

    parsed_count = int(parsed.notna().sum())
    if parsed_count == 0:
        logger.warning(
            f"   ⚠️ [{client_name}] Raw month filter skipped: no parseable Date values; keeping all {len(raw_df)} rows."
        )
        return raw_df

    in_month = parsed.between(start, end, inclusive="both")
    filtered = raw_df.loc[in_month].copy()

    dropped = int(len(raw_df) - len(filtered))
    if dropped > 0:
        logger.info(
            f"   🧹 [{client_name}] Raw month filter: kept {len(filtered)}/{len(raw_df)} rows "
            f"for {start.strftime('%Y-%m')}."
        )
    if filtered.empty:
        valid_dates = parsed.dropna()
        if not valid_dates.empty:
            logger.warning(
                f"   ⚠️ [{client_name}] Raw Date parsed range: "
                f"{valid_dates.min().strftime('%Y-%m-%d')} -> {valid_dates.max().strftime('%Y-%m-%d')} "
                f"(target month: {start.strftime('%Y-%m')})."
            )
        logger.warning(
            f"   ⚠️ [{client_name}] Raw month filter produced 0 rows for {start.strftime('%Y-%m')}; "
            "Raw Reconcile will be skipped."
        )
    return filtered

# ==============================================================================
# LOGIC: PROCESS ONE CLIENT
# ==============================================================================
def process_client_reconcile(
    gs: GSheetsClient,
    qbo_client: QBOClient,
    control_sheet_id: str,
    client_name: str,
    realm_id: str,
):
    logger.info(f"📂 [{client_name}] Reading Control Sheet (ID: {control_sheet_id})...")
    
    try:
        ctrl_df = gs.read_as_df(control_sheet_id, settings.CONTROL_TAB_NAME)
    except Exception as e:
        logger.error(f"   ❌ [{client_name}] Failed to read Control Sheet: {e}")
        return
    
    if ctrl_df.empty:
        return

    CTRL_COL_RECONCILE = "QBO Reconcile"
    COL_QBO_JV = "QBO Journal"
    COL_QBO_EXP = "QBO Expense"
    COL_QBO_TR = "QBO Transfer"

    status_series = ctrl_df.get(CTRL_COL_RECONCILE, pd.Series("", index=ctrl_df.index))
    reconcile_now_count = int(status_series.astype(str).str.strip().eq("RECONCILE NOW").sum())
    if reconcile_now_count == 0:
        logger.info(f"   ⏭️ [{client_name}] No RECONCILE NOW rows. Skipping QBO auth.")
        return

    try:
        logger.info(f"🔐 [{client_name}] Authenticating with Realm ID: {realm_id}")
        qbo_client.set_company(realm_id)
        logger.info(f"✅ [{client_name}] Authenticated. Ready to reconcile.")
    except Exception as e:
        logger.error(f"❌ Failed auth for {client_name}: {e}")
        return

    # Initialize Reconciler
    reconciler = Reconciler(qbo_client)

    for i, row in ctrl_df.iterrows():
        status = str(row.get(CTRL_COL_RECONCILE, "")).strip()
        if status != "RECONCILE NOW": continue

        row_num = i + 2
        country = row.get(settings.CTRL_COL_COUNTRY)
        is_month_end = is_month_end_row(country, row.get(settings.CTRL_COL_TAB_NAME))
        if is_month_end:
            country = settings.MONTH_END_COUNTRY  # same tab names Transform wrote
        transform_url = row.get(settings.CTRL_COL_TRANSFORM_URL)
        month_str = str(row.get(settings.CTRL_COL_MONTH, ""))
        raw_month = month_str.strip()

        if not transform_url or not month_str or month_str.lower() == "nan":
            _batch_update_control(gs, control_sheet_id, settings.CONTROL_TAB_NAME, row_num, ctrl_df.columns, {CTRL_COL_RECONCILE: "ERROR: Missing Info"})
            continue

        logger.info(f"⚖️  [{client_name}] Reconciling {country}...")
        _batch_update_control(gs, control_sheet_id, settings.CONTROL_TAB_NAME, row_num, ctrl_df.columns, {CTRL_COL_RECONCILE: "RUNNING..."})

        row_updates = {}
        has_issue = False
        dt_label = pd.to_datetime(month_str).strftime("%b %y")

        # --- NEW: Fetch Raw Data for Comparison ---
        source_url = row.get(settings.CTRL_COL_SOURCE_URL)
        raw_tab_name = row.get(settings.CTRL_COL_TAB_NAME)

        raw_df = pd.DataFrame()

        if is_month_end:
            # Month-end lines have no raw 'No' to compare; the QBO check below still runs.
            logger.info(f"   ⏭️ [{client_name}] Month-end JV row: raw-vs-transform check skipped.")
        elif RECONCILE_ENABLE_RAW_CHECK:
            try:
                if source_url and raw_tab_name:
                    logger.info(f"   📥 [{client_name}] Fetching Raw Source for Validation...")
                    client_name_lower = str(client_name).lower()
                    if "s5" in client_name_lower:
                        # S5's header row moves with the wallet-balance block above the
                        # grid, so it is located by content rather than fixed at row 19.
                        raw_df = read_s5_raw_df(gs, source_url, raw_tab_name, client_name)
                    else:
                        if settings.is_kzdw_family(client_name):
                            source_header_row = 5
                        elif "kzp" in client_name_lower:
                            source_header_row = 4
                        elif "umber" in client_name_lower:
                            source_header_row = 4
                        else:
                            source_header_row = 1
                        raw_df = gs.read_as_df(
                            source_url,
                            raw_tab_name,
                            header_row=source_header_row,
                            value_render_option='UNFORMATTED_VALUE'
                        )
                    raw_df = standardize_raw_df(raw_df, client_name=client_name, raw_month=raw_month)
                    # standardize_raw_df already returns the canonical schema, so reindex by
                    # name (as run_ingestion does) instead of renaming the first 25 columns by
                    # position -- a positional rename silently mislabels every column if the
                    # canonical order ever changes.
                    raw_df = raw_df.reindex(columns=RAW_STANDARD_COLUMNS, fill_value="")

                    # 8. Numeric Cleanup
                    for col in ["No", "USD - QBO", "Amount Fr", "Amount To"]:
                        if col in raw_df.columns:
                            raw_df[col] = pd.to_numeric(raw_df[col], errors="coerce").fillna(0)

                    # Keep KZP/S5/Umber raw rows in the target month to avoid cross-month duplicate "No".
                    # KZO uses mixed historical layouts, so date filtering is skipped there.
                    if any(x in str(client_name).lower() for x in ["kzp", "s5", "umber"]):
                        raw_df = _filter_raw_df_by_month(raw_df, month_str, client_name)
                    else:
                        logger.info(f"   ⏭️ [{client_name}] Raw month filter skipped (enabled for KZP/S5/Umber only).")

            except Exception as e:
                logger.error(f"   ⚠️ Failed to read Raw Source: {e}")
        else:
            logger.info(f"   ⏭️ [{client_name}] Raw-vs-transform check disabled by RECONCILE_ENABLE_RAW_CHECK.")

        # 1. Reconcile Journals
        try:
            tab = f"{country} {dt_label} - {settings.OUTPUT_TAB_JOURNALS}"
            try: df = gs.read_as_df_sync(transform_url, tab)
            except: df = pd.DataFrame()

            if not df.empty:
                # A. QBO Reconcile (Existing)
                res_qbo = reconciler.reconcile_journals(df, month_str)
                write_reconcile_results(gs, transform_url, tab, df, res_qbo)
                
                # B. Raw vs Transform Reconcile (NEW)
                if not raw_df.empty:
                    # print("RUN")
                    res_raw = reconciler.reconcile_raw_vs_transform(raw_df, df, "JournalEntry", client_name)
                    write_raw_check_results(gs, transform_url, tab, df, res_raw)

                if any("Mismatch" in r["status"] or "Missing" in r["status"] for r in res_qbo):
                    row_updates[COL_QBO_JV] = "QBO MISMATCH"
                    has_issue = True
                else:
                    row_updates[COL_QBO_JV] = "SYNCED"
        except Exception as e:
            logger.exception(f"   ❌ JV Reconcile Error: {e}")
            has_issue = True

        # 2. Reconcile Expenses
        try:
            tab = f"{country} {dt_label} - {settings.OUTPUT_TAB_EXPENSES}"
            try: df = gs.read_as_df_sync(transform_url, tab)
            except: df = pd.DataFrame()

            if not df.empty:
                # A. QBO Reconcile
                res_qbo = reconciler.reconcile_expenses(df, month_str)
                write_reconcile_results(gs, transform_url, tab, df, res_qbo)
                
                # B. Raw Check
                if not raw_df.empty:
                    res_raw = reconciler.reconcile_raw_vs_transform(raw_df, df, "Purchase", client_name)
                    write_raw_check_results(gs, transform_url, tab, df, res_raw)

                if any("Mismatch" in r["status"] or "Missing" in r["status"] for r in res_qbo):
                    row_updates[COL_QBO_EXP] = "QBO MISMATCH"
                    has_issue = True
                else:
                    row_updates[COL_QBO_EXP] = "SYNCED"
        except Exception as e:
            logger.exception(f"   ❌ Exp Reconcile Error: {e}")
            has_issue = True

        # 3. Reconcile Transfers
        try:
            tab = f"{country} {dt_label} - {settings.OUTPUT_TAB_WITHDRAW}"
            try: df = gs.read_as_df_sync(transform_url, tab)
            except: df = pd.DataFrame()

            if not df.empty:
                # A. QBO Reconcile
                res_qbo = reconciler.reconcile_transfers(df, month_str)
                write_reconcile_results(gs, transform_url, tab, df, res_qbo)
                
                # B. Raw Check
                if not raw_df.empty:
                    res_raw = reconciler.reconcile_raw_vs_transform(raw_df, df, "Transfer", client_name)
                    write_raw_check_results(gs, transform_url, tab, df, res_raw)

                if any("Mismatch" in r["status"] or "Missing" in r["status"] for r in res_qbo):
                    row_updates[COL_QBO_TR] = "QBO MISMATCH"
                    has_issue = True
                else:
                    row_updates[COL_QBO_TR] = "SYNCED"
        except Exception as e:
            logger.exception(f"   ❌ Trf Reconcile Error: {e}")
            has_issue = True

        final = "DONE (Issues Found)" if has_issue else "DONE"
        row_updates[CTRL_COL_RECONCILE] = final
        row_updates["Last Sync At"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        try:
            _batch_update_control(gs, control_sheet_id, settings.CONTROL_TAB_NAME, row_num, ctrl_df.columns, row_updates)
        except Exception as e:
            # Never let a control-sheet write failure (e.g. a cell-size/API error) abort the
            # rest of this client's rows -- log it and move on to the next row.
            logger.error(f"   ❌ [{client_name}] Failed to write control-sheet updates for row {row_num}: {e}")
        logger.info(f"✅ [{client_name}] Reconcile Complete: {final}")


# ==============================================================================
# MAIN ENTRY POINT
# ==============================================================================
def _is_target_client(client_row: pd.Series, target_client: str | None) -> bool:
    if not target_client:
        return True

    target = str(target_client).strip()
    if not target:
        return True
    target_norm = settings.normalize_workspace_name(target)
    if target_norm in {"all", "*", "all clients"}:
        return True

    row_client = str(client_row.get(settings.MST_COL_CLIENT, "")).strip()
    row_realm = str(client_row.get(settings.MST_COL_REALM_ID, "")).strip()
    row_sheet_id = str(client_row.get(settings.MST_COL_SHEET_ID, "")).strip()
    row_folder_id = str(client_row.get(settings.MST_COL_OUTPUT, "")).strip()

    if target == row_realm:
        return True
    if target == row_sheet_id:
        return True
    if target == row_folder_id:
        return True
    return target_norm == settings.normalize_workspace_name(row_client)

def _target_is_all(target_client: str | None) -> bool:
    if not target_client:
        return True
    t = settings.normalize_workspace_name(target_client)
    return t in {"", "all", "*", "all clients"}

def main(target_client: str | None = None):
    target_is_all = _target_is_all(target_client)
    dispatch_ctx = single_instance_lock("run_reconciliation_all_dispatch") if target_is_all else nullcontext(True)
    with dispatch_ctx as acquired:
        if target_is_all and not acquired:
            logger.warning("Another ALL reconciliation dispatch is already in progress. Skipping this run.")
            return

        gs = GSheetsClient()
        qbo_client = QBOClient(gs_client=gs)

        logger.info("🌍 Reading MASTER SHEET for Reconcile Jobs...")
        try:
            master_df = gs.read_as_df(settings.MASTER_SHEET_ID, settings.MASTER_TAB_NAME)
        except Exception as e:
            logger.error(f"❌ Critical: {e}")
            return

        # Normalize headers to avoid silent misses from extra spaces/newlines in sheet columns.
        master_df.columns = [" ".join(str(c).replace("\n", " ").split()) for c in master_df.columns]

        matched_clients = 0
        for i, client_row in master_df.iterrows():
            if not _is_target_client(client_row, target_client):
                continue
            matched_clients += 1

            client_name = str(client_row.get(settings.MST_COL_CLIENT, "Unknown"))
            if str(client_row.get(settings.MST_COL_STATUS, "")).strip().lower() != "active": continue
            if not settings.is_allowed_workspace(client_name):
                logger.warning(
                    f"⚠️ Skipping {client_name}: workspace not allowed for QBO API. "
                    f"Allowed: {', '.join(settings.ALLOWED_QBO_WORKSPACES)}"
                )
                continue

            sheet_id = str(client_row.get(settings.MST_COL_SHEET_ID, "")).strip()
            realm_id = str(client_row.get(settings.MST_COL_REALM_ID, "")).strip()

            if not sheet_id or not realm_id: continue

            print(f"\n🏢 RECONCILING CLIENT: {client_name} (Realm: {realm_id})")

            client_lock_name = f"run_reconciliation_client_{realm_id}"
            with single_instance_lock(client_lock_name) as client_acquired:
                if not client_acquired:
                    logger.warning(
                        f"⏭️ Skipping {client_name}: another reconciliation run is already processing Realm {realm_id}."
                    )
                    continue
                try:
                    process_client_reconcile(gs, qbo_client, sheet_id, client_name, realm_id)
                except Exception as e:
                    logger.error(f"❌ Failed client {client_name}: {e}")

        if target_client and matched_clients == 0:
            logger.warning(f"No client matched target '{target_client}'.")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run QBO reconciliation pipeline.")
    parser.add_argument(
        "--client",
        dest="client",
        default="",
        help="Target client name, Realm ID, Spreadsheet ID, or Output Folder ID.",
    )
    args = parser.parse_args()
    main(target_client=args.client)
