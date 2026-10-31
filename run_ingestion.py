from __future__ import annotations
import argparse
from contextlib import nullcontext

# --- FIX: USE WINDOWS SYSTEM CERTIFICATES ---
try:
    import pip_system_certs.wrappers
    pip_system_certs.wrappers.wrap_requests()
except ImportError:
    pass
# --------------------------------------------

from dotenv import load_dotenv
load_dotenv("config/secrets.env")

import calendar
import re
from datetime import datetime
from typing import Tuple, List, Dict, Set
import pandas as pd
from config import settings
from src.connectors.gsheets_client import GSheetsClient
from src.connectors.qbo_client import QBOClient
from src.logic.syncing import QBOSync
from src.logic.transformer import transform_raw, _build_id_prefixes
from src.utils.logger import setup_logger
from src.logic.raw_adapter import standardize_raw_df, read_s5_raw_df, RAW_STANDARD_COLUMNS
from src.logic.no_column_check import check_raw_no_column
from src.logic.month_end import (
    MonthEndInputError,
    STATUS_HEADER,
    changed_rows,
    is_month_end_row,
    plan_month_end,
    read_source_tab,
    transform_note,
)
from src.utils.run_lock import single_instance_lock
from gspread.utils import rowcol_to_a1

logger = setup_logger("ingestion")

# KZDW COY filter: KZDW posts only these COY values; every other row is dropped before
# selection. COY=TD posts to its own QBO company (TINDERPAY) since 2026-09-25, so the
# two companies never both post a TD row. Applies to KZDW only, never to TINDERPAY.
KZDW_ALLOWED_COY_VALUES: set[str] = {"DPP"}

# KZP added a blank title row in the August 2026 raw layout, moving the actual
# field names from row 4 to row 5. Keep row 4 as a fallback for older KZP tabs.
KZP_SOURCE_HEADER_ROWS = (5, 4)

def parse_mixed_date(series: pd.Series) -> pd.Series:
    """Parse Excel serial dates and regular date strings safely."""
    numeric = pd.to_numeric(series, errors="coerce")
    excel_mask = numeric.between(-60000, 120000)

    parsed = pd.Series(pd.NaT, index=series.index, dtype="datetime64[ns]")
    if excel_mask.any():
        parsed.loc[excel_mask] = pd.to_datetime(
            numeric.loc[excel_mask],
            origin="1899-12-30",
            unit="D",
            errors="coerce",
        )
    if (~excel_mask).any():
        parsed.loc[~excel_mask] = pd.to_datetime(series.loc[~excel_mask], errors="coerce")
    return parsed

# ==========================================
# 1. HELPER FUNCTIONS
# ==========================================

def get_month_date_range(month_str: str, last_month_date_val=None) -> Tuple[datetime, datetime]:
    """Builds [start, end] date range for a month.
    Start is always first day of month; end uses 'Last Month Date' when provided.
    """
    try:
        dt = pd.to_datetime(month_str)
        start_date = dt.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        _, last_day = calendar.monthrange(start_date.year, start_date.month)
        month_end = start_date.replace(day=last_day, hour=23, minute=59, second=59, microsecond=999999)

        if pd.isna(last_month_date_val) or str(last_month_date_val).strip() == "":
            return start_date, month_end

        numeric = pd.to_numeric(pd.Series([last_month_date_val]), errors="coerce").iloc[0]
        if pd.notna(numeric) and -60000 <= numeric <= 120000:
            custom_end = pd.to_datetime(numeric, origin="1899-12-30", unit="D", errors="coerce")
        else:
            custom_end = pd.to_datetime(last_month_date_val, errors="coerce")

        if pd.isna(custom_end):
            return start_date, month_end

        end_date = custom_end.replace(hour=23, minute=59, second=59, microsecond=999999)
        if end_date < start_date:
            return start_date, end_date
        return start_date, min(end_date, month_end)
    except Exception:
        return None, None

def _now_iso_local() -> str:
    """Returns current timestamp string."""
    now = datetime.now().astimezone()
    return now.strftime(f"%Y-%m-%d %H:%M:%S")

def _batch_update_control(gs, sheet_id, tab_name, row_num, columns, updates_dict):
    """Updates specific columns for a row in the Control Sheet."""
    headers = list(columns)
    batch_data = []
    for col_name, val in updates_dict.items():
        if col_name in headers:
            col_idx = headers.index(col_name) + 1
            batch_data.append({'row': row_num, 'col': col_idx, 'val': str(val)})
    if batch_data:
        gs.batch_update_cells(sheet_id, tab_name, batch_data)

def format_month_name(date_str: str) -> str:
    if not date_str: return ""
    try:
        return pd.to_datetime(date_str).strftime("%b %y")
    except:
        return date_str


def _normalized_headers(df: pd.DataFrame) -> set[str]:
    return {
        re.sub(r"\s+", " ", str(column).replace("\n", " ").strip()).lower()
        for column in df.columns
    }


def _has_kzp_source_headers(df: pd.DataFrame) -> bool:
    """Confirm that a candidate row contains the modern KZP field names."""
    headers = _normalized_headers(df)
    return {
        "date",
        "from account",
        "usd - qbo",
        "qbo import",
    }.issubset(headers)


def _read_source_raw_df(gs, source_url: str, raw_tab_name: str, client_name: str) -> pd.DataFrame:
    """Read a client's raw tab, allowing both current and legacy KZP headers."""
    client_name_lower = client_name.lower()
    if "kzp" in client_name_lower:
        for header_row in KZP_SOURCE_HEADER_ROWS:
            raw_df = gs.read_as_df(
                source_url,
                raw_tab_name,
                header_row=header_row,
                value_render_option="UNFORMATTED_VALUE",
            )
            if _has_kzp_source_headers(raw_df):
                if header_row != KZP_SOURCE_HEADER_ROWS[0]:
                    logger.info(
                        f"   [{client_name}] Using legacy KZP source header row {header_row}."
                    )
                raw_df.attrs["header_row"] = header_row
                return raw_df

        raise ValueError(
            "KZP raw header not found on row 5 or legacy row 4. "
            "Expected Date, From Account, USD - QBO, and QBO Import columns."
        )

    if "s5" in client_name_lower:
        # S5's header row moves with the wallet-balance block above the grid, so it is
        # located by content rather than fixed at row 19. See read_s5_raw_df.
        return read_s5_raw_df(gs, source_url, raw_tab_name, client_name)

    if settings.is_kzdw_family(client_name):
        source_header_row = 5
    elif "umber" in client_name_lower:
        source_header_row = 4
    else:
        source_header_row = 1

    raw_df = gs.read_as_df(
        source_url,
        raw_tab_name,
        header_row=source_header_row,
        value_render_option="UNFORMATTED_VALUE",
    )
    # Lets the No-column check map a frame row back to its sheet row.
    raw_df.attrs["header_row"] = source_header_row
    return raw_df

def _parse_no_set(raw_val) -> set[int]:
    if pd.isna(raw_val) or str(raw_val).strip() == "":
        return set()
    out: set[int] = set()
    for tok in re.split(r"[,\s;|]+", str(raw_val).strip()):
        if not tok:
            continue
        try:
            n = int(float(tok))
            if n > 0:
                out.add(n)
        except Exception:
            continue
    return out

def _serialize_no_set(vals: set[int]) -> str:
    if not vals:
        return ""
    return ";".join(str(x) for x in sorted(vals))

def _control_cell_ref(columns, col_name: str, row_num: int) -> str | None:
    """A1 reference of `col_name` on `row_num` of the control tab (e.g. 'F54')."""
    headers = list(columns)
    if col_name not in headers:
        return None
    return rowcol_to_a1(row_num, headers.index(col_name) + 1)


# The 'No' the transformer writes is column A of every Transform tab (Journals, Expenses and
# Transfers all start with it), which is what the formula below reads.
TRANSFORM_NO_COLUMN_RANGE = "A2:A"


