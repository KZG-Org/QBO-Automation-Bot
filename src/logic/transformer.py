from __future__ import annotations
from dataclasses import dataclass
from typing import Tuple, Dict, Any
import pandas as pd
import numpy as np
import difflib
from config import settings
import re
from src.logic.account_aliases import resolve_account_alias

# --- CONSTANTS ---
DEFAULT_JV_PREFIX = "KZO-JV"
DEFAULT_DOC_PREFIX = "KZO"
UMBER_JV_PREFIX = "UMBER-"
UMBER_DOC_PREFIX = "UMBER"
KZP_JV_PREFIX = "KZP-JV"
KZP_DOC_PREFIX = "KZP"
S5_JV_PREFIX = "S5-JV"
S5_DOC_PREFIX = "S5"
KZDW_JV_PREFIX = "KZDW-JV"
KZDW_DOC_PREFIX = "KZDW"
TDP_JV_PREFIX = "TDP-JV"
TDP_DOC_PREFIX = "TDP"

COL_NO = "No"
COL_DATE = "Date"
COL_USD = "USD - QBO"             
COL_METHOD = "QBO Method"         
COL_ACC_CR = "If Journal/Expense Method" 
COL_TR_FROM = "QBO Transfer Fr"
COL_TR_TO = "QBO Transfer To"
COL_TYPE = "Type"
COL_ITEM_DESC = "Item Description"
COL_CO = "CO"
COL_IN_OUT = "In/Out"
COL_BANK = "Account Fr"
COL_ACC_TO = "Account To"
# Raw 'USD - QBO' captured before any sign flipping, so direction rules can test the value
# exactly as it appears in the source sheet.
COL_RAW_USD = "_RawUsdQbo"

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

def safe_to_float(series: pd.Series, decimals: int = 4) -> pd.Series:
    s = pd.to_numeric(series, errors="coerce")
    s = s.replace([np.inf, -np.inf], np.nan)
    MAX_ALLOWED = 1e9
    s = s.where(s.abs() < MAX_ALLOWED)
    s = s.fillna(0.0)
    return s.round(decimals)

def _normalize_df_headers(df: pd.DataFrame) -> pd.DataFrame:
    """Cleans newlines and maps varying column names to standard constants."""
    df.columns = [str(c).replace('\n', ' ').strip() for c in df.columns]
    
    mapping = {
        "QBO Import Method (Journal/Expenses/Transfer)": COL_METHOD,
        "If Journal/Expense method: Another records": COL_ACC_CR,
        "If Transfer method: Fund Transfer From": COL_TR_FROM,
        "Transfer from": COL_TR_FROM,
        "Transfer to ((Can copy from column H )": COL_TR_TO,
        "Transfer to": COL_TR_TO,
        "USD": COL_USD  
    }
    return df.rename(columns=mapping)

# --- UPDATED HELPER FUNCTION ---
def find_id_in_map(
    mapping_dict: dict,
    search_name: str,
    allow_fuzzy: bool = True,
    client_name: str = "",
) -> str | None:
    if pd.isna(search_name) or str(search_name).strip() == "":
        return None
    
    # 1. Clean: collapse whitespace, drop the padding analysts leave around ':' path
    #    separators, then apply the workspace's account alias table (KZP's duplicated
    #    'Growth' leaf). Account callers pass client_name; location/vendor callers leave it
    #    blank and get plain normalization.
    clean_name = resolve_account_alias(search_name, client_name)
    search_lower = clean_name.lower()

    # 2. Explicit Replacements (Hardcoded fixes)
    replacements = {
        "CBD Z Card":   "KZO CBD Z",
        "Leading Card MKT - 1238": "Leading Card - 1238"
    }

    for bad_text, target_text in replacements.items():
        # Check if the bad text exists (Case Insensitive)
        if bad_text.lower() in clean_name.lower():
            # regex sub: Replace ONLY the bad_text part with target_text
            # flags=re.IGNORECASE ensures "cbd z card" matches "CBD Z Card"
            clean_name = re.sub(re.escape(bad_text), target_text, clean_name, flags=re.IGNORECASE)
            
            # Update the search variable for the next steps
            search_lower = clean_name.lower()
            break

    # --- STRATEGY 1: EXACT FULL MATCH ---
    # Checks "Fixed Assets:Equipment" == "Fixed Assets:Equipment"
    for qbo_name, qbo_id in mapping_dict.items():
        if qbo_name.lower() == search_lower: 
            print(f"   ✅ [Mapping] EXACT: '{search_name}' -> '{qbo_name}'")
            return qbo_id
    
    # --- STRATEGY 2: SUFFIX PATH MATCH (Split by :) ---
    # A sheet value may drop any number of leading parent levels, so compare against every
    # trailing path of the QBO name, not just the final segment:
    #   "Marketing:RnD:AI Expenses" -> "RnD:AI Expenses" and "AI Expenses"
    #   "Fixed Assets:Equipment"    -> "Equipment"
    # Both forms are accepted because analysts write either one.
    suffix_hits: dict[str, str] = {}
    for qbo_name, qbo_id in mapping_dict.items():
        if ":" not in qbo_name:
            continue
        parts = [p.strip() for p in qbo_name.split(":")]
        for i in range(1, len(parts)):
            if ":".join(parts[i:]).lower() == search_lower:
                suffix_hits.setdefault(qbo_id, qbo_name)
                break

    if len(suffix_hits) == 1:
        return next(iter(suffix_hits))
    if len(suffix_hits) > 1:
        # The same name lives under two different parents. Picking one is the same class of
        # error as fuzzy-matching a sibling account, so refuse when guessing is disallowed.
        if not allow_fuzzy:
            print(f"   ❌ [Mapping] AMBIGUOUS: '{search_name}' matches {sorted(suffix_hits.values())}")
            return None
        return next(iter(suffix_hits))

    # --- STRATEGY 3: FUZZY MATCH (80%) -- disabled for accounts ---
    # Only allows very close matches (typos), rejects partial substrings.
    # Callers resolving ACCOUNTS pass allow_fuzzy=False: a chart of accounts routinely holds
    # siblings differing only by a short code ("Investment in HR Company (ORZ)" vs "(OSR)"
    # scores 0.82 against the fully-qualified names), so a fuzzy guess would silently book to
    # the wrong account. Better to fail and let a human fix the sheet.
    qbo_names = list(mapping_dict.keys())
    if allow_fuzzy:
        matches = difflib.get_close_matches(clean_name, qbo_names, n=1, cutoff=0.80)
        if matches:
            best_match = matches[0]
            print(f"   ✨ [Mapping] FUZZY (80%): '{search_name}' -> '{best_match}'")
            return mapping_dict[best_match]

    near = difflib.get_close_matches(clean_name, qbo_names, n=3, cutoff=0.60)
    hint = f" Closest: {near}" if near else ""
    print(f"   ❌ [Mapping] FAILED: Could not find '{search_name}'.{hint}")
    return None

def _fix_grp_location(df: pd.DataFrame, col_name: str = "Location"):
    if col_name in df.columns:
        df[col_name] = df[col_name].apply(
            lambda x: "GROUP" if str(x).strip().upper() == "GRP" else x
        )


def _is_blank(value: Any) -> bool:
    if pd.isna(value):
        return True
    return str(value).strip() == ""


def _is_kzp_case(client_name: str = "") -> bool:
    return "kzp" in str(client_name).lower()

def _is_s5_case(client_name: str = "") -> bool:
    return "s5" in str(client_name).lower()

def _is_umber_case(client_name: str = "") -> bool:
    return "umber" in str(client_name).lower()

