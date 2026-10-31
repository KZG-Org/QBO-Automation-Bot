from __future__ import annotations
import argparse
import os
import time
import threading
import queue
from contextlib import nullcontext
# --- FIX 1: Load Secrets ---
from dotenv import load_dotenv
load_dotenv("config/secrets.env")
# ---------------------------

try:
    import pip_system_certs.wrappers
    pip_system_certs.wrappers.wrap_requests()
except ImportError:
    pass

import calendar
import pandas as pd
from datetime import datetime
from config import settings
from src.connectors.gsheets_client import GSheetsClient
from src.connectors.qbo_client import QBOClient
from src.logic.syncing import QBOSync
from src.logic.month_end import (
    CLASH_REMARK,
    find_number_clashes,
    is_month_end_row,
    read_source_tab,
    sync_status_updates,
    synced_journal_nos,
)
from src.utils.logger import setup_logger
from src.utils.run_lock import single_instance_lock

logger = setup_logger("syncing_runner")

def _env_int(name: str, default: int) -> int:
    try:
        return max(1, int(os.getenv(name, str(default))))
    except Exception:
        return default

def _env_float(name: str, default: float) -> float:
    try:
        return max(0.0, float(os.getenv(name, str(default))))
    except Exception:
        return default

# Sync pacing controls:
# - patch size for status updates to sheet
# - delay after each QBO call
# - delay after each sheet patch flush
SYNC_PATCH_SIZE = _env_int("QBO_SYNC_PATCH_SIZE", 20)
QBO_SYNC_CALL_DELAY_SEC = _env_float("QBO_SYNC_CALL_DELAY_SEC", 0.20)
QBO_SYNC_PATCH_DELAY_SEC = _env_float("QBO_SYNC_PATCH_DELAY_SEC", 0.25)
SYNC_PROGRESS_LOG_EVERY = _env_int("QBO_SYNC_PROGRESS_LOG_EVERY", 10)

def _throttle_qbo_call():
    if QBO_SYNC_CALL_DELAY_SEC > 0:
        time.sleep(QBO_SYNC_CALL_DELAY_SEC)

class AsyncSheetUpdater:
    """
    Writes row updates to a sheet on a background thread so QBO push and
    sheet write-back can run in parallel. Any unrecoverable write error is
    propagated on close() to avoid false "SYNCED" results.
    """
    _STOP = object()

    def __init__(self, gs, spreadsheet_url: str, tab_name: str, col_ctx: dict, batch_size: int):
        self.gs = gs
        self.spreadsheet_url = spreadsheet_url
        self.tab_name = tab_name
        self.col_ctx = col_ctx
        self.batch_size = max(1, int(batch_size))
        self._queue: queue.Queue = queue.Queue()
        self._error: Exception | None = None
        self._thread = threading.Thread(target=self._worker, daemon=True)
        self._thread.start()

    def enqueue(self, item: dict):
        self._queue.put(item)

    def _flush_batch(self, batch: list[dict]):
        if not batch:
            return
        logger.info(f"   >>> Flushing {len(batch)} updates to Sheet...")
        last_err = None
        for attempt in range(1, 4):
            try:
                _update_row_status_and_id(
                    self.gs,
                    self.spreadsheet_url,
                    self.tab_name,
                    batch,
                    col_ctx=self.col_ctx,
                )
                if QBO_SYNC_PATCH_DELAY_SEC > 0:
                    time.sleep(QBO_SYNC_PATCH_DELAY_SEC)
                return
            except Exception as e:
                last_err = e
                logger.error(f"Sheet flush failed (attempt {attempt}/3): {e}")
                time.sleep(0.5 * attempt)
        raise RuntimeError(
            f"Failed to write {len(batch)} updates to sheet tab '{self.tab_name}'"
        ) from last_err

    def _worker(self):
        batch = []
        try:
            while True:
                item = self._queue.get()
                if item is self._STOP:
                    break
                batch.append(item)
                if len(batch) >= self.batch_size:
                    self._flush_batch(batch)
                    batch = []
            if batch:
                self._flush_batch(batch)
        except Exception as e:
            self._error = e

    def close(self):
        self._queue.put(self._STOP)
        self._thread.join()
        if self._error:
            raise self._error