def _kzo_last_processed_formula(transform_url_ref: str, tab_prefix: str) -> str:
    """Live 'Last Processed Row' for KZO: max raw No across the month's three Transform tabs.

    Stored as a formula rather than a snapshot so the checkpoint tracks the Transform file
    itself. A run that appends rows and then dies before step 15 no longer leaves the cell
    behind reality, because the cell was never the record in the first place.

    Each IMPORTRANGE is wrapped in IFERROR so a month whose tabs do not all exist yet (or an
    IMPORTRANGE link still awaiting its one-time approval) reads 0 instead of poisoning the
    whole formula with #REF!. A 0 is safe: `_heal_last_processed` then recovers the real
    checkpoint from the tabs directly, and step 11b refuses to append anything already there.
    """
    ranges = ", ".join(
        f'IFERROR(IMPORTRANGE({transform_url_ref}, "{tab_prefix} - {tab}!{TRANSFORM_NO_COLUMN_RANGE}"), 0)'
        for tab in ("Journals", "Expenses", "Transfers")
    )
    return f"=IFERROR(MAX(UNIQUE(VSTACK({ranges}))), 0)"


def _kzo_checkpoint_cell(
    is_kzo_client: bool,
    columns,
    row_num: int,
    tab_prefix: str,
    fallback,
):
    """What to write into 'Last Processed Row': KZO's live formula, else the snapshot.

    Returns `fallback` for non-KZO clients, or when the control tab has no Transform File
    column to point IMPORTRANGE at.
    """
    if not is_kzo_client:
        return fallback
    transform_cell = _control_cell_ref(columns, settings.CTRL_COL_TRANSFORM_URL, row_num)
    if not transform_cell:
        return fallback
    return _kzo_last_processed_formula(transform_cell, tab_prefix)


def _decode_kzo_no(no_val: int, expected_month: int | None) -> str | None:
    """Best-effort reverse of the KZO 'No.' formula's date prefix.

    The sheet formula builds No. as TEXT(date, "MDD") + zero-padded running count of
    same-date rows, e.g. No=7310132 -> date_str "731" (Jul 31) + run_cnt "0132" (132nd
    same-date row). The month prefix is 1 digit (Jan-Sep) or 2 digits (Oct-Dec), which is
    ambiguous for a handful of values, so this is a diagnostic hint, not authoritative.
    """
    no_str = str(int(no_val))
    if len(no_str) != 7:
        return None

    candidates: list[tuple[int, int, int]] = []  # (month, day, run_cnt)

    month3, day3 = int(no_str[0]), int(no_str[1:3])
    if 1 <= month3 <= 12 and 1 <= day3 <= 31:
        candidates.append((month3, day3, int(no_str[3:])))

    month4, day4 = int(no_str[0:2]), int(no_str[2:4])
    if 10 <= month4 <= 12 and 1 <= day4 <= 31:
        candidates.append((month4, day4, int(no_str[4:])))

    if not candidates:
        return None

    if len(candidates) > 1 and expected_month is not None:
        preferred = [c for c in candidates if c[0] == expected_month]
        if preferred:
            candidates = preferred

    month, day, run_cnt = candidates[0]
    return f"{month}/{day} seq #{run_cnt}"

def _cap_pending_nos(vals: set[int], max_processed_no: int) -> set[int]:
    if max_processed_no <= 0:
        return set()
    return {x for x in vals if 0 < x <= max_processed_no}

def _filter_kzdw_coy(raw_df: pd.DataFrame, client_name: str) -> tuple[pd.DataFrame, dict[str, int]]:
    """
    KZDW only: keep rows whose COY is in KZDW_ALLOWED_COY_VALUES. Returns the kept rows
    and a {COY: count} of what was dropped. Dropped rows never reach pending or retry,
    so a later renumbering of the positional raw 'No' cannot turn them into a stale
    pending No that re-selects a different row.
    """
    if "kzdw" not in str(client_name).lower():
        return raw_df, {}

    coy = raw_df["COY"] if "COY" in raw_df.columns else pd.Series("", index=raw_df.index)
    normalized_coy = coy.fillna("").astype(str).str.strip().str.upper()
    keep = normalized_coy.isin(KZDW_ALLOWED_COY_VALUES)
    dropped = normalized_coy[~keep].replace("", "(blank)").value_counts().to_dict()
    return raw_df[keep].copy(), {str(k): int(v) for k, v in dropped.items()}

def _safe_int(val) -> int:
    """Coerce sheet values like 1234, 1234.0, '1,234' into int safely."""
    try:
        if pd.isna(val):
            return 0
    except Exception:
        pass

    try:
        s = str(val).strip()
        if not s:
            return 0
        s = s.replace(",", "")
        return int(float(s))
    except Exception:
        return 0

def _get_successfully_processed_nos(gs: GSheetsClient, spreadsheet_url: str, tabs: list[str]) -> set[int]:
    """
    Returns set of raw 'No' values that already exist in any output tab
    with a non-error Remarks (used to avoid reprocessing fully-completed rows).
    """
    processed: set[int] = set()
    for tab in tabs:
        try:
            df_out = gs.read_as_df_sync(spreadsheet_url, tab)
        except Exception:
            df_out = pd.DataFrame()

        if df_out.empty or "No" not in df_out.columns:
            continue

        df_tmp = df_out.copy()
        if "Remarks" in df_tmp.columns:
            err_mask = df_tmp["Remarks"].astype(str).str.contains("ERROR|Unbalance", case=False, na=False)
            df_tmp = df_tmp[~err_mask]

        if df_tmp.empty:
            continue

        nos = pd.to_numeric(df_tmp["No"], errors="coerce").dropna().astype(int).tolist()
        processed.update(nos)

    return processed

def get_transform_tab_state(
    gs: GSheetsClient,
    spreadsheet_url: str,
    tab_name: str,
    id_col_name: str,
    include_doc_id_match: bool = True,
    amount_col: str | None = None,
) -> Tuple[List[int], Dict[int, str], Set[int], Dict[int, float]]:
    """Read one Transform tab once and report what it already holds.

    Returns (rows_to_delete, preserved_ids, present_nos, amounts_by_no):
      * rows_to_delete / preserved_ids -- the ERROR-retry context: rows flagged
        ERROR/Unbalanced, to be deleted and rebuilt under their existing document id.
      * present_nos -- every raw 'No' already written to the tab, whatever its status.
      * amounts_by_no -- largest absolute `amount_col` value per No (empty without
        `amount_col`). KZO compares these with raw to tell a renumbered date (step 10d).

    present_nos exists because rows are appended to the Transform file (step 13) *before*
    'Last Processed Row' is written back to the control sheet (step 15). A run that dies in
    that window leaves the work done but the checkpoint stale, and every one of those rows
    then looks new again on the next run and is transformed a second time under a fresh Ref
    No. The Transform file is the durable record of what was actually produced, so callers
    trust it over the checkpoint cell.
    """
    try:
        # Use read_as_df to keep row positions aligned with sheet rows.
        df = gs.read_as_df(spreadsheet_url, tab_name)
        if df.empty or "No" not in df.columns:
            return [], {}, set(), {}

        work_df = df.copy()
        work_df["_sheet_row"] = work_df.index + 2  # +2 for header + 0-indexed DataFrame
        work_df["_no"] = pd.to_numeric(work_df.get("No"), errors="coerce")
        present_nos = {
            int(x) for x in work_df["_no"].dropna().astype(int).tolist() if int(x) > 0
        }
        amounts_by_no = (
            _abs_amounts_by_no(df["No"], df[amount_col])
            if amount_col and amount_col in df.columns
            else {}
        )

        if "Remarks" not in df.columns or id_col_name not in df.columns:
            return [], {}, present_nos, amounts_by_no

        work_df["_remarks"] = work_df["Remarks"].astype(str)
        work_df["_doc_id"] = work_df[id_col_name].astype(str).str.strip()

        # Any row flagged as error/unbalanced should trigger full cleanup for its document/no.
        error_mask = work_df["_remarks"].str.contains("ERROR|Unbalance", case=False, na=False)
        bad_rows = work_df[error_mask]
        if bad_rows.empty:
            return [], {}, present_nos, amounts_by_no

        bad_ids = set()
        if include_doc_id_match:
            bad_ids = set(bad_rows["_doc_id"].dropna().tolist())
            bad_ids.discard("")
        bad_nos = set(
            bad_rows["_no"]
            .dropna()
            .astype(int)
            .tolist()
        )

        target_mask = pd.Series(False, index=work_df.index)
        if include_doc_id_match and bad_ids:
            target_mask = target_mask | work_df["_doc_id"].isin(bad_ids)
        if bad_nos:
            target_mask = target_mask | work_df["_no"].fillna(-1).astype(int).isin(bad_nos)

        target_df = work_df[target_mask].copy()
        if target_df.empty:
            return [], {}, present_nos, amounts_by_no

        rows_to_delete = sorted(target_df["_sheet_row"].astype(int).unique().tolist(), reverse=True)
        existing_id_map = {}

        for _, row in target_df.iterrows():
            try:
                s_no = int(float(str(row.get("No", ""))))
                doc_id = str(row.get(id_col_name, "")).strip()
                if s_no > 0 and doc_id:
                    existing_id_map[s_no] = doc_id
            except Exception:
                pass

        return rows_to_delete, existing_id_map, present_nos, amounts_by_no
    except Exception as e:
        logger.exception(f"get_transform_tab_state crashed on tab '{tab_name}': {e}")
        raise