def _is_s5_or_umber_case(client_name: str = "") -> bool:
    return _is_s5_case(client_name) or _is_umber_case(client_name)

def _is_kzp_like_case(client_name: str = "") -> bool:
    return _is_kzp_case(client_name) or _is_s5_or_umber_case(client_name)


def _is_kzo_case(client_name: str = "") -> bool:
    return "kzo" in str(client_name).lower()


def _is_kzdw_case(client_name: str = "") -> bool:
    # TINDERPAY shares KZDW's layout and currency rules; only its prefixes differ.
    return settings.is_kzdw_family(client_name)

def _should_check_currency_transfer_only(client_name: str = "") -> bool:
    """
    Workspaces where currency validation should run only for Transfer,
    not for Journal/Expense.
    """
    return _is_kzdw_case(client_name)

def _should_check_transfer_currency(client_name: str = "") -> bool:
    return _is_kzdw_case(client_name)


def _normalize_currency(value: Any) -> str:
    text = str(value).strip() if not pd.isna(value) else ""
    if text == "":
        return "USD"
    return "USD" if "USD" in text.upper() else text

def _normalize_currency_code(value: Any) -> str:
    text = _normalize_currency(value).strip().upper()
    if not text:
        return "USD"
    return text[:3]

def _build_currency_exchange_series(
    df: pd.DataFrame,
    currency_col: str,
    rate_col: str = "Currency Rate",
) -> pd.Series:
    out = pd.Series(np.nan, index=df.index, dtype="float64")
    if rate_col in df.columns:
        raw_vals = df[rate_col].astype(str).str.replace(",", "", regex=False).str.strip()
        parsed = pd.to_numeric(raw_vals, errors="coerce")
        out = parsed.where(parsed > 0)

    if currency_col in df.columns:
        usd_mask = df[currency_col].apply(_normalize_currency_code) == "USD"
        out.loc[usd_mask & out.isna()] = 1.0
    return out


def _format_no_with_suffix(no_value: Any, suffix: str) -> str:
    raw = str(no_value).strip()
    if raw == "" or raw.lower() in {"nan", "none", "null"}:
        return suffix.lstrip(".")

    numeric = pd.to_numeric(pd.Series([no_value]), errors="coerce").iloc[0]
    if pd.notna(numeric):
        if np.isclose(float(numeric), round(float(numeric)), atol=1e-9):
            base = str(int(round(float(numeric))))
        else:
            base = raw.rstrip("0").rstrip(".")
            if base == "":
                base = raw
    else:
        base = raw
    return f"{base}{suffix}"


def _append_umber_transfer_residual_lines(umber_transfer_lines: pd.DataFrame) -> pd.DataFrame:
    """
    For Umber Journal Transfer rows in USDT/USDC:
    - group by day
    - when row count >= 2 and source USD sum != 0
    - pair each positive with the closest negative by amount
    - if positive remainder exists, append one extra line:
      No=<base>.1, Account=Payment/Banking, Amount=<remainder>
    """
    if umber_transfer_lines is None or umber_transfer_lines.empty:
        return umber_transfer_lines

    required_cols = {COL_DATE, "Amount"}
    if not required_cols.issubset(set(umber_transfer_lines.columns)):
        return umber_transfer_lines

    category_mask = pd.Series(True, index=umber_transfer_lines.index)
    if "Category" in umber_transfer_lines.columns:
        category_vals = umber_transfer_lines["Category"].fillna("").astype(str).str.strip().str.lower()
        category_mask = category_vals.isin({"transfer", "transfers"})

    if "Currency" in umber_transfer_lines.columns:
        currency_vals = umber_transfer_lines["Currency"].fillna("").astype(str).str.strip().str.upper()
        currency_mask = currency_vals.isin({"USDT", "USDC"})
    else:
        currency_mask = pd.Series(False, index=umber_transfer_lines.index)

    eligible_mask = category_mask & currency_mask
    eligible = umber_transfer_lines[eligible_mask].copy()
    if eligible.empty:
        return umber_transfer_lines

    eligible["_DateKey"] = pd.to_datetime(eligible[COL_DATE], errors="coerce", dayfirst=True).dt.normalize()
    residual_rows = []
    suffix_counts: dict[str, int] = {}

    for _, grp in eligible.groupby("_DateKey", sort=False):
        if grp.empty or len(grp) < 2:
            continue

        if COL_USD in grp.columns:
            day_sum = safe_to_float(grp[COL_USD]).sum()
        else:
            day_sum = safe_to_float(grp["Amount"]).sum()

        if np.isclose(day_sum, 0.0, atol=1e-3):
            continue

        grp_sorted = grp.sort_values(by=["_LineGroupOrder"], kind="stable") if "_LineGroupOrder" in grp.columns else grp
        positives: list[tuple[int, float]] = []
        negatives: list[tuple[int, float]] = []
        for idx, row in grp_sorted.iterrows():
            amt = float(row.get("Amount", 0) or 0)
            if amt > 1e-9:
                positives.append((idx, amt))
            elif amt < -1e-9:
                negatives.append((idx, abs(amt)))

        if not positives or not negatives:
            continue

        used_neg_positions: set[int] = set()
        for pos_idx, pos_amt in positives:
            best_pos = None
            best_diff = None
            for neg_pos, (_, neg_amt) in enumerate(negatives):
                if neg_pos in used_neg_positions:
                    continue
                diff = abs(pos_amt - neg_amt)
                if best_diff is None or diff < best_diff:
                    best_diff = diff
                    best_pos = neg_pos

            if best_pos is None:
                break

            used_neg_positions.add(best_pos)
            neg_amt = negatives[best_pos][1]
            remain = round(abs(pos_amt - neg_amt), 2)
            if remain <= 0:
                continue

            base_row = umber_transfer_lines.loc[pos_idx].copy()
            base_no = str(base_row.get("No", "")).strip()
            suffix_counts.setdefault(base_no, 0)
            suffix_counts[base_no] += 1
            suffix = f".{suffix_counts[base_no]}"

            base_row["No"] = _format_no_with_suffix(base_row.get("No", ""), suffix)
            base_row["Account"] = "Payment/Banking"
            base_row["Amount"] = remain
            base_row["Location"] = ""
            base_row["_LineRole"] = 2
            residual_rows.append(base_row)

    if not residual_rows:
        return umber_transfer_lines

    residual_df = pd.DataFrame(residual_rows)
    return pd.concat([umber_transfer_lines, residual_df], ignore_index=True)

def _account_currency_from_id(qbo_mappings: Dict[str, dict], account_id: str | None) -> str | None:
    if not account_id:
        return None
    acc_meta = (qbo_mappings or {}).get("accounts_meta", {})
    raw_currency = (acc_meta.get(account_id) or {}).get("currency")
    if not raw_currency:
        return None
    return _normalize_currency_code(raw_currency)

def _currency_mismatch_error(
    row_no: Any,
    file_currency: Any,
    account_checks: list[tuple[str, str, str | None]],
) -> str | None:
    """
    account_checks: [(label, account_name, qbo_account_currency_or_none), ...]
    """
    file_ccy = _normalize_currency_code(file_currency)
    mismatches = []
    for label, acc_name, qbo_ccy in account_checks:
        if not qbo_ccy:
            continue
        if qbo_ccy != file_ccy:
            mismatches.append(f"{label} '{acc_name}'={qbo_ccy}")

    if not mismatches:
        return None
    return (
        f"ERROR | Currency mismatch: File={file_ccy} | "
        f"{'; '.join(mismatches)} | Row No: {row_no}"
    )