def _build_status_col_ctx(gs, spreadsheet_url, tab_name, headers: list[str]) -> dict:
    """
    Resolve and cache column positions once per tab to avoid repeated sheet header reads.
    Ensures Remarks/QBO ID/QBO Link exist.
    """
    working_headers = list(headers or [])
    required = ["Remarks", "QBO ID", "QBO Link"]
    for col_name in required:
        if col_name not in working_headers:
            col_idx = len(working_headers) + 1
            gs.update_cell(spreadsheet_url, tab_name, 1, col_idx, col_name)
            working_headers.append(col_name)
    return {
        "col_rem": working_headers.index("Remarks") + 1,
        "col_id": working_headers.index("QBO ID") + 1,
        "col_link": working_headers.index("QBO Link") + 1,
    }

def _should_log_progress(i: int, total: int) -> bool:
    pos = i + 1
    if pos == 1 or pos == total:
        return True
    return (pos % SYNC_PROGRESS_LOG_EVERY) == 0

def _month_bounds(month_str: str) -> tuple[str | None, str | None]:
    """First/last day of month_str as 'YYYY-MM-DD', or (None, None) if unparseable."""
    dt = pd.to_datetime(month_str, errors="coerce")
    if pd.isna(dt):
        return None, None
    last_day = calendar.monthrange(int(dt.year), int(dt.month))[1]
    return f"{dt.year}-{dt.month:02d}-01", f"{dt.year}-{dt.month:02d}-{last_day}"

def _batch_update_control(gs, sheet_id, tab_name, row_num, columns, updates_dict):
    headers = list(columns)
    batch_data = []
    for col_name, val in updates_dict.items():
        if col_name in headers:
            col_idx = headers.index(col_name) + 1
            batch_data.append({'row': row_num, 'col': col_idx, 'val': str(val)})
    if batch_data:
        gs.batch_update_cells(sheet_id, tab_name, batch_data)

def _update_row_status_and_id(gs, spreadsheet_url, tab_name, updates: list, col_ctx: dict | None = None):
    """
    Updates:
    - Remarks
    - QBO ID (raw number)
    - QBO Link (raw URL)
    """
    if not updates:
        return

    if not col_ctx:
        df_header = gs.read_as_df(spreadsheet_url, tab_name)
        col_ctx = _build_status_col_ctx(gs, spreadsheet_url, tab_name, df_header.columns.tolist())

    col_rem = col_ctx["col_rem"]
    col_id = col_ctx["col_id"]
    col_link = col_ctx["col_link"]

    batch_payload = []

    for item in updates:
        row_no = item["row_idx"] + 2

        # Remarks
        batch_payload.append({
            "row": row_no,
            "col": col_rem,
            "val": item["status"]
        })

        # QBO ID
        if item.get("qbo_id"):
            batch_payload.append({
                "row": row_no,
                "col": col_id,
                "val": str(item["qbo_id"])
            })

        # QBO Link (RAW URL)
        if item.get("qbo_link"):
            batch_payload.append({
                "row": row_no,
                "col": col_link,
                "val": item["qbo_link"]
            })

    gs.batch_update_cells(spreadsheet_url, tab_name, batch_payload)

def _write_back_month_end_status(gs, row: pd.Series, synced_jvs: set[str]):
    """Month-end rows: set the source Status of posted journals to 'Synced <JV>'.

    Never fatal: QBO already holds the journals, and the next sync writes it again because
    synced_jvs also covers journals the Transform tab already shows as synced.
    """
    source_url = str(row.get(settings.CTRL_COL_SOURCE_URL, "")).strip()
    tab_name = str(row.get(settings.CTRL_COL_TAB_NAME, "")).strip()
    try:
        source = read_source_tab(gs.read_values(source_url, tab_name, value_render_option="UNFORMATTED_VALUE"))
        updates = sync_status_updates(source, synced_jvs)
        if updates:
            gs.batch_update_cells(source_url, tab_name, updates)
            logger.info(f"   ✅ Month-end: updated Status on {len(updates)} source line(s) in '{tab_name}'.")
    except Exception as e:
        logger.warning(f"   ⚠️ Month-end: could not update Status in '{tab_name}': {e}")