def _heal_last_processed(last_processed: int, transformed_nos: set[int]) -> int:
    """Raise the checkpoint to cover rows the Transform file already holds.

    The control-sheet cell can lag behind reality (a run that appended rows then failed
    before step 15, or a hand-edit that rewound it). Rows in the Transform file are finished
    work, so the checkpoint may only ever move forward to include them.
    """
    if not transformed_nos:
        return last_processed
    return max(int(last_processed), max(transformed_nos))


def _already_transformed_nos(
    processing_nos: set[int],
    transformed_nos: set[int],
    retry_nos: set[int],
) -> set[int]:
    """Nos selected for processing that the Transform file already holds.

    Retry Nos are excluded: their rows are deleted before the append, so rebuilding them is
    the intended behavior. Everything else would be a second copy under a new Ref No.
    """
    return {n for n in processing_nos if n in transformed_nos and n not in retry_nos}


def _abs_amounts_by_no(nos: pd.Series, amounts: pd.Series) -> dict[int, float]:
    """Largest absolute amount per raw No, the reconciler's raw-vs-transform rule.

    Journal lines come in +/- pairs per raw row, so the largest line is the row's amount.
    Blank or zero Nos and amounts that do not parse (e.g. '75,669.22' is fine, 'n/a' is not)
    are left out.
    """
    work = pd.DataFrame({
        "no": pd.to_numeric(nos.reset_index(drop=True), errors="coerce"),
        "amt": pd.to_numeric(
            amounts.reset_index(drop=True).astype(str).str.replace(",", "", regex=False),
            errors="coerce",
        ).abs(),
    }).dropna()
    work = work[work["no"] > 0]
    return {int(no): float(amt) for no, amt in work.groupby("no")["amt"].max().items()}


def _kzo_no_day(no_val: int) -> int:
    """The date part of a KZO raw No: TEXT(date, "MDD") ahead of the 4-digit same-date count."""
    return int(no_val) // 10000


def _kzo_missed_nos(
    ready_nos: set[int],
    last_processed: int,
    transformed_nos: set[int],
    transform_amounts: dict[int, float],
    raw_amounts: dict[int, float],
) -> tuple[set[int], dict[int, tuple[int, set[int]]]]:
    """KZO: ready rows the checkpoint already passed that the Transform file never received.

    KZO's No starts with the row's date, so the checkpoint (the highest No transformed) means
    "the latest date reached". A row for an earlier date that becomes ready after a later
    date was transformed is below it for good. The Transform file is the record of what was
    produced, so such a row is picked up -- unless its date looks renumbered: the No formula
    renumbers a date when a row is inserted or deleted there, and then a missing No can be a
    row that is already in the Transform file under its old No.

    A date counts as renumbered when one of its transformed Nos no longer shows the same
    amount in raw (or is gone from raw), with the reconciler's 0.05 tolerance. The caller
    leaves ERROR-retry Nos out of `transform_amounts`, since those rows are being rebuilt.

    Returns (missed_nos, held): held maps a date (see `_kzo_no_day`) to the first
    mismatching transformed No and the missed Nos kept back on that date.
    """
    behind = {n for n in ready_nos if 0 < n <= last_processed and n not in transformed_nos}
    held: dict[int, tuple[int, set[int]]] = {}
    for day in sorted({_kzo_no_day(n) for n in behind}):
        for t_no in sorted(n for n in transform_amounts if _kzo_no_day(n) == day):
            raw_amt = raw_amounts.get(t_no)
            if raw_amt is None or abs(raw_amt - transform_amounts[t_no]) > 0.05:
                held[day] = (t_no, {n for n in behind if _kzo_no_day(n) == day})
                break
    missed = {n for n in behind if _kzo_no_day(n) not in held}
    return missed, held


def _kzo_held_note(held: dict[int, tuple[int, set[int]]]) -> str:
    """'Pending Nos Note' text for the missed rows `_kzo_missed_nos` kept back."""
    return "; ".join(
        f"held {day // 100}/{day % 100} rows {_serialize_no_set(nos)}: No {bad_no} no longer "
        f"matches its Transform row, so that date looks renumbered. Check the raw tab, then "
        f"put the Nos to add in Pending Amount Nos"
        for day, (bad_no, nos) in sorted(held.items())
    )


def _get_sheet_rows_for_nos(
    gs: GSheetsClient,
    spreadsheet_url: str,
    tab_name: str,
    target_nos: set[int],
) -> list[int]:
    """Return sheet row numbers whose 'No' is in target_nos (for safe retry cleanup)."""
    if not target_nos:
        return []
    try:
        df = gs.read_as_df(spreadsheet_url, tab_name)
    except Exception:
        return []
    if df.empty or "No" not in df.columns:
        return []

    work_df = df.copy()
    work_df["_sheet_row"] = work_df.index + 2
    work_df["_no"] = pd.to_numeric(work_df["No"], errors="coerce").fillna(-1).astype(int)
    rows = work_df[work_df["_no"].isin(target_nos)]["_sheet_row"].astype(int).unique().tolist()
    return sorted(rows, reverse=True)

def _reserve_journal_numbers(
    gs, control_sheet_id: str, ctrl_df: pd.DataFrame, row_num: int, last_journal_no: int, global_last_jv: int
) -> int:
    """Save this row's Last Journal No before anything is written to the Transform file.

    Journal numbers live nowhere else until sync puts them in QBO. If a run stopped after the
    Transform write but before the final control update, the next run would mint the same
    numbers again and sync would skip the second journal as "already synced". Writing the
    counter first turns that into a harmless gap. Returns the run-wide counter for the next row.
    """
    _batch_update_control(gs, control_sheet_id, settings.CONTROL_TAB_NAME, row_num, ctrl_df.columns, {
        "Last Journal No": last_journal_no,
    })
    return max(global_last_jv, last_journal_no)


def _month_end_tab_state(values: list[list]) -> tuple[set[str], list[int]]:
    """(journal numbers in a month-end Transform tab, sheet rows of journals flagged ERROR).

    A journal with any ERROR line (a sync failure, or a user asking for a rebuild) is dropped
    whole; its source lines still say 'Transformed <JV>', so the plan rebuilds it under the
    same number.
    """
    if not values:
        return set(), []
    header = [str(h).strip() for h in values[0]]
    if "Journal No" not in header:
        return set(), []
    jv_idx = header.index("Journal No")
    rem_idx = header.index("Remarks") if "Remarks" in header else None
    jv_by_row: dict[int, str] = {}
    error_jvs: set[str] = set()
    for offset, row in enumerate(values[1:]):
        jv = str(row[jv_idx]).strip() if jv_idx < len(row) else ""
        if not jv:
            continue
        jv_by_row[offset + 2] = jv
        remarks = str(row[rem_idx]) if rem_idx is not None and rem_idx < len(row) else ""
        if "ERROR" in remarks.upper():
            error_jvs.add(jv)
    error_rows = sorted(r for r, jv in jv_by_row.items() if jv in error_jvs)
    return set(jv_by_row.values()) - error_jvs, error_rows