def journal_columns(client_name: str = "") -> list[str]:
    """Column order of a Transform 'Journals' tab; the 'Sample - Journals' template matches it."""
    if _is_kzdw_case(client_name):
        return ["No", "Journal No", "Date", "Memo", "Account", "Amount", "Name", "Location", "Currency Code", "Currency Exchange", "Class", "Remarks"]
    return ["No", "Journal No", "Date", "Memo", "Account", "Amount", "Name", "Location", "Currency Code", "Class", "Remarks"]

def _build_id_prefixes(client_name: str = "") -> tuple[str, str]:
    if _is_umber_case(client_name):
        return UMBER_JV_PREFIX, UMBER_DOC_PREFIX
    if _is_kzp_case(client_name):
        return KZP_JV_PREFIX, KZP_DOC_PREFIX
    if _is_s5_case(client_name):
        return S5_JV_PREFIX, S5_DOC_PREFIX
    if settings.is_tinderpay_workspace(client_name):
        return TDP_JV_PREFIX, TDP_DOC_PREFIX
    if _is_kzdw_case(client_name):
        return KZDW_JV_PREFIX, KZDW_DOC_PREFIX
    return DEFAULT_JV_PREFIX, DEFAULT_DOC_PREFIX

@dataclass
class TransformResult:
    journals: pd.DataFrame
    expenses: pd.DataFrame
    withdraw: pd.DataFrame
    last_journal_no: int
    last_expense_no: int
    last_withdraw_no: int
    max_row_processed: int | None