def process_client_sync(
    gs: GSheetsClient,
    qbo_client: QBOClient,
    control_sheet_id: str,
    client_name: str,
    realm_id: str,
):
    BATCH_SIZE = SYNC_PATCH_SIZE
    logger.info(f"📂 [{client_name}] Processing Control Sheet...")
    try:
        ctrl_df = gs.read_as_df(control_sheet_id, settings.CONTROL_TAB_NAME)
    except Exception as e:
        logger.error(f"   ❌ [{client_name}] Failed to read Control Sheet: {e}")
        return

    if ctrl_df.empty:
        return

    status_series = ctrl_df.get(settings.CTRL_COL_QBO_SYNC, pd.Series("", index=ctrl_df.index))
    sync_now_count = int(status_series.astype(str).str.strip().eq("SYNC NOW").sum())
    if sync_now_count == 0:
        logger.info(f"   ⏭️ [{client_name}] No SYNC NOW rows. Skipping QBO auth/mappings.")
        return

    try:
        logger.info(f"🔐 [{client_name}] Authenticating with Realm ID: {realm_id}")
        qbo_client.set_company(realm_id)
        logger.info(f"✅ [{client_name}] Authenticated. Ready to sync.")
    except Exception as e:
        logger.error(f"❌ Auth/Sync failed for {client_name}: {e}")
        return

    sync_engine = QBOSync(client=qbo_client)

    COL_QBO_JV = "QBO Journal"
    COL_QBO_EXP = "QBO Expense"
    COL_QBO_TR = "QBO Transfer"

    for i, row in ctrl_df.iterrows():
        if str(row.get(settings.CTRL_COL_QBO_SYNC, "")).strip() != "SYNC NOW": continue
        
        row_num = i + 2
        country = str(row.get(settings.CTRL_COL_COUNTRY, "")).strip()
        transform_url = str(row.get(settings.CTRL_COL_TRANSFORM_URL, "")).strip()
        month_str = str(row.get(settings.CTRL_COL_MONTH, "")).strip()
        
        if not transform_url or not month_str: continue

        _batch_update_control(gs, control_sheet_id, settings.CONTROL_TAB_NAME, row_num, ctrl_df.columns, {settings.CTRL_COL_QBO_SYNC: "PROCESSING"})
        
        has_error = False
        jv_status, exp_status, tr_status = "Skipped", "Skipped", "Skipped"
        is_month_end = is_month_end_row(country, row.get(settings.CTRL_COL_TAB_NAME, ""))
        if is_month_end:
            country = settings.MONTH_END_COUNTRY  # same tab names Transform wrote

        dt_label = pd.to_datetime(month_str).strftime("%b %y")
        tab_jv = f"{country} {dt_label} - Journals"
        tab_exp = f"{country} {dt_label} - Expenses"
        tab_tr = f"{country} {dt_label} - Transfers"

        # Bounds for the Transfer duplicate check. QBO can't filter on PrivateNote (where the
        # Ref No lives), so the check scans notes locally -- scoping the fetch to this month
        # keeps it from pulling the entire Transfer history on every run.
        month_start, month_end = _month_bounds(month_str)

        # ---------------------------------------------------------
        # 1. SYNC JOURNALS
        # ---------------------------------------------------------
        posted_jvs: set[str] = set()
        clash_jvs: set[str] = set()
        df_jv = pd.DataFrame()
        try:
            logger.info(f"   Using Tab: {tab_jv}")
            try: df_jv = gs.read_as_df_sync(transform_url, tab_jv)
            except: df_jv = pd.DataFrame()

            if not df_jv.empty and "Remarks" in df_jv.columns:
                col_ctx = _build_status_col_ctx(gs, transform_url, tab_jv, df_jv.columns.tolist())
                to_sync = df_jv[df_jv["Remarks"].astype(str).str.contains("Ready to sync", case=False, na=False)]
                
                if to_sync.empty:
                    jv_status = "SYNCED"
                else:
                    all_jv_nos = to_sync["Journal No"].unique().tolist()
                    existing_docs = sync_engine.get_existing_duplicates("JournalEntry", all_jv_nos)
                    # Month-end rows: a number already in QBO is only skipped when it is this
                    # same journal; otherwise the journal would be lost without an error.
                    if is_month_end and existing_docs:
                        clash_jvs = find_number_clashes(
                            to_sync, existing_docs, sync_engine.get_journal_details(list(existing_docs))
                        )
                    updater = AsyncSheetUpdater(gs, transform_url, tab_jv, col_ctx=col_ctx, batch_size=BATCH_SIZE)
                    section_fail = False
                    try:
                        for jv_no, group in to_sync.groupby("Journal No"):
                            if str(jv_no) in clash_jvs:
                                msg = CLASH_REMARK.format(jv=jv_no)
                                logger.error(f"   ❌ {msg}")
                                has_error = True
                                section_fail = True
                                for idx in group.index:
                                    updater.enqueue({"row_idx": idx, "status": msg, "qbo_id": "", "qbo_link": ""})
                                continue

                            if str(jv_no) in existing_docs:
                                already_synced_msg = f"Skipper (Already synced in QBO at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')})"
                                for idx in group.index:
                                    updater.enqueue({"row_idx": idx, "status": already_synced_msg, "qbo_id": "", "qbo_link": ""})
                                posted_jvs.add(str(jv_no))
                                continue

                            try:
                                resp = sync_engine.push_journal(jv_no, group)
                                posted_jvs.add(str(jv_no))
                                _throttle_qbo_call()
                                new_id = resp.get("JournalEntry", {}).get("Id", "")
                                qbo_link = sync_engine.build_qbo_url("JournalEntry", new_id) if new_id else ""
                                msg = f"Synced at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"

                                for idx in group.index:
                                    updater.enqueue({
                                        "row_idx": idx,
                                        "status": msg,
                                        "qbo_id": new_id,
                                        "qbo_link": qbo_link
                                    })
                            except Exception as e:
                                msg = f"ERROR: {str(e)}"
                                has_error = True
                                section_fail = True
                                _throttle_qbo_call()
                                for idx in group.index:
                                    updater.enqueue({"row_idx": idx, "status": msg, "qbo_id": "", "qbo_link": ""})
                    finally:
                        updater.close()
                    jv_status = "SYNC FAIL" if section_fail else "SYNCED"

            if is_month_end and not df_jv.empty:
                _write_back_month_end_status(gs, row, synced_journal_nos(df_jv) | posted_jvs)
        except Exception as e:
            logger.error(f"   ❌ Journal Sync Fail: {e}")
            has_error = True
            jv_status = "SYNC FAIL"

        # ---------------------------------------------------------
        # 2. SYNC EXPENSES (UPDATED WITH BATCHING)
        # ---------------------------------------------------------
        try:
            logger.info(f"   Using Tab: {tab_exp}")
            try: df_exp = gs.read_as_df_sync(transform_url, tab_exp)
            except: df_exp = pd.DataFrame()

            if not df_exp.empty and "Remarks" in df_exp.columns:
                col_ctx = _build_status_col_ctx(gs, transform_url, tab_exp, df_exp.columns.tolist())
                to_sync = df_exp[df_exp["Remarks"].astype(str).str.contains("Ready to sync", case=False, na=False)]
                
                if to_sync.empty:
                    exp_status = "SYNCED"
                else:
                    all_exp_nos = to_sync["Exp Ref. No"].unique().tolist()
                    existing_docs = sync_engine.get_existing_duplicates("Purchase", all_exp_nos)

                    updater = AsyncSheetUpdater(gs, transform_url, tab_exp, col_ctx=col_ctx, batch_size=BATCH_SIZE)
                    section_fail = False
                    total_rows = len(to_sync)

                    # Use enumerate to track progress count
                    try:
                        for i, (idx, row_data) in enumerate(to_sync.iterrows()):
                            if _should_log_progress(i, total_rows):
                                logger.info(f"   [Expense {i+1}/{total_rows}] Processing Ref: {row_data.get('Exp Ref. No')}...")

                            ref_no = str(row_data.get("Exp Ref. No", ""))
                            
                            # --- Logic: Check Duplicates ---
                            if ref_no in existing_docs:
                                already_synced_msg = f"Skipper (Already synced in QBO at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')})"
                                updater.enqueue({"row_idx": idx, "status": already_synced_msg, "qbo_id": "", "qbo_link": ""})
                            
                            # --- Logic: Push to QBO ---
                            else:
                                try:
                                    resp = sync_engine.push_expense(ref_no, row_data)
                                    _throttle_qbo_call()
                                    new_id = resp.get("Purchase", {}).get("Id", "")
                                    qbo_link = sync_engine.build_qbo_url("Purchase", new_id) if new_id else ""
                                    msg = f"Synced at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"

                                    updater.enqueue({
                                        "row_idx": idx,
                                        "status": msg,
                                        "qbo_id": new_id,
                                        "qbo_link": qbo_link
                                    })
                                except Exception as e:
                                    error_msg = f"ERROR: {str(e)}"
                                    logger.error(f"      -> Failed: {error_msg}")
                                    updater.enqueue({"row_idx": idx, "status": error_msg, "qbo_id": "", "qbo_link": ""})
                                    has_error = True
                                    section_fail = True
                                    _throttle_qbo_call()
                    finally:
                        updater.close()

                    exp_status = "SYNC FAIL" if section_fail else "SYNCED"
        except Exception as e:
            logger.error(f"   ❌ Expense Sync Fail: {e}")
            has_error = True
            exp_status = "SYNC FAIL"

        # ---------------------------------------------------------
        # 3. SYNC TRANSFERS (UPDATED WITH BATCHING)
        # ---------------------------------------------------------
        try:
            logger.info(f"   Using Tab: {tab_tr}")
            try: df_tr = gs.read_as_df_sync(transform_url, tab_tr)
            except: df_tr = pd.DataFrame()

            if not df_tr.empty and "Remarks" in df_tr.columns:
                col_ctx = _build_status_col_ctx(gs, transform_url, tab_tr, df_tr.columns.tolist())
                to_sync = df_tr[df_tr["Remarks"].astype(str).str.contains("Ready to sync", case=False, na=False)]
                
                if to_sync.empty:
                    tr_status = "SYNCED"
                else:
                    all_tr_nos = to_sync["Ref No"].unique().tolist()
                    existing_docs = sync_engine.get_existing_duplicates(
                        "Transfer", all_tr_nos, date_start=month_start, date_end=month_end
                    )

                    updater = AsyncSheetUpdater(gs, transform_url, tab_tr, col_ctx=col_ctx, batch_size=BATCH_SIZE)
                    section_fail = False
                    total_rows = len(to_sync)

                    try:
                        for i, (idx, row_data) in enumerate(to_sync.iterrows()):
                            if _should_log_progress(i, total_rows):
                                logger.info(f"   [Transfer {i+1}/{total_rows}] Processing Ref: {row_data.get('Ref No')}...")

                            ref_no = str(row_data.get("Ref No", ""))

                            if ref_no in existing_docs:
                                already_synced_msg = f"Skipper (Already synced in QBO at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')})"
                                updater.enqueue({"row_idx": idx, "status": already_synced_msg, "qbo_id": "", "qbo_link": ""})
                            else:
                                try:
                                    resp = sync_engine.push_transfer(row_data)
                                    _throttle_qbo_call()
                                    new_id = resp.get("Transfer", {}).get("Id", "")
                                    qbo_link = sync_engine.build_qbo_url("Transfer", new_id) if new_id else ""
                                    msg = f"Synced at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"

                                    updater.enqueue({
                                        "row_idx": idx,
                                        "status": msg,
                                        "qbo_id": new_id,
                                        "qbo_link": qbo_link
                                    })
                                except Exception as e:
                                    error_msg = f"ERROR: {str(e)}"
                                    logger.error(f"      -> Failed: {error_msg}")
                                    updater.enqueue({"row_idx": idx, "status": error_msg, "qbo_id": "", "qbo_link": ""})
                                    has_error = True
                                    section_fail = True
                                    _throttle_qbo_call()
                    finally:
                        updater.close()

                    tr_status = "SYNC FAIL" if section_fail else "SYNCED"
        except Exception as e:
            logger.error(f"   ❌ Transfer Sync Fail: {e}")
            has_error = True
            tr_status = "SYNC FAIL"

        final_status = "PARTIAL ERROR" if has_error else "DONE"
        
        update_payload = {
            settings.CTRL_COL_QBO_SYNC: final_status,
            "Last Sync At": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        }
        
        if jv_status != "Skipped": update_payload[COL_QBO_JV] = jv_status
        if exp_status != "Skipped": update_payload[COL_QBO_EXP] = exp_status
        if tr_status != "Skipped": update_payload[COL_QBO_TR] = tr_status

        _batch_update_control(gs, control_sheet_id, settings.CONTROL_TAB_NAME, row_num, ctrl_df.columns, update_payload)
        logger.info(f"✅ [{client_name}] Sync Complete: {final_status}")