def _process_month_end_row(
    gs,
    control_sheet_id: str,
    ctrl_df: pd.DataFrame,
    row: pd.Series,
    row_num: int,
    client_name: str,
    qbo_mappings: dict,
    transform_url: str,
    start_jv: int,
) -> int:
    """Transform one Month-end JV control row. Returns the highest journal number reserved.

    Order matters for duplicates (README "Month-end JV rows"): the journal numbers are saved
    to the control row before the source Status is written, and the Status before the
    Transform tab, so an interrupted run can leave a gap but never reuses a number or copies
    a line twice.
    """
    country = str(row.get(settings.CTRL_COL_COUNTRY, "")).strip()
    source_url = str(row.get(settings.CTRL_COL_SOURCE_URL, "")).strip()
    tab_name = str(row.get(settings.CTRL_COL_TAB_NAME, "")).strip()
    raw_month = str(row.get(settings.CTRL_COL_MONTH, "")).strip()
    month = format_month_name(raw_month)
    tab_jv = f"{country} {month} - {settings.OUTPUT_TAB_JOURNALS}"
    col_last_jv, col_qbo_jv = "Last Journal No", "QBO Journal"

    def update_control(values: dict):
        if settings.CTRL_COL_TRANSFORM_NOTE in values and settings.CTRL_COL_TRANSFORM_NOTE not in ctrl_df.columns:
            gs.update_cell(control_sheet_id, settings.CONTROL_TAB_NAME, 1, len(ctrl_df.columns) + 1, settings.CTRL_COL_TRANSFORM_NOTE)
            ctrl_df[settings.CTRL_COL_TRANSFORM_NOTE] = ""
        _batch_update_control(gs, control_sheet_id, settings.CONTROL_TAB_NAME, row_num, ctrl_df.columns, values)

    def stop(note: str, reserved: int) -> int:
        message = f"⛔ Month-end tab '{tab_name}': {note}"
        logger.warning(f"   ⛔ [{client_name}] Row {row_num}: {message}")
        try:
            update_control({
                settings.CTRL_COL_ACTIVE: "ERROR",
                settings.CTRL_COL_TRANSFORM_NOTE: message,
                settings.CTRL_COL_LAST_RUN_AT: _now_iso_local(),
            })
        except Exception as e:
            logger.error(f"   ❌ [{client_name}] Row {row_num}: could not write the stop note: {e}")
        return reserved

    # Two month-end rows for one month would each rebuild the other's journals.
    others = [
        i + 2 for i, r in ctrl_df.iterrows()
        if i + 2 != row_num
        and is_month_end_row(r.get(settings.CTRL_COL_COUNTRY))
        and format_month_name(str(r.get(settings.CTRL_COL_MONTH, "")).strip()) == month
    ]
    if others:
        return stop(f"another Month-end JV row exists for {month} (row {others[0]}). Keep only one.", start_jv)

    month_start, month_end = get_month_date_range(raw_month, row.get(settings.CTRL_COL_LAST_MONTH_DATE, ""))
    if month_start is None:
        return stop(f"Month '{raw_month}' is not a date.", start_jv)

    def read_source():
        return read_source_tab(gs.read_values(source_url, tab_name, value_render_option="UNFORMATTED_VALUE"))

    # Read everything that decides what gets written. Any failure here stops before one write.
    try:
        # Strict read: a failed read must not look like "no tab yet", or every journal not
        # synced yet would be copied again (and posted with doubled lines).
        jv_in_tab, error_rows = _month_end_tab_state(gs.read_tab_values(transform_url, tab_jv) or [])
        source = read_source()
        if not source.has_status_header and (jv_in_tab or error_rows):
            # Without Status every line looks new: the month would be renumbered and posted again.
            return stop(
                f"the Status column is missing, but '{tab_jv}' already has journals. "
                f"Put the Status column back with its values, then run Transform again.",
                start_jv,
            )
        if error_rows:
            logger.info(f"   [{client_name}] Month-end: removing {len(error_rows)} ERROR line(s) from '{tab_jv}' to rebuild them.")
            gs.delete_rows(transform_url, tab_jv, error_rows)
        plan = plan_month_end(
            source,
            client_name=client_name,
            month_start=month_start,
            month_end=month_end,
            start_jv=start_jv,
            jv_in_tab=jv_in_tab,
            qbo_mappings=qbo_mappings,
        )
        if plan.rows_to_verify:
            moved = changed_rows(source, read_source(), plan.rows_to_verify)
            if moved:
                shown = ", ".join(str(r) for r in moved[:10])
                return stop(f"the tab changed during Transform (rows {shown}). Run Transform again.", start_jv)
    except MonthEndInputError as e:
        return stop(f"{e}.", start_jv)
    except Exception as e:
        return stop(
            f"could not read the source tab or '{tab_jv}' ({e}). Nothing was written. Run Transform again.",
            start_jv,
        )

    reserved = max(start_jv, plan.last_journal_no)
    update_control({col_last_jv: reserved})

    # The numbers are taken from here on. Never raise past this point: the caller must get
    # `reserved` so the next row of this run starts after these numbers.
    try:
        try:
            if plan.status_updates and not source.has_status_header:
                gs.update_cell(source_url, tab_name, 1, source.status_col, STATUS_HEADER)
            gs.batch_update_cells(source_url, tab_name, [
                {"row": r, "col": source.status_col, "val": text}
                for r, text in sorted(plan.status_updates.items())
            ])
        except Exception as e:
            return stop(
                f"the bot could not write Status ({e}). Its Google account needs Editor access on this file. "
                f"Nothing was copied.",
                reserved,
            )

        if not plan.journals.empty:
            template = (
                settings.MONTH_END_TEMPLATE_TAB
                if settings.MONTH_END_TEMPLATE_TAB in gs.worksheet_titles(control_sheet_id)
                else settings.JOURNAL_TEMPLATE_TAB
            )
            gs.append_or_create_df(
                transform_url, tab_jv, plan.journals,
                template_tab_name=template, template_spreadsheet_id=control_sheet_id,
            )
            gs.cleanup_default_sheet(transform_url)

        note = transform_note(plan, tab_name, month)
        logger.info(f"   ✅ [{client_name}] Month-end row {row_num}: {note}")
        updates = {
            settings.CTRL_COL_ACTIVE: "DONE",
            settings.CTRL_COL_TRANSFORM_NOTE: note,
            settings.CTRL_COL_LAST_RUN_AT: _now_iso_local(),
        }
        if col_qbo_jv in ctrl_df.columns:
            if plan.error_line_count:
                updates[col_qbo_jv] = "ERROR"
            elif not plan.journals.empty or str(row.get(col_qbo_jv, "")).strip().upper() == "ERROR":
                updates[col_qbo_jv] = "READY TO SYNC"
        update_control(updates)
    except Exception as e:
        return stop(
            f"Transform stopped after giving out journal numbers ({e}). Run Transform again: "
            f"the journals are rebuilt under the same numbers.",
            reserved,
        )
    return reserved


# ==========================================
# 2. CORE LOGIC (PER CLIENT)
# ==========================================