# ==========================================
# 1. PROCESS JOURNALS
# ==========================================
def process_journals(df: pd.DataFrame, start_no: int, qbo_mappings: Dict[str, dict], existing_ids: Dict[int, str] = None, client_name: str = "") -> tuple[pd.DataFrame, int]:
    print(f"\n--- DEBUG: Processing JOURNALS (Input Rows: {len(df)}) ---")
    SPECIAL_CASE = 'Reclass'
    
    if df.empty: return pd.DataFrame(), start_no
    if COL_METHOD not in df.columns: return pd.DataFrame(), start_no
    
    # KZP special rule: Type == Reimbursements keeps Journal debit/credit behavior,
    # but Journal No is grouped by date (handled in ID generation below).
    mask_kzp_reimbursements = pd.Series(False, index=df.index)
    type_series = df[COL_TYPE] if COL_TYPE in df.columns else pd.Series("", index=df.index)
    if _is_kzp_case(client_name) and COL_TYPE in df.columns:
        mask_kzp_reimbursements = (
            type_series.astype(str).str.strip().str.lower() == "reimbursements"
        )

    # Snapshot the source sign BEFORE the Reclass flip below. The KZO Category=Transfer rule
    # is defined against the value as it appears in the sheet, not the flipped one.
    df[COL_RAW_USD] = pd.to_numeric(df[COL_USD], errors="coerce").fillna(0.0)

    # Keep true Reclass behavior for non-reimbursements only.
    df.loc[(df[SPECIAL_CASE] == 'Reclass') & (~mask_kzp_reimbursements), COL_USD] *= -1

    mask_method_journal = df[COL_METHOD].astype(str).str.contains("Journal", case=False, na=False)
    mask_method_reclass = df[COL_METHOD].astype(str).str.contains("Reclass", case=False, na=False)

    # Reimbursements should use Journal debit/credit account logic.
    mask_std = mask_method_journal | mask_kzp_reimbursements
    # Reimbursements are excluded from Reclass single-line processing.
    mask_reclass = mask_method_reclass & (~mask_kzp_reimbursements)
    
    df_std = df[mask_std].copy()
    df_reclass = df[mask_reclass].copy()
    
    current_max = start_no
    processed_std = pd.DataFrame()
    processed_reclass = pd.DataFrame()

    # --- A. STANDARD JOURNALS ---
    if not df_std.empty:
        df_std = df_std.copy()
        # Keep source row order so debit/credit lines can be written as grouped pairs.
        df_std["_LineGroupOrder"] = range(len(df_std))
        jv_prefix, _ = _build_id_prefixes(client_name)
        is_date_group_workspace = (
            (_is_kzo_case(client_name) and not _is_kzp_case(client_name))
            or _is_kzdw_case(client_name)
            or _is_s5_case(client_name)
            or _is_umber_case(client_name)
        )
        date_group_map = {}
        if is_date_group_workspace and COL_DATE in df_std.columns:
            grouped_dates = pd.to_datetime(
                df_std[COL_DATE],
                errors="coerce",
                dayfirst=_is_umber_case(client_name),
            ).dt.normalize()
        else:
            grouped_dates = pd.Series(pd.NaT, index=df_std.index)

        if _is_kzdw_case(client_name):
            currency_codes = df_std["Currency"].apply(_normalize_currency_code) if "Currency" in df_std.columns else pd.Series("USD", index=df_std.index)
        else:
            currency_codes = pd.Series("USD", index=df_std.index)

        reimbursements_in_std = pd.Series(False, index=df_std.index)
        reimburse_date_map = {}
        if _is_kzp_case(client_name) and COL_TYPE in df_std.columns:
            reimbursements_in_std = (
                df_std[COL_TYPE].astype(str).str.strip().str.lower() == "reimbursements"
            )
            if reimbursements_in_std.any() and COL_DATE in df_std.columns:
                reimburse_dates = pd.to_datetime(df_std[COL_DATE], errors="coerce").dt.normalize()
            else:
                reimburse_dates = pd.Series(pd.NaT, index=df_std.index)
        else:
            reimburse_dates = pd.Series(pd.NaT, index=df_std.index)

        generated_ids = []
        for idx, row in df_std.iterrows():
            s_no = int(float(str(row.get(COL_NO, 0))))
            if existing_ids and s_no in existing_ids:
                existing_id = existing_ids[s_no]
                generated_ids.append(existing_id)
                if is_date_group_workspace:
                    dt = grouped_dates.loc[idx]
                    if pd.notna(dt):
                        key = (dt, currency_codes.loc[idx]) if _is_kzdw_case(client_name) else dt
                        date_group_map[key] = existing_id
            elif is_date_group_workspace:
                dt = grouped_dates.loc[idx]
                if pd.notna(dt):
                    key = (dt, currency_codes.loc[idx]) if _is_kzdw_case(client_name) else dt
                    if key not in date_group_map:
                        current_max += 1
                        date_group_map[key] = f"{jv_prefix}{str(current_max).zfill(4)}"
                    generated_ids.append(date_group_map[key])
                else:
                    current_max += 1
                    generated_ids.append(f"{jv_prefix}{str(current_max).zfill(4)}")
            elif reimbursements_in_std.loc[idx]:
                dt = reimburse_dates.loc[idx]
                if pd.notna(dt):
                    if dt not in reimburse_date_map:
                        current_max += 1
                        reimburse_date_map[dt] = f"{jv_prefix}{str(current_max).zfill(4)}"
                    generated_ids.append(reimburse_date_map[dt])
                else:
                    current_max += 1
                    generated_ids.append(f"{jv_prefix}{str(current_max).zfill(4)}")
            else:
                current_max += 1
                generated_ids.append(f"{jv_prefix}{str(current_max).zfill(4)}")

        df_std["Journal No"] = generated_ids
        if _is_kzdw_case(client_name):
            df_std["Currency Code"] = currency_codes
            df_std["Currency Exchange"] = _build_currency_exchange_series(df_std, "Currency Code")
        else:
            df_std["Currency Code"] = "USD"
            df_std["Currency Exchange"] = ""
        df_std["Name"] = df_std[COL_ITEM_DESC]
        
        # Standard mapping:
        # - KZP: split by sign and use Transfer From/To accounts
        # - KZO: Debit uses Sub Category, Credit uses If Journal/Expense Method
        # - Others: Debit uses Bank/Account Fr, Credit uses If Journal/Expense Method
        if _is_umber_case(client_name):
            umber_base = df_std.copy().rename(columns={COL_ITEM_DESC: "Memo", COL_CO: "Location"})
            if "Location" not in umber_base.columns:
                umber_base["Location"] = ""
            split_base = umber_base
            umber_transfer_lines = pd.DataFrame()

            # Umber special case:
            # Category=Transfer rows already come as one negative + one positive source row.
            # Keep each source row as a single journal line and map account from Transfer From/To.
            if "Category" in umber_base.columns:
                transfer_mask = (
                    umber_base["Category"].fillna("").astype(str).str.strip().str.lower().isin({"transfer", "transfers"})
                )
                if transfer_mask.any():
                    umber_transfer_lines = umber_base[transfer_mask].copy()
                    split_base = umber_base[~transfer_mask].copy()

                    transfer_amount = safe_to_float(umber_transfer_lines[COL_USD])
                    from_vals = umber_transfer_lines[COL_TR_FROM] if COL_TR_FROM in umber_transfer_lines.columns else pd.Series("", index=umber_transfer_lines.index)
                    to_vals = umber_transfer_lines[COL_TR_TO] if COL_TR_TO in umber_transfer_lines.columns else pd.Series("", index=umber_transfer_lines.index)
                    from_txt = from_vals.fillna("").astype(str).str.strip().str.lower()
                    to_txt = to_vals.fillna("").astype(str).str.strip().str.lower()
                    blank_tokens = {"", "nan", "none", "null", "-"}
                    has_from = ~from_txt.isin(blank_tokens)
                    has_to = ~to_txt.isin(blank_tokens)

                    # Umber transfer rule (user-defined precedence):
                    # 1) if TransferFr == TransferTo => positive amount, Account = TransferFr
                    # 2) if TransferTo == "Transfer" => negative amount, Account = TransferFr
                    # 3) fallback to previous directional logic
                    from_only = has_from & ~has_to
                    to_only = has_to & ~has_from
                    both_set = has_from & has_to
                    same_from_to = both_set & (from_txt == to_txt)
                    to_is_transfer = has_to & (to_txt == "transfer")

                    umber_transfer_lines["Amount"] = np.where(
                        same_from_to,
                        transfer_amount.abs(),
                        np.where(
                            to_is_transfer,
                            -transfer_amount.abs(),
                            np.where(
                                from_only,
                                -transfer_amount.abs(),
                                np.where(
                                    to_only,
                                    transfer_amount.abs(),
                                    np.where(
                                        both_set,
                                        np.where(
                                            transfer_amount < 0,
                                            -transfer_amount.abs(),
                                            np.where(transfer_amount > 0, transfer_amount.abs(), transfer_amount),
                                        ),
                                        transfer_amount,
                                    ),
                                ),
                            ),
                        ),
                    )
                    umber_transfer_lines["Account"] = np.where(
                        same_from_to,
                        from_vals,
                        np.where(
                            to_is_transfer,
                            from_vals,
                            np.where(
                                from_only,
                                from_vals,
                                np.where(
                                    to_only,
                                    to_vals,
                                    np.where(
                                        both_set,
                                        np.where(
                                            transfer_amount < 0,
                                            from_vals,
                                            np.where(transfer_amount > 0, to_vals, from_vals),
                                        ),
                                        np.where(
                                            transfer_amount < 0,
                                            from_vals,
                                            np.where(transfer_amount > 0, to_vals, from_vals.where(has_from, to_vals)),
                                        ),
                                    ),
                                ),
                            ),
                        ),
                    )
                    umber_transfer_lines["Location"] = ""
                    umber_transfer_lines["_LineRole"] = 0

                    # TEMP DEBUG: inspect Umber transfer sign/account mapping row-by-row.
                    debug_df = pd.DataFrame(
                        {
                            "No": umber_transfer_lines.get("No", ""),
                            "TransferFr": from_vals,
                            "TransferTo": to_vals,
                            "SrcAmount": transfer_amount,
                            "FinalAmount": umber_transfer_lines["Amount"],
                            "PickedAccount": umber_transfer_lines["Account"],
                        }
                    )
                    print("   [DEBUG][UMBER][Transfer Journal Mapping]")
                    print(debug_df.to_string(index=False))

                    umber_transfer_lines = _append_umber_transfer_residual_lines(umber_transfer_lines)

            split_lines = pd.DataFrame()
            if not split_base.empty:
                amount_abs = safe_to_float(split_base[COL_USD]).abs()

                neg_line = split_base.copy()
                neg_line["Amount"] = -amount_abs
                neg_line["Account"] = neg_line[COL_TR_FROM] if COL_TR_FROM in neg_line.columns else ""
                if COL_BANK in neg_line.columns:
                    neg_line["Account"] = neg_line["Account"].where(
                        neg_line["Account"].astype(str).str.strip() != "",
                        neg_line[COL_BANK],
                    )
                    neg_line["Account"] = neg_line["Account"].fillna(neg_line[COL_BANK])
                neg_line["Location"] = ""
                neg_line["_LineRole"] = 0

                pos_line = split_base.copy()
                pos_line["Amount"] = amount_abs
                pos_line["Account"] = pos_line[COL_TR_TO] if COL_TR_TO in pos_line.columns else ""
                if COL_TYPE in pos_line.columns:
                    pos_line["Account"] = pos_line["Account"].where(
                        pos_line["Account"].astype(str).str.strip() != "",
                        pos_line[COL_TYPE],
                    )
                    pos_line["Account"] = pos_line["Account"].fillna(pos_line[COL_TYPE])
                pos_line["Location"] = ""
                pos_line["_LineRole"] = 1

                split_lines = pd.concat([neg_line, pos_line], ignore_index=True)

            processed_std = pd.concat([split_lines, umber_transfer_lines], ignore_index=True)
        elif _is_kzp_case(client_name) or _is_s5_case(client_name):
            kzp_s5_base = df_std.copy().rename(columns={COL_ITEM_DESC: "Memo", COL_CO: "Location"})
            if "Location" not in kzp_s5_base.columns:
                kzp_s5_base["Location"] = ""
            loc_fallback = kzp_s5_base[COL_CO] if COL_CO in kzp_s5_base.columns else ""

            amount_abs = safe_to_float(kzp_s5_base[COL_USD]).abs()

            neg_line = kzp_s5_base.copy()
            neg_line["Amount"] = -amount_abs
            neg_line["Account"] = neg_line[COL_TR_FROM] if COL_TR_FROM in neg_line.columns else ""
            if COL_BANK in neg_line.columns:
                neg_line["Account"] = neg_line["Account"].where(
                    neg_line["Account"].astype(str).str.strip() != "",
                    neg_line[COL_BANK],
                )
                neg_line["Account"] = neg_line["Account"].fillna(neg_line[COL_BANK])
            neg_line["Location"] = "" if _is_s5_case(client_name) else neg_line["Location"].fillna(loc_fallback)
            neg_line["_LineRole"] = 0

            pos_line = kzp_s5_base.copy()
            pos_line["Amount"] = amount_abs
            pos_line["Account"] = pos_line[COL_TR_TO] if COL_TR_TO in pos_line.columns else ""
            if COL_TYPE in pos_line.columns:
                pos_line["Account"] = pos_line["Account"].where(
                    pos_line["Account"].astype(str).str.strip() != "",
                    pos_line[COL_TYPE],
                )
                pos_line["Account"] = pos_line["Account"].fillna(pos_line[COL_TYPE])
            pos_line["Location"] = "" if _is_s5_case(client_name) else pos_line["Location"].fillna(loc_fallback)
            pos_line["_LineRole"] = 1

            processed_std = pd.concat([neg_line, pos_line], ignore_index=True)
        else:
            deb = df_std.copy()
            deb["Amount"] = safe_to_float(deb[COL_USD]) * -1

            if _is_kzo_case(client_name):
                deb = deb.rename(columns={COL_ITEM_DESC: "Memo", COL_CO: "Location"})
                # Strictly use 'Sub Category' for the debit side, ignoring 'From Account'
                deb["Account"] = deb.get(COL_TYPE, "")
            else:
                deb = deb.rename(columns={COL_ITEM_DESC: "Memo", COL_BANK: "Account", COL_CO: "Location"})
                # Fallback for old layouts where debit account sits in Type.
                if COL_TYPE in deb.columns:
                    deb["Account"] = deb["Account"].where(
                        deb["Account"].astype(str).str.strip() != "",
                        deb[COL_TYPE],
                    )
                    deb["Account"] = deb["Account"].fillna(deb[COL_TYPE])

            # Fill NA locations with raw CO value
            deb["Location"] = deb["Location"].fillna(df_std[COL_CO])
            deb["_LineRole"] = 0

            # Credit
            cred = df_std.copy()
            cred["Amount"] = pd.to_numeric(cred[COL_USD], errors='coerce').fillna(0.0)
            if _is_kzo_case(client_name):
                cred = cred.rename(columns={COL_ITEM_DESC: "Memo", COL_CO: "Location"})
                # Strictly use 'If Journal/Expense Method' for the credit side
                cred["Account"] = cred.get(COL_ACC_CR, "")
            else:
                cred = cred.rename(columns={COL_ITEM_DESC: "Memo", COL_ACC_CR: "Account", COL_CO: "Location"})
            # Fill NA locations with raw CO value
            cred["Location"] = cred["Location"].fillna(df_std[COL_CO])
            cred["_LineRole"] = 1

            processed_std = pd.concat([deb, cred], ignore_index=True)

        processed_std = processed_std.sort_values(
            by=["Journal No", "_LineGroupOrder", "_LineRole"],
            kind="stable",
        ).reset_index(drop=True)

    # --- B. RECLASS JOURNALS ---
    if not df_reclass.empty:
        jv_prefix, _ = _build_id_prefixes(client_name)
        if COL_CO in df_reclass.columns: _fix_grp_location(df_reclass, COL_CO)
        
        df_reclass["_GroupDate"] = pd.to_datetime(df_reclass[COL_DATE]).dt.normalize()
        if _is_kzdw_case(client_name):
            df_reclass["_CurrencyCode"] = df_reclass["Currency"].apply(_normalize_currency_code) if "Currency" in df_reclass.columns else "USD"
            unique_groups = sorted(df_reclass[["_GroupDate", "_CurrencyCode"]].dropna().drop_duplicates().to_records(index=False))
        else:
            unique_groups = sorted(df_reclass["_GroupDate"].dropna().unique())

        date_map = {}
        for grp in unique_groups:
            current_max += 1
            if _is_kzdw_case(client_name):
                date_map[grp] = f"{jv_prefix}{str(current_max).zfill(4)}"
            else:
                date_map[grp] = f"{jv_prefix}{str(current_max).zfill(4)}"

        if _is_kzdw_case(client_name):
            df_reclass["Journal No"] = df_reclass.apply(
                lambda r: date_map.get((r["_GroupDate"], r["_CurrencyCode"]), "") if pd.notna(r["_GroupDate"]) else "",
                axis=1,
            )
        else:
            df_reclass["Journal No"] = df_reclass["_GroupDate"].map(date_map)

        df_reclass.drop(columns=["_GroupDate", "_CurrencyCode"], inplace=True, errors='ignore')

        df_reclass["Amount"] = pd.to_numeric(df_reclass[COL_USD], errors='coerce').fillna(0.0)
        if _is_kzdw_case(client_name):
            df_reclass["Currency Code"] = df_reclass["Currency"].apply(_normalize_currency_code) if "Currency" in df_reclass.columns else "USD"
            df_reclass["Currency Exchange"] = _build_currency_exchange_series(df_reclass, "Currency Code")
        else:
            df_reclass["Currency Code"] = "USD"
            df_reclass["Currency Exchange"] = ""
        df_reclass["Class"] = ""
        df_reclass["Name"] = df_reclass[COL_ITEM_DESC]
        # Assigned before any split so both subsets keep their original relative order.
        df_reclass["_LineGroupOrder"] = range(len(df_reclass))

        # KZO ONLY: a Reclass row whose Category is Transfer describes a movement between two
        # accounts, so it must post as a debit/credit pair against From/To Account rather than
        # the usual single line. UMBER has its own Category=Transfer handling in the standard
        # journal branch above; KZP/KZDW/S5 keep the plain single-line reclass behavior.
        rc_transfer_mask = pd.Series(False, index=df_reclass.index)
        if (
            _is_kzo_case(client_name)
            and not _is_kzp_case(client_name)
            and "Category" in df_reclass.columns
        ):
            rc_transfer_mask = (
                df_reclass["Category"].fillna("").astype(str).str.strip().str.lower()
                .isin({"transfer", "transfers"})
            )

        df_rc_transfer = df_reclass[rc_transfer_mask].copy()
        df_reclass = df_reclass[~rc_transfer_mask].copy()

        reclass_out_cols = [
            "No", "Journal No", "Date", "Memo", "Account", "Amount", "Name", "Location",
            "Currency Code", "Currency Exchange", "Class", "_LineGroupOrder", "_LineRole",
        ]
        reclass_parts = []

        if not df_reclass.empty:
            df_reclass = df_reclass.rename(columns={COL_ITEM_DESC: "Memo", COL_BANK: "Account", COL_CO: "Location"})
            # Reclass follows the same fallback as standard journals.
            if COL_TYPE in df_reclass.columns:
                df_reclass["Account"] = df_reclass["Account"].where(
                    df_reclass["Account"].astype(str).str.strip() != "",
                    df_reclass[COL_TYPE],
                )
                df_reclass["Account"] = df_reclass["Account"].fillna(df_reclass[COL_TYPE])
            # Fill NA locations with raw CO value
            df_reclass["Location"] = df_reclass["Location"].fillna(df_reclass[COL_CO] if COL_CO in df_reclass.columns else "")
            df_reclass["_LineRole"] = 0
            reclass_parts.append(df_reclass[reclass_out_cols])

        if not df_rc_transfer.empty:
            raw_usd = pd.to_numeric(df_rc_transfer.get(COL_RAW_USD, 0), errors="coerce").fillna(0.0)
            amt_abs = pd.to_numeric(df_rc_transfer["Amount"], errors="coerce").fillna(0.0).abs()

            def _acc_series(col: str) -> pd.Series:
                if col in df_rc_transfer.columns:
                    return df_rc_transfer[col].fillna("").astype(str).str.strip()
                return pd.Series("", index=df_rc_transfer.index)

            from_acc = _acc_series(COL_BANK)     # "From Account"
            to_acc = _acc_series(COL_ACC_TO)     # "To Account"

            # USD - QBO > 0: money leaves From (credit/negative) and lands in To (debit/positive).
            # USD - QBO < 0: the movement is reversed.
            # Exactly 0 cannot occur in practice (zero-amount rows are held as pending during
            # ingestion) and would be dropped by the |Amount| filter below regardless.
            outbound = raw_usd >= 0
            neg_acc = pd.Series(np.where(outbound, from_acc, to_acc), index=df_rc_transfer.index)
            pos_acc = pd.Series(np.where(outbound, to_acc, from_acc), index=df_rc_transfer.index)

            neg_line = df_rc_transfer.copy()
            neg_line["Amount"] = -amt_abs
            neg_line["Account"] = neg_acc
            neg_line["_LineRole"] = 0

            pos_line = df_rc_transfer.copy()
            pos_line["Amount"] = amt_abs
            pos_line["Account"] = pos_acc
            pos_line["_LineRole"] = 1

            for part in (neg_line, pos_line):
                part.rename(columns={COL_ITEM_DESC: "Memo", COL_CO: "Location"}, inplace=True)
                if "Location" not in part.columns:
                    part["Location"] = ""
                part["Location"] = part["Location"].fillna("")
                # No Type fallback here: From/To are explicit, and a blank one must fail
                # validation rather than silently book to a category account.
                reclass_parts.append(part[reclass_out_cols])

        processed_reclass = (
            pd.concat(reclass_parts, ignore_index=True) if reclass_parts else pd.DataFrame()
        )

    # --- 3. Safe Combination ---
    total_journals = pd.concat([processed_std, processed_reclass], ignore_index=True)

    if total_journals.empty:
        return pd.DataFrame(), start_no

    total_journals = total_journals[total_journals["Amount"].abs() > 1e-9].copy()
    if "_LineGroupOrder" not in total_journals.columns:
        total_journals["_LineGroupOrder"] = 0
    if "_LineRole" not in total_journals.columns:
        total_journals["_LineRole"] = 0
     
    for col in total_journals.select_dtypes(include=['datetime64', 'datetimetz']).columns:
        total_journals[col] = total_journals[col].astype(str)
    
    total_journals["Amount"] = total_journals["Amount"].astype(float).round(2)
    diffs = total_journals.groupby("Journal No")["Amount"].sum()

    for journal_id, diff in diffs.items():
        if not np.isclose(diff, 0, atol=1e-3):
            if abs(diff) <= 0.50:
                mask = total_journals["Journal No"] == journal_id
                if not mask.any(): continue
                target_idx = total_journals[mask].index[-1]
                total_journals.loc[target_idx, "Amount"] -= diff

    # Keep each Journal No contiguous in output (debit/credit grouped together).
    total_journals = total_journals.sort_values(
        by=["Journal No", "_LineGroupOrder", "_LineRole"],
        kind="stable",
    ).reset_index(drop=True)

    # --- VALIDATION ---
    balance_map = total_journals.groupby("Journal No")["Amount"].sum()
    unbalanced_ids = balance_map[abs(balance_map) > 0.01].index.tolist()

    map_acc = qbo_mappings.get('accounts', {})
    map_loc = qbo_mappings.get('locations', {})

    def validate_journal_row(row):
        row_no = row.get("No", "")
        if row["Journal No"] in unbalanced_ids:
            diff = round(balance_map[row["Journal No"]], 2)
            return f"ERROR | Unbalanced ({diff}) | Row No: {row_no}"
        acc_name = row["Account"]
        if _is_blank(acc_name):
            return f"ERROR | Missing Account Name | Row No: {row_no}"
        
        acc_id = find_id_in_map(map_acc, acc_name, allow_fuzzy=False, client_name=client_name)
        if not acc_id:
            return f"ERROR | Account not found: '{acc_name}' | Row No: {row_no}"

        if _is_kzdw_case(client_name) and not _should_check_currency_transfer_only(client_name):
            mismatch_error = _currency_mismatch_error(
                row_no=row_no,
                file_currency=row.get("Currency Code", "USD"),
                account_checks=[("Account", acc_name, _account_currency_from_id(qbo_mappings, acc_id))],
            )
            if mismatch_error:
                return mismatch_error
             
        if not _is_s5_or_umber_case(client_name):
            loc_name = row.get("Location")
            if (not _is_blank(loc_name)) and not find_id_in_map(map_loc, loc_name):
                return f"ERROR | Location not found: '{loc_name}' | Row No: {row_no}"
        
        return "Ready to sync"

    if _is_s5_or_umber_case(client_name):
        total_journals["Location"] = ""
    total_journals["Remarks"] = total_journals.apply(validate_journal_row, axis=1)
    total_journals["Class"] = ""  if _is_s5_or_umber_case(client_name) else total_journals.get("Location", "").fillna("")

    cols_order = journal_columns(client_name)
    for c in cols_order:
        if c not in total_journals.columns: total_journals[c] = ""
    
    total_journals["Amount"] = total_journals["Amount"].astype(float).round(2)
    return total_journals[cols_order], current_max