def _is_target_client(row: pd.Series, target_client: str | None) -> bool:
    if not target_client:
        return True

    target = str(target_client).strip()
    if not target:
        return True
    target_norm = settings.normalize_workspace_name(target)
    if target_norm in {"all", "*", "all clients"}:
        return True

    row_client = str(row.get(settings.MST_COL_CLIENT, "")).strip()
    row_realm = str(row.get(settings.MST_COL_REALM_ID, "")).strip()
    row_sheet_id = str(row.get(settings.MST_COL_SHEET_ID, "")).strip()
    row_folder_id = str(row.get(settings.MST_COL_OUTPUT, "")).strip()

    # Allow targeting by realm ID or by normalized client name.
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
    dispatch_ctx = single_instance_lock("run_syncing_all_dispatch") if target_is_all else nullcontext(True)
    with dispatch_ctx as acquired:
        if target_is_all and not acquired:
            logger.warning("Another ALL syncing dispatch is already in progress. Skipping this run.")
            return

        gs = GSheetsClient()
        qbo_client = QBOClient(gs_client=gs)
        try:
            master_df = gs.read_as_df(settings.MASTER_SHEET_ID, settings.MASTER_TAB_NAME)
        except Exception as e:
            logger.error(f"❌ Critical: {e}")
            return

        # Normalize headers to avoid silent misses from extra spaces/newlines in sheet columns.
        master_df.columns = [" ".join(str(c).replace("\n", " ").split()) for c in master_df.columns]

        matched_clients = 0
        for _, row in master_df.iterrows():
            if str(row.get(settings.MST_COL_STATUS, "")).strip().lower() != "active": continue
            if not _is_target_client(row, target_client):
                continue
            matched_clients += 1
            client_name = row.get(settings.MST_COL_CLIENT)
            if not settings.is_allowed_workspace(client_name):
                logger.warning(
                    f"⚠️ Skipping {client_name}: workspace not allowed for QBO API. "
                    f"Allowed: {', '.join(settings.ALLOWED_QBO_WORKSPACES)}"
                )
                continue
            sheet_id = row.get(settings.MST_COL_SHEET_ID)
            realm_id = str(row.get(settings.MST_COL_REALM_ID)).strip()
            if not sheet_id or not realm_id: continue

            logger.info(f"🏢 STARTING SYNC FOR: {client_name} ({realm_id})")
            client_lock_name = f"run_syncing_client_{realm_id}"
            with single_instance_lock(client_lock_name) as client_acquired:
                if not client_acquired:
                    logger.warning(
                        f"⏭️ Skipping {client_name}: another syncing run is already processing Realm {realm_id}."
                    )
                    continue
                try:
                    process_client_sync(gs, qbo_client, sheet_id, client_name, realm_id)
                except Exception as e:
                    logger.error(f"❌ Sync failed for {client_name}: {e}")

        if target_client and matched_clients == 0:
            logger.warning(f"No client matched target '{target_client}'.")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run QBO syncing pipeline.")
    parser.add_argument(
        "--client",
        dest="client",
        default="",
        help="Target client name, Realm ID, Spreadsheet ID, or Output Folder ID.",
    )
    args = parser.parse_args()
    main(target_client=args.client)