def process_client_control_sheet(
    gs: GSheetsClient,
    qbo_client: QBOClient,
    control_sheet_id: str,
    client_name: str,
    realm_id: str,
):
    """
    Reads the specific Client's Control Sheet and processes all 'READY' jobs.
    """
    logger.info(f"📂 [{client_name}] Opening Control Sheet (ID: {control_sheet_id})...")

    # --- A. Read the Control Sheet ---
    try:
        ctrl_df = gs.read_as_df(control_sheet_id, settings.CONTROL_TAB_NAME)
    except Exception as e:
        logger.error(f"   ❌ [{client_name}] Failed to read Control Tab: {e}")
        return

    if ctrl_df.empty: 
        logger.warning(f"   ⚠️ [{client_name}] Control Sheet is empty.")
        return

    # Normalize headers to avoid silent misses from extra spaces/newlines.
    ctrl_df.columns = [" ".join(str(c).replace("\n", " ").split()) for c in ctrl_df.columns]

    # Avoid expensive QBO auth/mapping calls when this client has nothing to run.
    status_series = ctrl_df.get(settings.CTRL_COL_ACTIVE, pd.Series("", index=ctrl_df.index))
    ready_count = int(status_series.astype(str).str.strip().eq("READY").sum())
    if ready_count == 0:
        logger.info(f"   ⏭️ [{client_name}] No READY rows in control sheet. Skipping QBO auth/mappings.")
        return

    # --- B. Authenticate/Switch QBO context ---
    try:
        logger.info(f"🔐 [{client_name}] Authenticating with Realm ID: {realm_id}")
        qbo_client.set_company(realm_id)
        logger.info(f"✅ [{client_name}] Successfully authenticated. Ready to fetch QBO mappings.")
    except Exception as e:
        logger.error(f"❌ Critical Auth Failure for {client_name}: {e}")
        return

    # --- C. Fetch QBO Mappings (Specific to this Client/Realm) ---
    try:
        temp_sync = QBOSync(qbo_client)
        qbo_mappings = temp_sync.mappings
        num_accounts = len(qbo_mappings.get('accounts', {}))
        num_locations = len(qbo_mappings.get('locations', {}))
        logger.info(f"   ✅ [{client_name}] QBO Mappings fetched: {num_accounts} accounts, {num_locations} locations.")
        if num_accounts == 0:
            logger.warning(f"   ⚠️ [{client_name}] WARNING: No accounts found! Check Realm ID is correct.")
    except Exception as e:
        logger.error(f"   ❌ [{client_name}] Failed to fetch mappings. Check Realm ID/Token. Error: {e}")
        return

    # --- Constants for this Client ---
    COL_LAST_JV = "Last Journal No"
    COL_LAST_EXP = "Last Expense No"
    COL_LAST_TR = "Last Transfer No"
    COL_QBO_JV = "QBO Journal"
    COL_QBO_EXP = "QBO Expense"
    COL_QBO_TR = "QBO Transfer"
    COL_PENDING_AMOUNT_NOS = "Pending Amount Nos"

    # KZO's raw 'No' is derived from row position (see _decode_kzo_no), so a same-date
    # insertion/deletion elsewhere in the raw tab renumbers later rows. Ensure the
    # diagnostic note column exists so stale-pending-No hints (below) have somewhere to go.
    is_kzo_client = not (
        any(x in client_name.lower() for x in ("kzp", "s5", "umber"))
        or settings.is_kzdw_family(client_name)
    )
    if is_kzo_client and settings.CTRL_COL_PENDING_NOS_NOTE not in ctrl_df.columns:
        new_col_idx = len(ctrl_df.columns) + 1
        gs.update_cell(control_sheet_id, settings.CONTROL_TAB_NAME, 1, new_col_idx, settings.CTRL_COL_PENDING_NOS_NOTE)
        ctrl_df[settings.CTRL_COL_PENDING_NOS_NOTE] = ""

    # Get the max journal number currently recorded in the sheet
    global_last_jv = ctrl_df[COL_LAST_JV].apply(_safe_int).max()

    # --- D. Iterate Control Sheet Rows ---
    for i, row in ctrl_df.iterrows():
        # 1. Check Trigger
        status_val = str(row.get(settings.CTRL_COL_ACTIVE, "")).strip()
        if status_val != 'READY': continue

        row_num = i + 2
        logger.info(f"🚀 [{client_name}] Processing Row {row_num}...")
        _batch_update_control(gs, control_sheet_id, settings.CONTROL_TAB_NAME, row_num, ctrl_df.columns, {settings.CTRL_COL_ACTIVE: "PROCESSING"})

        try:
            # 2. Extract Job Details
            country = str(row.get(settings.CTRL_COL_COUNTRY, "")).strip()
            source_url = str(row.get(settings.CTRL_COL_SOURCE_URL, "")).strip()
            transform_url = str(row.get(settings.CTRL_COL_TRANSFORM_URL, "")).strip()
            raw_tab_name = str(row.get(settings.CTRL_COL_TAB_NAME, "")).strip()
            raw_month = str(row.get(settings.CTRL_COL_MONTH, "")).strip()
            last_month_date = row.get(settings.CTRL_COL_LAST_MONTH_DATE, "")
            month = format_month_name(raw_month)

            # 3. Create/Link Transform File
            created_new_transform = False
            if not transform_url or len(transform_url) < 10:
                new_title = f"{client_name} - {country} QBO - {month}"
                logger.info(f"   ⚠️ No Transform File. Creating: '{new_title}'...")
                try:
                    transform_url = gs.create_spreadsheet(new_title)
                    new_file_id = transform_url.split("/d/")[1].split("/")[0]
                    # Copy permissions from the Client's Control Sheet to the new Transform File
                    gs.copy_permissions(source_id=control_sheet_id, target_id=new_file_id)
                    
                    _batch_update_control(gs, control_sheet_id, settings.CONTROL_TAB_NAME, row_num, ctrl_df.columns, {settings.CTRL_COL_TRANSFORM_URL: transform_url})
                    created_new_transform = True
                except Exception as e:
                    logger.error(f"   ❌ Failed to create spreadsheet: {e}")
                    raise e
            
            # 4. Prepare ID Counters
            last_processed_raw = row.get(settings.CTRL_COL_LAST_PROCESSED_ROW, 0)
            last_processed = _safe_int(last_processed_raw)
            if last_processed == 0 and str(last_processed_raw).strip() not in ("", "0", "nan", "<NA>"):
                logger.warning(
                    f"   ⚠️ [{client_name}] '{settings.CTRL_COL_LAST_PROCESSED_ROW}' did not parse as "
                    f"a number (value: {str(last_processed_raw).strip()!r}); treating it as 0 and "
                    f"recovering the checkpoint from the Transform file. If that is #REF!, the "
                    f"IMPORTRANGE link needs its one-time approval in the control sheet."
                )
            
            # Fetch latest QBO Journal No to prevent overlap. Same prefix the transformer
            # mints, so the lookup can never drift from the numbers actually written.
            journal_prefix, _ = _build_id_prefixes(client_name)
            qbo_last_jv = qbo_client.get_max_journal_number(journal_prefix)
            final_start_jv = max(global_last_jv, qbo_last_jv)
            
            last_exp = _safe_int(row.get(COL_LAST_EXP, 0))
            last_tr = _safe_int(row.get(COL_LAST_TR, 0))
            pending_nos_from_control = _parse_no_set(row.get(COL_PENDING_AMOUNT_NOS, ""))
            previous_pending_nos = _cap_pending_nos(pending_nos_from_control, last_processed)

            # If this run created a brand-new transform file, treat it as a fresh row state.
            # This avoids accidental skipping when a duplicated control row carries old counters.
            if created_new_transform:
                if last_processed > 0 or previous_pending_nos:
                    logger.info(
                        f"   [{client_name}] New transform detected; resetting carried row state "
                        f"(Last Processed Row {last_processed} -> 0, Pending Amount Nos cleared)."
                    )
                last_processed = 0
                previous_pending_nos = set()

            tab_prefix = f"{country} {month}"
            tab_jv, tab_exp, tab_tr = f"{tab_prefix} - Journals", f"{tab_prefix} - Expenses", f"{tab_prefix} - Transfers"

            # Month-end JV rows read hand-entered journal lines, not a COY raw tab.
            if is_month_end_row(country):
                global_last_jv = max(global_last_jv, _process_month_end_row(
                    gs, control_sheet_id, ctrl_df, row, row_num, client_name,
                    qbo_mappings, transform_url, final_start_jv,
                ))
                continue

            # 5. Read each Transform tab once: ERROR-retry context + the raw Nos it already
            #    holds (the durable record of what was actually produced).
            preserved_ids = {'journals': {}, 'expenses': {}, 'transfers': {}}
            deletions: Dict[str, List[int]] = {}
            retry_nos: list[int] = []
            transformed_nos: set[int] = set()
            transform_amounts: dict[int, float] = {}  # for KZO's renumbered-date check (10d)

            # Retry by raw No only (avoid broad doc-id expansion).
            d_jv, ids_jv, nos_jv, amt_jv = get_transform_tab_state(
                gs, transform_url, tab_jv, "Journal No", include_doc_id_match=False, amount_col="Amount"
            )
            if d_jv:
                deletions[tab_jv] = d_jv
                preserved_ids['journals'] = ids_jv
            transformed_nos |= nos_jv

            d_exp, ids_exp, nos_exp, amt_exp = get_transform_tab_state(
                gs, transform_url, tab_exp, "Exp Ref. No", include_doc_id_match=False, amount_col="Expense Line Amount"
            )
            if d_exp:
                deletions[tab_exp] = d_exp
                preserved_ids['expenses'] = ids_exp
            transformed_nos |= nos_exp

            d_tr, ids_tr, nos_tr, amt_tr = get_transform_tab_state(
                gs, transform_url, tab_tr, "Ref No", include_doc_id_match=False, amount_col="Transfer Amount"
            )
            if d_tr:
                deletions[tab_tr] = d_tr
                preserved_ids['transfers'] = ids_tr
            transformed_nos |= nos_tr

            for tab_amounts in (amt_jv, amt_exp, amt_tr):
                for no_val, amt in tab_amounts.items():
                    transform_amounts[no_val] = max(amt, transform_amounts.get(no_val, 0.0))

            retry_nos = list(set([k for sub in preserved_ids.values() for k in sub.keys()]))

            # 5b. Self-heal the checkpoint. Rows are appended to the Transform file (step 13)
            #     before Last Processed Row is written back (step 15), so a run that broke in
            #     that window left the work done but the cell stale -- and every one of those
            #     rows would look new again here and be transformed a second time under a
            #     fresh Ref No. Same effect if the cell was rewound by hand. The Transform
            #     file is the record of truth, so the checkpoint only moves forward.
            healed_last_processed = _heal_last_processed(last_processed, transformed_nos)
            if healed_last_processed > last_processed:
                logger.warning(
                    f"   ⚠️ [{client_name}] '{settings.CTRL_COL_LAST_PROCESSED_ROW}' "
                    f"({last_processed}) is behind the Transform file (max No "
                    f"{healed_last_processed}). A previous run wrote rows without saving the "
                    f"checkpoint, or the cell was edited. Using {healed_last_processed} so "
                    f"those rows are not transformed twice."
                )
                last_processed = healed_last_processed
                previous_pending_nos = _cap_pending_nos(pending_nos_from_control, last_processed)

            # 6. Read & Clean Source Data
            raw_df = _read_source_raw_df(
                gs,
                source_url,
                raw_tab_name,
                client_name,
            )

            # 6a. A broken 'No' column (blocked ARRAYFORMULA/SEQUENCE, or #REF! rows) would
            #     make every affected row silently look unnumbered. Stop and say what to fix.
            no_column_problem = check_raw_no_column(gs, source_url, raw_tab_name, raw_df, client_name, country)
            if no_column_problem:
                logger.warning(f"   ⛔ [{client_name}] Row {row_num}: {no_column_problem}")
                if settings.CTRL_COL_TRANSFORM_NOTE not in ctrl_df.columns:
                    gs.update_cell(control_sheet_id, settings.CONTROL_TAB_NAME, 1, len(ctrl_df.columns) + 1, settings.CTRL_COL_TRANSFORM_NOTE)
                    ctrl_df[settings.CTRL_COL_TRANSFORM_NOTE] = ""
                _batch_update_control(gs, control_sheet_id, settings.CONTROL_TAB_NAME, row_num, ctrl_df.columns, {
                    settings.CTRL_COL_ACTIVE: "ERROR",
                    settings.CTRL_COL_TRANSFORM_NOTE: no_column_problem,
                    settings.CTRL_COL_LAST_RUN_AT: _now_iso_local(),
                })
                continue

            raw_df = standardize_raw_df(raw_df, client_name=client_name, raw_month=raw_month)

            # --- LOGGING START ---
            initial_count = len(raw_df)
            logger.info(f"   📊 [{client_name}] Step 6: Raw Rows Read: {initial_count}")
            # ---------------------

            if raw_df.empty:
                logger.info(f"   [{client_name}] Raw tab empty.")
                _batch_update_control(gs, control_sheet_id, settings.CONTROL_TAB_NAME, row_num, ctrl_df.columns, {settings.CTRL_COL_ACTIVE: "DONE (Empty)"})
                continue
            
            # Keep canonical raw schema from raw_adapter (supports KZDW Currency Rate).
            raw_df = raw_df.reindex(columns=RAW_STANDARD_COLUMNS, fill_value="")

            raw_df["CO"] = raw_df["CO"].astype(str).str.replace("GRP", "GROUP").str.strip()

            # 7. Date Filtering (Strict Month Match)
            target_start, target_end = get_month_date_range(raw_month, last_month_date)
            if target_start and target_end:
                # Robust Parse
                raw_df["_TempDate"] = parse_mixed_date(raw_df["Date"])
                
                # Filter
                month_mask = (raw_df["_TempDate"] >= target_start) & (raw_df["_TempDate"] <= target_end)
                raw_df = raw_df[month_mask].copy()
                raw_df.drop(columns=["_TempDate"], inplace=True)
                
                # --- LOGGING DATE FILTER ---
                after_date_count = len(raw_df)
                dropped_date = initial_count - after_date_count
                logger.info(
                    f"   🗓️ [{client_name}] Step 7: Date Filter "
                    f"({target_start.date()} -> {target_end.date()}) -> "
                    f"Kept: {after_date_count} | Dropped: {dropped_date}"
                )
                # ---------------------------

                if raw_df.empty:
                    logger.warning(f"   [{client_name}] ⚠️ No rows found for {month} in Source.")
                    _batch_update_control(gs, control_sheet_id, settings.CONTROL_TAB_NAME, row_num, ctrl_df.columns, {settings.CTRL_COL_ACTIVE: "DONE (No Data)"})
                    continue

            # 8. Numeric Cleanup (Do this first so we can check for 0 amounts)
            for col in ["No", "USD - QBO", "Amount Fr", "Amount To"]:
                if col in raw_df.columns:
                    raw_df[col] = pd.to_numeric(raw_df[col], errors="coerce").fillna(0)

            # 8b. KZO: raw amount per No for the renumbered-date check (10d), taken before the
            #     Exclude filter so a row excluded after it was transformed still compares.
            raw_amounts_by_no = (
                _abs_amounts_by_no(raw_df["No"], raw_df["USD - QBO"]) if is_kzo_client else {}
            )

            # 9. Exclude Rows
            before_exclude = len(raw_df)
            raw_df = raw_df[~raw_df["Check (Internal use)"].astype(str).str.contains("exclude", na=False, case=False)].copy()

            after_exclude = len(raw_df)
            dropped_exclude = before_exclude - after_exclude
            if dropped_exclude > 0:
                logger.info(f"   🚫 [{client_name}] Step 9: 'Exclude' Filter -> Kept: {after_exclude} | Dropped: {dropped_exclude}")

            # 9b. KZDW COY Filter: KZDW posts only KZDW_ALLOWED_COY_VALUES (DPP).
            raw_df, dropped_coy = _filter_kzdw_coy(raw_df, client_name)
            if dropped_coy:
                logger.info(
                    f"   🏷️ [{client_name}] Step 9b: COY Filter ({', '.join(sorted(KZDW_ALLOWED_COY_VALUES))} only) "
                    f"-> Kept: {len(raw_df)} | Dropped: {sum(dropped_coy.values())} {dropped_coy}"
                )

            # 10. Track Pending Rows & Select Rows to Process
            method_col = "QBO Method"
            amount_col = "USD - QBO" # We use USD - QBO as the standard amount column

            method_non_blank = raw_df[method_col].notna() & (raw_df[method_col].str.strip() != "")
            amt_numeric = raw_df[amount_col] # Already converted to numeric in Step 8

            # ---> A. Identify Pending Rows (Method exists, but amount is 0)
            pending_amount_mask = method_non_blank & (amt_numeric == 0)
            current_pending_nos = set(
                int(x) for x in raw_df.loc[pending_amount_mask, "No"].astype(int).tolist() if int(x) > 0
            )

            # ---> A2. KZO ONLY: flag pending Nos that vanished from raw entirely (row-shift
            # signal). Pending rows only ever had their integer No stored (no content
            # snapshot), so a missing No can't be auto-relinked here -- this is a log/note
            # diagnostic pointing the analyst at the likely date, not a reassignment.
            pending_nos_note = ""
            if is_kzo_client:
                raw_no_set = set(int(x) for x in raw_df["No"].tolist() if int(x) > 0)
                stale_pending_nos = previous_pending_nos - raw_no_set
                if stale_pending_nos:
                    expected_month = None
                    parsed_raw_month = pd.to_datetime(raw_month, errors="coerce")
                    if pd.notna(parsed_raw_month):
                        expected_month = int(parsed_raw_month.month)

                    note_parts = []
                    nearby_window = 3
                    for stale_no in sorted(stale_pending_nos):
                        hint_text = _decode_kzo_no(stale_no, expected_month) or "undecodable"
                        nearby_range = f"{stale_no - nearby_window}-{stale_no + nearby_window}"
                        logger.warning(
                            f"   ⚠️ [{client_name}] Pending No {stale_no} not found in current raw "
                            f"({tab_prefix}) — decoded as {hint_text}. A row may have been "
                            f"inserted/removed for that date in the raw tab; check Nos {nearby_range} "
                            f"for the shifted row."
                        )
                        note_parts.append(f"{stale_no} ({hint_text}, missing — check {nearby_range})")
                    pending_nos_note = "; ".join(note_parts)

            # ---> B. Identify Ready Rows (Method exists, and amount is NOT 0)
            ready_mask = method_non_blank & (amt_numeric != 0)
            ready_df = raw_df[ready_mask].copy()

            # 10a. Strictly new rows (No > last_processed)
            new_df = ready_df[ready_df["No"] > last_processed].copy()

            # 10b. Always retry rows listed in Pending Amount Nos.
            late_filled_df = raw_df[
                (raw_df["No"] <= last_processed) &
                (raw_df["No"].isin(previous_pending_nos)) &
                method_non_blank
            ].copy()

            # 10c. Always retry old ERROR rows from transform outputs.
            retry_df = raw_df[
                raw_df["No"].isin(retry_nos) &
                method_non_blank
            ].copy()

            # 10d. KZO ONLY: ready rows the checkpoint already passed but the Transform file
            #      never received. KZO's No starts with the date, so transforming a later date
            #      first leaves every earlier row that gets its method afterwards below the
            #      checkpoint (TH Sep 2026). Rows on a date that looks renumbered are held and
            #      named in the note instead, so a shifted No cannot post a row twice.
            missed_df = ready_df.iloc[0:0]
            held_count = 0
            if is_kzo_client:
                retry_no_set = set(retry_nos)
                missed_nos, held = _kzo_missed_nos(
                    ready_nos={int(x) for x in ready_df["No"].tolist()} - previous_pending_nos,
                    last_processed=last_processed,
                    transformed_nos=transformed_nos,
                    transform_amounts={
                        n: amt for n, amt in transform_amounts.items() if n not in retry_no_set
                    },
                    raw_amounts=raw_amounts_by_no,
                )
                missed_df = ready_df[ready_df["No"].isin(missed_nos)].copy()
                if held:
                    held_count = sum(len(nos) for _, nos in held.values())
                    held_note = _kzo_held_note(held)
                    logger.warning(f"   ⚠️ [{client_name}] Step 10d: {held_note}")
                    pending_nos_note = (
                        f"{pending_nos_note}; {held_note}" if pending_nos_note else held_note
                    )

            processing_df = (
                pd.concat([new_df, late_filled_df, retry_df, missed_df])
                  .drop_duplicates(subset=["No"])
            )

            # --- LOGGING SELECTION ---
            no_numeric = pd.to_numeric(raw_df["No"], errors="coerce").fillna(0).astype(int)
            no_method_count = int((~method_non_blank).sum())
            zero_amount_count = int((method_non_blank & (amt_numeric == 0)).sum())
            positive_amt_count = int(ready_mask.sum())
            eligible_old_done_count = int(
                ((no_numeric <= last_processed) &
                 (~no_numeric.isin(previous_pending_nos)) &
                 (~no_numeric.isin(retry_nos)) &
                 (method_non_blank & (amt_numeric != 0))).sum()
            ) - len(missed_df) - held_count  # below the checkpoint but never transformed
            logger.info(
                f"   🔢 [{client_name}] Step 10: Selection -> New: {len(new_df)}, "
                f"Late-filled: {len(late_filled_df)}, Retry: {len(retry_df)}, "
                f"Missed: {len(missed_df)} | Total: {len(processing_df)}"
            )
            logger.info(
                f"   🔍 [{client_name}] Step 10 Detail -> No Method: {no_method_count}, "
                f"Zero Amount(Pending): {zero_amount_count}, Ready Rows: {positive_amt_count}, "
                f"Eligible Old & done: {eligible_old_done_count}, Held (renumbered date): {held_count}, "
                f"Last Processed Row: {last_processed}"
            )
            # -------------------------

            if processing_df.empty:
                logger.info(f"   [{client_name}] No new rows to process.")
                pending_to_write = _cap_pending_nos(current_pending_nos, last_processed)
                idle_updates = {
                    settings.CTRL_COL_LAST_RUN_AT: _now_iso_local(),
                    COL_PENDING_AMOUNT_NOS: _serialize_no_set(pending_to_write), # <-- ADDED
                    settings.CTRL_COL_PENDING_NOS_NOTE: pending_nos_note,
                    settings.CTRL_COL_TRANSFORM_NOTE: "",  # clears an earlier No-column message
                    settings.CTRL_COL_ACTIVE: "DONE"
                }
                # Install/repair KZO's live checkpoint formula even on a run with nothing to
                # transform, so it does not wait for the next batch to appear.
                idle_checkpoint = _kzo_checkpoint_cell(
                    is_kzo_client, ctrl_df.columns, row_num, tab_prefix, None
                )
                if idle_checkpoint is not None:
                    idle_updates[settings.CTRL_COL_LAST_PROCESSED_ROW] = idle_checkpoint
                _batch_update_control(gs, control_sheet_id, settings.CONTROL_TAB_NAME, row_num, ctrl_df.columns, idle_updates)
                continue

            # Delete only rows that are actually being retried from ERROR state.
            retry_selected_nos = set(
                int(x)
                for x in pd.to_numeric(retry_df.get("No"), errors="coerce").dropna().astype(int).tolist()
                if int(x) > 0
            )
            deletions = {}
            if retry_selected_nos:
                for tab_name in (tab_jv, tab_exp, tab_tr):
                    rows = _get_sheet_rows_for_nos(gs, transform_url, tab_name, retry_selected_nos)
                    if rows:
                        deletions[tab_name] = rows
            
            # 11. Execute Deletions (Clean up bad rows before appending new ones)
            for tab, rows in deletions.items(): gs.delete_rows(transform_url, tab, rows)

            # 11b. Last line of defence: whatever the checkpoint said, a No still present in
            #      the Transform file after the deletions above is finished work. Appending it
            #      again would mint a second Ref No for the same source row and (once synced)
            #      double-post it to QBO.
            processing_nos = (
                pd.to_numeric(processing_df["No"], errors="coerce").fillna(0).astype(int)
            )
            duplicate_nos = _already_transformed_nos(
                set(processing_nos.tolist()), transformed_nos, retry_selected_nos
            )
            if duplicate_nos:
                dup_mask = processing_nos.isin(duplicate_nos)
                preview = sorted(duplicate_nos)
                logger.warning(
                    f"   ⚠️ [{client_name}] Skipping {int(dup_mask.sum())} row(s) already in the "
                    f"Transform file: {preview[:20]}{' ...' if len(preview) > 20 else ''}. "
                    f"To rebuild one on purpose, mark its Transform row ERROR so the retry path "
                    f"deletes it and reuses its Ref No."
                )
                skip_note = (
                    f"skipped {len(duplicate_nos)} already-transformed No(s): "
                    f"{_serialize_no_set(duplicate_nos)}"
                )
                pending_nos_note = (
                    f"{pending_nos_note}; {skip_note}" if pending_nos_note else skip_note
                )
                processing_df = processing_df[~dup_mask.values].copy()

            logger.info(f"   [{client_name}] Transforming {len(processing_df)} rows...")

            # 12. RUN TRANSFORMER
            result = transform_raw(
                raw_df=processing_df, 
                last_jv=final_start_jv, 
                last_exp=last_exp, 
                last_tr=last_tr, 
                country=country,  # <--- NEW ARGUMENT
                qbo_mappings=qbo_mappings, 
                existing_ids=preserved_ids,
                client_name=client_name
            )
            # 12b. Reserve the journal numbers before writing them anywhere else, and carry the
            #      counter to the next row of this run (it was read once, before the loop).
            global_last_jv = _reserve_journal_numbers(
                gs, control_sheet_id, ctrl_df, row_num, result.last_journal_no, global_last_jv
            )

            # 13. Write Output
            # Note: We use 'control_sheet_id' as the template source. 
            # Assumes the Client's Control Sheet has the "Sample - Journals" etc. hidden tabs.
            
            def write_tab(df_out, tab_out, templ_name):
                if not df_out.empty:
                    # Fix dates for JSON serialization
                    for col in df_out.select_dtypes(include=['datetime64', 'datetimetz']).columns:
                        df_out[col] = df_out[col].dt.strftime('%Y-%m-%d')
                    
                    gs.append_or_create_df(
                        transform_url, 
                        tab_out, 
                        df_out, 
                        template_tab_name=templ_name, 
                        template_spreadsheet_id=control_sheet_id
                    )

            write_tab(result.journals, tab_jv, "Sample - Journals")
            write_tab(result.expenses, tab_exp, "Sample - Expenses")
            write_tab(result.withdraw, tab_tr, "Sample - Transfers")

            gs.cleanup_default_sheet(transform_url)

            # 14. Check Status of Output (Any errors generated by Transformer?)
            def check_status(df):
                if df.empty: return ""
                if "Remarks" in df.columns and df["Remarks"].astype(str).str.contains("ERROR", case=False, na=False).any(): return "ERROR"
                return "READY TO SYNC"

            status_jv = check_status(result.journals)
            status_exp = check_status(result.expenses)
            status_tr = check_status(result.withdraw)

            # 15. Final Updates to Control Sheet
            final_last_row = max(last_processed, result.max_row_processed) if result.max_row_processed else last_processed
            pending_to_write = _cap_pending_nos(current_pending_nos, final_last_row)
            # KZO stores the checkpoint as a live formula over the Transform tabs rather than
            # the number this run happened to reach; everyone else keeps the snapshot.
            last_processed_cell = _kzo_checkpoint_cell(
                is_kzo_client, ctrl_df.columns, row_num, tab_prefix, final_last_row
            )

            updates = {
                settings.CTRL_COL_LAST_PROCESSED_ROW: last_processed_cell,
                COL_LAST_EXP: result.last_expense_no,
                COL_LAST_TR: result.last_withdraw_no,
                COL_PENDING_AMOUNT_NOS: _serialize_no_set(pending_to_write), # <-- ADDED
                settings.CTRL_COL_PENDING_NOS_NOTE: pending_nos_note,
                settings.CTRL_COL_TRANSFORM_NOTE: "",  # clears an earlier No-column message
                settings.CTRL_COL_LAST_RUN_AT: _now_iso_local(),
                settings.CTRL_COL_ACTIVE: "DONE"
            }
            if COL_QBO_JV in ctrl_df.columns: updates[COL_QBO_JV] = status_jv
            if COL_QBO_EXP in ctrl_df.columns: updates[COL_QBO_EXP] = status_exp
            if COL_QBO_TR in ctrl_df.columns: updates[COL_QBO_TR] = status_tr
            
            _batch_update_control(gs, control_sheet_id, settings.CONTROL_TAB_NAME, row_num, ctrl_df.columns, updates)
            logger.info(f"   ✅ [{client_name}] Row {row_num} Complete.")

        except Exception as e:
            logger.error(f"❌ [{client_name}] Error processing row {row_num}: {e}")
            _batch_update_control(gs, control_sheet_id, settings.CONTROL_TAB_NAME, row_num, ctrl_df.columns, {settings.CTRL_COL_ACTIVE: "ERROR"})
            continue