# ==========================================
# 2. PROCESS EXPENSES
# ==========================================
def process_expenses(df: pd.DataFrame, country: str,
                     start_no: int, qbo_mappings: Dict[str, dict], existing_ids: Dict[int, str] = None, client_name: str = "") -> Tuple[pd.DataFrame, int]:
    print(f"\n--- DEBUG: Processing EXPENSES (Input Rows: {len(df)}) ---")
    if df is None or df.empty: return pd.DataFrame(), start_no
    if existing_ids is None: existing_ids = {}

    df[COL_USD] = pd.to_numeric(df[COL_USD], errors='coerce').fillna(0.0)
    d = df[df[COL_USD].round(2) != 0].copy()
    
    if len(df) != len(d):
        print(f"      ⚠️ Dropped {len(df) - len(d)} rows due to 0.00 amount.")
        
    if d.empty: return pd.DataFrame(), start_no

    if _is_kzdw_case(client_name):
        # Keep "Currency" for KZDW while dropping transfer-side currency columns.
        d = d[[c for c in d.columns if c.lower() not in {"currency to", "currency from"}]]
    else:
        d = d[[c for c in d.columns if "currency" not in c.lower()]]

    if COL_METHOD in d.columns: 
        d = d[d[COL_METHOD].astype(str).str.contains("Expense", case=False, na=False)]

    # KZP reimbursements are journal-only; do not duplicate into Expense tab.
    if _is_kzp_case(client_name) and COL_TYPE in d.columns:
        d = d[d[COL_TYPE].astype(str).str.strip().str.lower() != "reimbursements"]
    
    if COL_IN_OUT in d.columns:
        numeric_vals = pd.to_numeric(d[COL_IN_OUT], errors="coerce").fillna(0)
        d = d[numeric_vals < 0]

    if d.empty: 
        return pd.DataFrame(), start_no

    d["Payee (Dummy)"] = "Dummy"
    d["Payment Method"] = "Cash"
    if _is_kzdw_case(client_name) and "Currency" in d.columns:
        d["Currency Code"] = d["Currency"].apply(_normalize_currency)
        d["Currency Exchange"] = _build_currency_exchange_series(d, "Currency Code")
    else:
        d["Currency Code"] = "USD"
        d["Currency Exchange"] = ""
    # Expense source account:
    # Prefer Transfer From (W) and fall back to If Journal/Expense Method (V).
    if COL_TR_FROM in d.columns:
        transfer_from_vals = d[COL_TR_FROM]
        fallback_vals = d[COL_ACC_CR] if COL_ACC_CR in d.columns else ""
        d["Account (Cr)"] = transfer_from_vals.where(
            transfer_from_vals.astype(str).str.strip() != "",
            fallback_vals,
        )
        d["Account (Cr)"] = d["Account (Cr)"].fillna(fallback_vals)
    else:
        d["Account (Cr)"] = d[COL_ACC_CR]
    
    d["Expense Line Amount"] = safe_to_float(d[COL_USD]) * -1
    d["Payment Date"] = pd.to_datetime(d[COL_DATE], errors="coerce")

    # ID GENERATION
    _, doc_prefix = _build_id_prefixes(client_name)
    ref_nos = []
    for i, row in d.iterrows():
        s_no = int(row.get(COL_NO, 0))
        if existing_ids and s_no in existing_ids:
            ref_nos.append(existing_ids[s_no])
        else:
            mm_yy = row["Payment Date"].strftime("%m%y") if pd.notna(row["Payment Date"]) else "0000"
            start_no += 1 
            if _is_kzp_case(client_name) or _is_s5_case(client_name) or _is_umber_case(client_name):
                ref_nos.append(f"{doc_prefix}{mm_yy}E{str(start_no).zfill(4)}")
            elif _is_kzdw_case(client_name):
                ref_nos.append(f"{doc_prefix}{mm_yy}E{str(start_no).zfill(4)}")
            else:
                ref_nos.append(f"{doc_prefix}{country}{mm_yy}E{str(start_no).zfill(4)}")
    
    d["Exp Ref. No"] = ref_nos

    rename_map = {
        COL_ITEM_DESC: "Memo",
        COL_CO: "Location",
        COL_TYPE: "Expense Account (Dr)",
        "Currency Code": "Currency"
    }
    # Avoid duplicate "Currency" headers when raw currency is still present (e.g. KZDW).
    if "Currency" in d.columns and "Currency Code" in d.columns:
        d = d.drop(columns=["Currency"])
    d = d.rename(columns=rename_map)
    d["Expense Description"] = d["Memo"]

    if _is_s5_or_umber_case(client_name):
        d["Location"] = ""
        d["Class"] = ""
    else:
        _fix_grp_location(d, "Location")
        # Fill NA locations with raw CO value
        d["Location"] = d["Location"].fillna(d.get(COL_CO, ""))
        d["Class"] = d["Location"].fillna("")

    # Validation
    map_acc = qbo_mappings.get('accounts', {})
    map_loc = qbo_mappings.get('locations', {})
    
    # DEBUG: Log account mappings for troubleshooting
    if not map_acc:
        print(f"   ⚠️ WARNING: No accounts in QBO mappings for country={country}. Check realm ID.")

    def validate_expense_row(row):
        row_no = row.get("No", "")
        if _is_blank(row["Account (Cr)"]):
            return f"ERROR | Missing Source Account | Row No: {row_no}"
        if _is_blank(row["Expense Account (Dr)"]):
            return f"ERROR | Missing Expense Account | Row No: {row_no}"
        if pd.isna(row["Payment Date"]):
            return f"ERROR | Missing Date | Row No: {row_no}"
        
        # --- MAPPING CHECKS ---
        src_acc = row["Account (Cr)"]
        exp_acc = row["Expense Account (Dr)"]
        
        src_acc_id = find_id_in_map(map_acc, src_acc, allow_fuzzy=False, client_name=client_name)
        if not src_acc_id:
            available = ', '.join(list(map_acc.keys())[:3]) if map_acc else 'NONE'
            return f"ERROR | Source Account not in QBO: '{src_acc}' | Available: {available}... | Row No: {row_no}"
             
        exp_acc_id = find_id_in_map(map_acc, exp_acc, allow_fuzzy=False, client_name=client_name)
        if not exp_acc_id:
            available = ', '.join(list(map_acc.keys())[:3]) if map_acc else 'NONE'
            return f"ERROR | Expense Account not in QBO: '{exp_acc}' | Available: {available}... | Row No: {row_no}"

        if _is_kzdw_case(client_name) and not _should_check_currency_transfer_only(client_name):
            mismatch_error = _currency_mismatch_error(
                row_no=row_no,
                file_currency=row.get("Currency", "USD"),
                account_checks=[
                    ("Source", src_acc, _account_currency_from_id(qbo_mappings, src_acc_id)),
                    ("Expense", exp_acc, _account_currency_from_id(qbo_mappings, exp_acc_id)),
                ],
            )
            if mismatch_error:
                return mismatch_error
             
        if not _is_s5_or_umber_case(client_name):
            loc_name = row.get("Location")
            if (not _is_blank(loc_name)) and not find_id_in_map(map_loc, loc_name):
                return f"ERROR | Location not in QBO: '{loc_name}' | Row No: {row_no}"
        return "Ready to sync"

    d["Remarks"] = d.apply(validate_expense_row, axis=1)

    if _is_kzdw_case(client_name):
        cols_order = ["No", "Exp Ref. No", "Account (Cr)", "Payee (Dummy)", "Memo", "Payment Date", "Payment Method", "Expense Account (Dr)", "Expense Description", "Expense Line Amount", "Currency", "Currency Exchange", "Location", "Class", "Remarks"]
    else:
        cols_order = ["No", "Exp Ref. No", "Account (Cr)", "Payee (Dummy)", "Memo", "Payment Date", "Payment Method", "Expense Account (Dr)", "Expense Description", "Expense Line Amount", "Currency", "Location", "Class", "Remarks"]
    for c in cols_order:
        if c not in d.columns: d[c] = ""
    
    d = d[cols_order].copy()
    for col in d.select_dtypes(include=["datetime64", "datetimetz"]).columns:
        d[col] = d[col].astype(str)
    
    return d, start_no

# ==========================================
# 3. PROCESS TRANSFERS
# ==========================================
def process_transfers(df: pd.DataFrame, country: str,
                      start_no: int, qbo_mappings: Dict[str, dict], existing_ids: Dict[int, str] = None, client_name: str = "") -> tuple[pd.DataFrame, int]:
    print(f"\n--- DEBUG: Processing TRANSFERS (Input Rows: {len(df)}) ---")
    if df.empty: return pd.DataFrame(), start_no
    if existing_ids is None: existing_ids = {}

    df[COL_USD] = pd.to_numeric(df[COL_USD], errors='coerce').fillna(0.0)
    transfers = df[df[COL_USD].round(2) != 0].copy()

    if COL_METHOD in transfers.columns:
        transfers = transfers[transfers[COL_METHOD].astype(str).str.contains("Transfer", case=False, na=False)]

    # KZP reimbursements are journal-only; do not duplicate into Transfer tab.
    if _is_kzp_case(client_name) and COL_TYPE in transfers.columns:
        transfers = transfers[transfers[COL_TYPE].astype(str).str.strip().str.lower() != "reimbursements"]
    
    if transfers.empty: 
        return pd.DataFrame(), start_no

    # ID GENERATION
    _, doc_prefix = _build_id_prefixes(client_name)
    ref_nos = []
    for i, row in transfers.iterrows():
        s_no = int(row.get(COL_NO, 0))
        if existing_ids and s_no in existing_ids:
            ref_nos.append(existing_ids[s_no])
        else:
            dt = pd.to_datetime(row[COL_DATE], errors='coerce')
            date_str = dt.strftime('%m%y') if pd.notna(dt) else "0000"
            start_no += 1
            if _is_kzp_case(client_name) or _is_s5_case(client_name) or _is_umber_case(client_name):
                ref_nos.append(f"{doc_prefix}{date_str}T{str(start_no).zfill(4)}")
            elif _is_kzdw_case(client_name):
                ref_nos.append(f"{doc_prefix}{date_str}T{str(start_no).zfill(4)}")
            else:
                ref_nos.append(f"{doc_prefix}{country}{date_str}T{str(start_no).zfill(4)}")

    transfers["Ref No"] = ref_nos

    rename_map = {
        COL_TR_FROM: "Transfer Funds From",
        COL_TR_TO: "Transfer Funds To",
        COL_ITEM_DESC: "Memo",
        COL_CO: "Location",
    }
    transfers = transfers.rename(columns=rename_map)

    # QBO Transfer requires positive amount.
    # For negative source amounts, flip direction to preserve movement semantics.
    negative_mask = pd.to_numeric(transfers[COL_USD], errors="coerce").fillna(0.0) < 0
    if negative_mask.any():
        from_vals = transfers.loc[negative_mask, "Transfer Funds From"].copy()
        transfers.loc[negative_mask, "Transfer Funds From"] = transfers.loc[negative_mask, "Transfer Funds To"]
        transfers.loc[negative_mask, "Transfer Funds To"] = from_vals

    transfers["Transfer Amount"] = pd.to_numeric(transfers[COL_USD], errors="coerce").fillna(0.0).abs()
    if _is_kzdw_case(client_name) and "Currency" in transfers.columns:
        transfers["Currency"] = transfers["Currency"].apply(_normalize_currency)
        transfers["Currency Exchange"] = _build_currency_exchange_series(transfers, "Currency")
    else:
        transfers["Currency"] = "USD"
        transfers["Currency Exchange"] = ""
    transfers["Memo"] = transfers["Ref No"] + " - " + transfers["Memo"].astype(str)
    
    if _is_s5_or_umber_case(client_name):
        transfers["Location"] = ""
        transfers["Class"] = ""
    else:
        _fix_grp_location(transfers, "Location")
        # Fill NA locations with raw CO value
        transfers["Location"] = transfers["Location"].fillna(transfers.get(COL_CO, ""))
        transfers["Class"] = transfers["Location"].fillna("")

    # Validation
    map_acc = qbo_mappings.get('accounts', {})
    map_loc = qbo_mappings.get('locations', {})
    
    # DEBUG: Log account mappings for troubleshooting
    if not map_acc:
        print(f"   ⚠️ WARNING: No accounts in QBO mappings for country={country}. Check realm ID.")
    
    def validate_transfer_row(row):
        row_no = row.get("No", "")
        if _is_blank(row["Transfer Funds From"]):
            return f"ERROR | Missing From Account | Row No: {row_no}"
        if _is_blank(row["Transfer Funds To"]):
            return f"ERROR | Missing To Account | Row No: {row_no}"
        
        from_acc = row["Transfer Funds From"]
        to_acc = row["Transfer Funds To"]
        
        from_acc_id = find_id_in_map(map_acc, from_acc, allow_fuzzy=False, client_name=client_name)
        if not from_acc_id:
            available = ', '.join(list(map_acc.keys())[:3]) if map_acc else 'NONE'
            return f"ERROR | 'From' Account not in QBO: '{from_acc}' | Available: {available}... | Row No: {row_no}"
             
        to_acc_id = find_id_in_map(map_acc, to_acc, allow_fuzzy=False, client_name=client_name)
        if not to_acc_id:
            available = ', '.join(list(map_acc.keys())[:3]) if map_acc else 'NONE'
            return f"ERROR | 'To' Account not in QBO: '{to_acc}' | Available: {available}... | Row No: {row_no}"

        row_currency_code = _normalize_currency_code(row.get("Currency", "USD"))
        if _should_check_transfer_currency(client_name) and row_currency_code == "USD":
            mismatch_error = _currency_mismatch_error(
                row_no=row_no,
                file_currency=row_currency_code,
                account_checks=[
                    ("From", from_acc, _account_currency_from_id(qbo_mappings, from_acc_id)),
                    ("To", to_acc, _account_currency_from_id(qbo_mappings, to_acc_id)),
                ],
            )
            if mismatch_error:
                return mismatch_error
             
        if row["Transfer Funds From"] == row["Transfer Funds To"]: 
            return f"ERROR | 'From' and 'To' Accounts cannot be the same | Row No: {row_no}"

        # Two different names can still resolve to one QBO account -- via the explicit
        # replacements above ("CBD Z Card" -> "KZO CBD Z") or a leaf name matching a
        # sub-account. QBO answers such a transfer with a bare 400 Bad Request, so catch it
        # here where the row can be fixed instead of at sync time.
        if from_acc_id == to_acc_id:
            return (
                f"ERROR | 'From' and 'To' resolve to the same QBO account (Id {from_acc_id}): "
                f"'{from_acc}' -> '{to_acc}' | Row No: {row_no}"
            )
            
        if not _is_s5_or_umber_case(client_name):
            loc_name = row.get("Location")
            if (not _is_blank(loc_name)) and not find_id_in_map(map_loc, loc_name):
                return f"ERROR | Location not in QBO: '{loc_name}' | Row No: {row_no}"
        return "Ready to sync"

    transfers["Remarks"] = transfers.apply(validate_transfer_row, axis=1)

    if _is_kzdw_case(client_name):
        cols_order = ["No", "Ref No", "Transfer Funds From", "Transfer Funds To", "Transfer Amount", "Memo", COL_DATE, "Location", "Class", "Currency", "Currency Exchange", COL_TYPE, "Remarks"]
    else:
        cols_order = ["No", "Ref No", "Transfer Funds From", "Transfer Funds To", "Transfer Amount", "Memo", COL_DATE, "Location", "Class", "Currency", COL_TYPE, "Remarks"]
    for c in cols_order:
        if c not in transfers.columns: transfers[c] = ""
    
    return transfers[cols_order], start_no