# ==========================================
# 3. MAIN ENTRY POINT
# ==========================================
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
    dispatch_ctx = single_instance_lock("run_ingestion_all_dispatch") if target_is_all else nullcontext(True)
    with dispatch_ctx as acquired:
        if target_is_all and not acquired:
            logger.warning("Another ALL ingestion dispatch is already in progress. Skipping this run.")
            return

        gs = GSheetsClient()
        
        # Initialize QBO Client with GSheets (to allow it to read/write tokens)
        qbo_client = QBOClient(gs_client=gs)

        logger.info("🌍 Reading MASTER SHEET to find active clients...")
        
        try:
            master_df = gs.read_as_df(settings.MASTER_SHEET_ID, settings.MASTER_TAB_NAME)
        except Exception as e:
            logger.error(f"❌ Critical: Could not read Master Sheet: {e}")
            return

        # Normalize headers to avoid silent misses from extra spaces/newlines in sheet columns.
        master_df.columns = [" ".join(str(c).replace("\n", " ").split()) for c in master_df.columns]

        if master_df.empty:
            logger.warning("Master sheet is empty.")
            return

        # Loop through Clients
        matched_clients = 0
        for i, client_row in master_df.iterrows():
            if not _is_target_client(client_row, target_client):
                continue
            matched_clients += 1

            client_name = str(client_row.get(settings.MST_COL_CLIENT, "Unknown"))
            status = str(client_row.get(settings.MST_COL_STATUS, "")).strip()
            
            # Filter Active Clients
            if status.lower() != "active":
                continue

            if not settings.is_allowed_workspace(client_name):
                logger.warning(
                    f"⚠️ Skipping {client_name}: workspace not allowed for QBO API. "
                    f"Allowed: {', '.join(settings.ALLOWED_QBO_WORKSPACES)}"
                )
                continue

            sheet_id = str(client_row.get(settings.MST_COL_SHEET_ID, "")).strip()
            realm_id = str(client_row.get(settings.MST_COL_REALM_ID, "")).strip()

            if not sheet_id or not realm_id:
                logger.warning(f"⚠️ Skipping {client_name}: Missing Sheet ID or Realm ID.")
                continue

            print(f"🏢 STARTING CLIENT: {client_name}")
            print(f"   Realm ID: {realm_id} | Sheet: {sheet_id}")

            client_lock_name = f"run_ingestion_client_{realm_id}"
            with single_instance_lock(client_lock_name) as client_acquired:
                if not client_acquired:
                    logger.warning(
                        f"⏭️ Skipping {client_name}: another ingestion run is already processing Realm {realm_id}."
                    )
                    continue
                # Run Ingestion for this Client
                try:
                    process_client_control_sheet(gs, qbo_client, sheet_id, client_name, realm_id)
                except Exception as e:
                    logger.error(f"❌ Critical Logic Failure for {client_name}: {e}")

        if target_client and matched_clients == 0:
            logger.warning(f"No client matched target '{target_client}'.")

        logger.info("🏁 All Clients Processed.")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run QBO ingestion/transform pipeline.")
    parser.add_argument(
        "--client",
        dest="client",
        default="",
        help="Target client name, Realm ID, Spreadsheet ID, or Output Folder ID.",
    )
    args = parser.parse_args()
    main(target_client=args.client)