# ==========================================
# 4. MAIN TRANSFORM ENTRY POINT
# ==========================================
def transform_raw(raw_df: pd.DataFrame, country: str,
                  last_jv: int, last_exp: int, last_tr: int, qbo_mappings: Dict[str, dict] = None, existing_ids: Dict[str, dict] = None, client_name: str = "") -> TransformResult:
    if raw_df is None or raw_df.empty:
        return TransformResult(pd.DataFrame(), pd.DataFrame(), pd.DataFrame(), last_jv, last_exp, last_tr, None)

    df = _normalize_df_headers(raw_df.copy())

    # Shared Cleaning
    if COL_NO in df.columns:
        df[COL_NO] = pd.to_numeric(df[COL_NO], errors="coerce").fillna(0).astype(int)

    if "Category" in df.columns: 
        df = df[df["Category"].fillna("").astype(str).str.strip() != ""]

    if COL_DATE in df.columns:
        df[COL_DATE] = parse_mixed_date(df[COL_DATE])

    if COL_IN_OUT in df.columns: 
        df[COL_IN_OUT] = pd.to_numeric(df[COL_IN_OUT], errors="coerce").fillna(0)

    if COL_USD in df.columns:
        df[COL_USD] = pd.to_numeric(df[COL_USD], errors="coerce").fillna(0.0)
        df = df[~df[COL_USD].isna()]

    final_jv, new_jv_no = process_journals(df, last_jv, qbo_mappings, existing_ids.get('journals') if existing_ids else None, client_name=client_name)
    final_exp, new_exp_no = process_expenses(df, country, last_exp, qbo_mappings, existing_ids.get('expenses') if existing_ids else None, client_name=client_name)
    final_tr, new_tr_no = process_transfers(df, country, last_tr, qbo_mappings, existing_ids.get('transfers') if existing_ids else None, client_name=client_name)

    max_row = int(df[COL_NO].max()) if not df.empty else None

    return TransformResult(
        journals=final_jv,
        expenses=final_exp,
        withdraw=final_tr,
        last_journal_no=new_jv_no,
        last_expense_no=new_exp_no,
        last_withdraw_no=new_tr_no,
        max_row_processed=max_row
    )
