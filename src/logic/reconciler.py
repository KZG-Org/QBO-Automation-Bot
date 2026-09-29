from __future__ import annotations
import pandas as pd
import calendar
import re
import difflib
from config import settings
from src.connectors.qbo_client import QBOClient
from src.logic.account_aliases import resolve_account_alias_ex
from src.utils.logger import setup_logger

logger = setup_logger("reconciler")

class Reconciler:
    def __init__(self, qbo_client: QBOClient):
        self.client = qbo_client
        self._qbo_entity_cache: dict[tuple[str, str, str], tuple[dict, dict]] = {}
        self._qbo_transfer_cache: dict[tuple[str, str], list] = {}

    def _coerce_scalar(self, val):
        """Collapse Series/DataFrame values (from duplicate columns) into one scalar."""
        if isinstance(val, pd.DataFrame):
            if val.empty:
                return ""
            flat_vals = pd.Series(val.to_numpy().ravel())
            if flat_vals.empty:
                return ""
            non_na = flat_vals[flat_vals.notna()]
            return non_na.iloc[0] if not non_na.empty else flat_vals.iloc[0]

        if isinstance(val, pd.Series):
            if val.empty:
                return ""
            non_na = val[val.notna()]
            return non_na.iloc[0] if not non_na.empty else val.iloc[0]

        return val

    def _row_get_scalar(self, row: pd.Series, key: str, default=""):
        """Safe cell getter that tolerates duplicate column names."""
        if key not in row:
            return default
        try:
            val = row.get(key, default)
        except Exception:
            return default
        val = self._coerce_scalar(val)
        try:
            if pd.isna(val):
                return default
        except Exception:
            pass
        return val

    def _has_value(self, val) -> bool:
        v = self._coerce_scalar(val)
        try:
            if pd.isna(v):
                return False
        except Exception:
            pass
        return str(v).strip().lower() not in {"", "nan", "none", "null"}

    def _safe_float(self, val) -> float:
        """Converts string with commas '1,234.56' to float 1234.56"""
        val = self._coerce_scalar(val)
        try:
            if pd.isna(val) or val == "":
                return 0.0
        except Exception:
            if val == "":
                return 0.0
        if isinstance(val, (int, float)): return float(val)
        try:
            clean_val = str(val).replace(",", "").strip()
            return float(clean_val)
        except ValueError:
            # LOGGING: Warn if conversion fails
            # logger.warning(f"⚠️ Could not convert '{val}' to float. Defaulting to 0.0")
            return 0.0

    def _normalize_account(self, name: str) -> str:
        """
        Simple normalization for display/logging.
        Example: 'Fixed Assets:Equipment' -> 'equipment'
        """
        if not name: return ""
        return name.split(":")[-1].strip().lower()

    def _normalize_qbo_id(self, qbo_id_val) -> str:
        """
        Normalizes sheet QBO IDs.
        Supports legacy journal line IDs like '2888.1' -> '2888'.
        """
        raw = str(qbo_id_val or "").strip()
        if not raw:
            return ""
        m = re.match(r"^(\d+)\.\d+$", raw)
        if m:
            return m.group(1)
        return raw

    def _normalize_date_str(self, val) -> str:
        """Best-effort 'YYYY-MM-DD' normalization; '' if unparseable.

        Raw sheets are read with value_render_option='UNFORMATTED_VALUE', so raw Date
        cells arrive as bare Excel serial numbers (e.g. 45871.0), not date strings.
        pd.to_datetime() on a bare number defaults to Unix-epoch nanoseconds, which
        silently produces the wrong date instead of erroring -- so that case must be
        special-cased before falling back to normal string/datetime parsing.
        """
        val = self._coerce_scalar(val)
        if val is None or val == "":
            return ""

        if isinstance(val, (int, float)) and not isinstance(val, bool):
            if not (20000 <= val <= 90000):
                return ""
            dt = pd.to_datetime(val, unit="D", origin="1899-12-30", errors="coerce")
            return "" if pd.isna(dt) else dt.strftime("%Y-%m-%d")

        try:
            dt = pd.to_datetime(val, errors="coerce")
        except Exception:
            return ""
        if pd.isna(dt):
            return ""
        return dt.strftime("%Y-%m-%d")

    def _get_month_range(self, date_str: str) -> tuple[str, str]:
        try:
            dt = pd.to_datetime(date_str)
            _, last_day = calendar.monthrange(dt.year, dt.month)
            return f"{dt.year}-{dt.month:02d}-01", f"{dt.year}-{dt.month:02d}-{last_day}"
        except:
            return None, None

    def _fetch_qbo_data(self, entity: str, start_date: str, end_date: str) -> tuple[dict, dict]:
        cache_key = (entity, start_date, end_date)
        if cache_key in self._qbo_entity_cache:
            logger.info(f"   ⚡ Using cached QBO {entity} [{start_date} to {end_date}]")
            return self._qbo_entity_cache[cache_key]

        logger.info(f"   🔍 Querying QBO {entity} [{start_date} to {end_date}]...")
        query = f"SELECT * FROM {entity} WHERE TxnDate >= '{start_date}' AND TxnDate <= '{end_date}' MAXRESULTS 1000"
        results = self.client.query(query)
        
        map_id = {}
        map_doc = {}
        
        for item in results:
            if "Id" in item: map_id[str(item["Id"])] = item
            doc_num = item.get("DocNumber")
            if doc_num: map_doc[str(doc_num)] = item

        out = (map_id, map_doc)
        self._qbo_entity_cache[cache_key] = out
        return out

    def _fetch_transfers_list(self, start_date: str, end_date: str) -> list:
        cache_key = (start_date, end_date)
        if cache_key in self._qbo_transfer_cache:
            logger.info(f"   ⚡ Using cached QBO Transfers [{start_date} to {end_date}]")
            return self._qbo_transfer_cache[cache_key]

        logger.info(f"   🔍 Querying QBO Transfers [{start_date} to {end_date}]...")
        query = f"SELECT * FROM Transfer WHERE TxnDate >= '{start_date}' AND TxnDate <= '{end_date}' MAXRESULTS 1000"
        out = self.client.query(query)
        self._qbo_transfer_cache[cache_key] = out
        return out

    # --- NEW: ROBUST MATCHING LOGIC (COPIED FROM SYNCING) ---
    def _is_account_match(self, sheet_acc: str, qbo_acc: str) -> bool:
        """
        Returns True if sheet_acc matches qbo_acc using:
        1. Workspace account aliases + Explicit Replacements
        2. Exact Match
        3. Suffix-path Match
        4. Fuzzy Match (>80%)
        """
        if not sheet_acc or not qbo_acc: return False
        
        # 0. Basic Clean + workspace account aliases (KZP's duplicated 'Growth' leaf), so
        #    the sheet value is compared in the same qualified form the sync stage posted.
        sheet_clean, sheet_aliased = resolve_account_alias_ex(
            sheet_acc, getattr(self.client, "client_name", "")
        )
        qbo_clean = re.sub(r'\s+', ' ', str(qbo_acc)).strip()
        
        # 1. Replacements (Hardcoded fixes)
        replacements = {
            "CBD Z Card":   "KZO CBD Z",
            "Leading Card MKT - 1238": "Leading Card - 1238"
        }
        for bad_text, target_text in replacements.items():
            if bad_text.lower() in sheet_clean.lower():
                sheet_clean = re.sub(re.escape(bad_text), target_text, sheet_clean, flags=re.IGNORECASE)
                break

        s_lower = sheet_clean.lower()
        q_lower = qbo_clean.lower()

        # 2. EXACT MATCH
        if s_lower == q_lower: return True

        # 3. SUFFIX-PATH MATCH -- every trailing path of the QBO name, matching the
        #    transform/sync matchers: Sheet="Equipment" or "Fixed Assets:Equipment"
        #    both match QBO="Fixed Assets:Equipment".
        if ":" in q_lower:
            q_parts = [part.strip() for part in q_lower.split(":")]
            for i in range(1, len(q_parts)):
                if ":".join(q_parts[i:]) == s_lower:
                    return True
        
        # 4. FUZZY MATCH (80%) -- never for an alias-disambiguated account. Two 'Growth'
        #    leaves under different parents score 0.81 against each other, so fuzzy here
        #    would pass the exact wrong-parent posting the alias table exists to catch.
        if sheet_aliased:
            # QBO sometimes reports a bare account Name where a fully qualified path was
            # expected. With no parent to compare against, the two same-named leaves are
            # indistinguishable -- compare leaves rather than report a mismatch that the
            # QBO payload cannot actually prove.
            if ":" not in q_lower:
                return s_lower.split(":")[-1].strip() == q_lower
            return False

        # Check against full name
        if difflib.SequenceMatcher(None, s_lower, q_lower).ratio() >= 0.80:
            return True
        
        # Check against leaf name (often needed for fuzzy matches on sub-accounts)
        if ":" in q_lower:
            q_leaf = q_lower.split(":")[-1].strip()
            if difflib.SequenceMatcher(None, s_lower, q_leaf).ratio() >= 0.80:
                return True
                
        return False

    # --- 1. JOURNALS (UPGRADED) ---
    def reconcile_journals(self, df: pd.DataFrame, month_str: str) -> list[dict]:
        """
        Performs Line-by-Line reconciliation with Fuzzy Matching.
        """
        if df.empty or "Journal No" not in df.columns: return []

        start, end = self._get_month_range(month_str)
        if not start: return []

        map_id, map_doc = self._fetch_qbo_data("JournalEntry", start, end)
        updates = []

        # Process each Journal Group (e.g., JV-001)
        for jv_no, group in df.groupby("Journal No"):
            first_row = group.iloc[0]
            
            # 1. FIND QBO RECORD
            qbo_record = None
            qbo_id_val = self._row_get_scalar(first_row, "QBO ID", "")
            if self._has_value(qbo_id_val):
                qbo_record = map_id.get(self._normalize_qbo_id(qbo_id_val))
            if not qbo_record:
                qbo_record = map_doc.get(str(jv_no).strip())

            # 2. IF MISSING ENTIRELY
            if not qbo_record:
                for idx in group.index:
                    updates.append({"row_idx": idx, "status": "❌ Journal Not Found in QBO"})
                continue

            # 3. CHECK HEADER (Date only; memo check intentionally skipped)
            header_errors = []
            
            # Date Check
            sheet_date = pd.to_datetime(self._row_get_scalar(first_row, "Date", "")).strftime("%Y-%m-%d")
            qbo_date = qbo_record.get("TxnDate")
            if sheet_date != qbo_date:
                header_errors.append(f"Date Mismatch ({sheet_date} vs {qbo_date})")

            header_status_prefix = "⚠️ " + "; ".join(header_errors) + " | " if header_errors else ""

            # 4. PREPARE QBO LINES POOL (For Matching)
            qbo_lines_pool = []
            for line in qbo_record.get("Line", []):
                detail = line.get("JournalEntryLineDetail", {})
                
                # STORE FULL NAME FOR FUZZY MATCHING
                full_acc_name = detail.get("AccountRef", {}).get("name", "")
                
                # Determine amount (Debit positive, Credit negative for matching logic)
                amt = float(line.get("Amount", 0))
                if detail.get("PostingType") == "Credit":
                    amt = -amt
                
                qbo_lines_pool.append({
                    "full_name": full_acc_name, # Stored for matching
                    "display_name": self._normalize_account(full_acc_name), # Stored for error msg
                    "amount": amt,
                    # Sync writes the sheet's Memo into each line's Description; used to break ties.
                    "description": re.sub(r"\s+", " ", str(line.get("Description") or "")).strip(),
                    "matched": False
                })

            # 5. MATCH SHEET ROWS TO QBO LINES
            # Each pass runs over the whole journal before the next one starts, so a row's
            # fallback (amount-only / account-only) can never claim a QBO line that a later
            # row matches exactly. Row by row, a sheet line already removed from QBO took the
            # next same-account line as an "Amount Mismatch" and left its real owner unmatched.
            ok_status = f"⚠️ Header: {'; '.join(header_errors)}" if header_errors else "✅ Matched"
            pending = [
                (idx, str(self._row_get_scalar(row, "Account", "")),
                 self._safe_float(self._row_get_scalar(row, "Amount", 0)),
                 re.sub(r"\s+", " ", str(self._row_get_scalar(row, "Memo", "") or "")).strip())
                for idx, row in group.iterrows()
            ]
            status_by_idx = {}

            def claim(accept, describe):
                for idx, sheet_acc_raw, sheet_amt, sheet_memo in pending:
                    if idx in status_by_idx:
                        continue
                    candidates = [q for q in qbo_lines_pool if not q["matched"] and accept(sheet_acc_raw, sheet_amt, q)]
                    if not candidates:
                        continue
                    # Among the lines this pass accepts, prefer the one carrying this row's memo,
                    # then the nearest amount; min() keeps QBO's line order for full ties.
                    q_line = min(candidates, key=lambda q: (q["description"] != sheet_memo, abs(q["amount"] - sheet_amt)))
                    q_line["matched"] = True
                    status_by_idx[idx] = describe(sheet_acc_raw, sheet_amt, q_line)

            # Pass 1: Account AND Strict Amount Match
            claim(lambda acc, amt, q: self._is_account_match(acc, q["full_name"]) and abs(q["amount"] - amt) < 0.01,
                  lambda acc, amt, q: ok_status)
            # Pass 2: Amount Match ONLY (Wrong Account)
            claim(lambda acc, amt, q: abs(q["amount"] - amt) < 0.01,
                  lambda acc, amt, q: f"{header_status_prefix}❌ Account Mismatch (Sheet: '{acc}' vs QBO: '{q['display_name']}')")
            # Pass 3: Account Match ONLY (Wrong Amount)
            claim(lambda acc, amt, q: self._is_account_match(acc, q["full_name"]),
                  lambda acc, amt, q: f"{header_status_prefix}❌ Amount Mismatch (Sheet: {amt} vs QBO: {q['amount']})")

            for idx, sheet_acc_raw, sheet_amt, _ in pending:
                # Pass 4: No match found
                updates.append({
                    "row_idx": idx,
                    "status": status_by_idx.get(
                        idx, f"{header_status_prefix}❌ No matching line in QBO for '{sheet_acc_raw}' : {sheet_amt}"
                    ),
                })

        return updates

    # --- 2. EXPENSES (UPDATED: ONLY CHECK PAYMENT ACCOUNT) ---
    def reconcile_expenses(self, df: pd.DataFrame, month_str: str) -> list[dict]:
        """
        Validates Date, Amount, and PAYMENT ACCOUNT (Account Cr).
        Ignores Expense Category Lines.
        """
        if df.empty or "Exp Ref. No" not in df.columns: return []
        start, end = self._get_month_range(month_str)
        if not start: return []
        map_id, map_doc = self._fetch_qbo_data("Purchase", start, end)
        updates = []

        for idx, row in df.iterrows():
            qbo_record = None
            qbo_id_val = self._row_get_scalar(row, "QBO ID", "")
            if self._has_value(qbo_id_val):
                qbo_record = map_id.get(self._normalize_qbo_id(qbo_id_val))
            if not qbo_record:
                qbo_record = map_doc.get(str(self._row_get_scalar(row, "Exp Ref. No", "")).strip())

            if not qbo_record:
                updates.append({"row_idx": idx, "status": "❌ Not found in QBO"})
                continue

            errors = []
            
            # 1. Date Check
            s_date = pd.to_datetime(self._row_get_scalar(row, "Payment Date", "")).strftime("%Y-%m-%d")
            q_date = qbo_record.get("TxnDate")
            if s_date != q_date: errors.append(f"Date: {s_date} != {q_date}")

            # 2. Amount Check
            s_amt = abs(self._safe_float(self._row_get_scalar(row, "Expense Line Amount", 0)))
            q_amt = float(qbo_record.get("TotalAmt", 0))
            if abs(s_amt - q_amt) > 0.01:
                errors.append(f"Amount: {s_amt:,.2f} != {q_amt:,.2f}")

            # 3. PAYMENT ACCOUNT CHECK (Account Cr)
            # This checks the main 'AccountRef' on the Purchase object (where money came FROM)
            sheet_pay_acc = str(self._row_get_scalar(row, "Account (Cr)", ""))
            qbo_pay_acc = qbo_record.get("AccountRef", {}).get("name", "")
            
            # Use robust match
            if not self._is_account_match(sheet_pay_acc, qbo_pay_acc):
                 errors.append(f"Payment Account Mismatch: '{sheet_pay_acc}' != '{self._normalize_account(qbo_pay_acc)}'")

            status = "✅ Matched" if not errors else "⚠️ " + "; ".join(errors)
            updates.append({"row_idx": idx, "status": status})

        return updates

    # --- 3. TRANSFERS ---
    def reconcile_transfers(self, df: pd.DataFrame, month_str: str) -> list[dict]:
        if df.empty or "Ref No" not in df.columns: return []
        start, end = self._get_month_range(month_str)
        if not start: return []

        qbo_list = self._fetch_transfers_list(start, end)
        map_id = {str(item["Id"]): item for item in qbo_list if "Id" in item}
        updates = []

        for idx, row in df.iterrows():
            qbo_record = None
            qbo_id_val = self._row_get_scalar(row, "QBO ID", "")
            if self._has_value(qbo_id_val):
                qbo_record = map_id.get(self._normalize_qbo_id(qbo_id_val))
            
            if not qbo_record:
                ref_no = str(self._row_get_scalar(row, "Ref No", "")).strip()
                qbo_record = next((item for item in qbo_list if ref_no in item.get("PrivateNote", "")), None)

            if not qbo_record:
                updates.append({"row_idx": idx, "status": "❌ Not found in QBO"})
                continue

            errors = []
            s_amt = abs(self._safe_float(self._row_get_scalar(row, "Transfer Amount", 0)))
            q_amt = float(qbo_record.get("Amount", 0))
            
            if abs(s_amt - q_amt) > 0.01:
                errors.append(f"Amount: {s_amt:,.2f} != {q_amt:,.2f}")
            
            s_date = pd.to_datetime(self._row_get_scalar(row, "Date", "")).strftime("%Y-%m-%d")
            q_date = qbo_record.get("TxnDate")
            if s_date != q_date:
                errors.append(f"Date: {s_date} != {q_date}")

            status = "✅ Matched" if not errors else "⚠️ " + "; ".join(errors)
            updates.append({"row_idx": idx, "status": status})

        return updates

    # --- 4. RAW vs TRANSFORM RECONCILE (FIXED) ---
    def reconcile_raw_vs_transform(
        self, raw_df: pd.DataFrame, transform_df: pd.DataFrame, entity_type: str, client_name: str = ""
    ) -> list[dict]:
        updates = []
        if raw_df.empty or "No" not in raw_df.columns: 
            logger.warning("⚠️ Raw Comparison Skipped: Empty DataFrame or missing 'No' column")
            return []
        
        if transform_df.empty or "No" not in transform_df.columns:
            logger.warning("Raw Comparison Skipped: Transform DataFrame missing 'No' column")
            return []

        # 1. Clean & Index Raw
        raw_clean = raw_df.copy()
        raw_no_cols = raw_clean.loc[:, raw_clean.columns == "No"]
        if raw_no_cols.empty:
            logger.warning("Raw Comparison Skipped: Raw DataFrame has no usable 'No' column")
            return []
        raw_clean["_Key"] = pd.to_numeric(raw_no_cols.iloc[:, 0], errors="coerce").fillna(0).astype(int)
        raw_clean = raw_clean[raw_clean["_Key"] > 0].set_index("_Key")

        # 2. Group Transform (Aggregate Max Abs Value)
        transform_df = transform_df.copy()
        transform_no_cols = transform_df.loc[:, transform_df.columns == "No"]
        if transform_no_cols.empty:
            logger.warning("Raw Comparison Skipped: Transform DataFrame has no usable 'No' column")
            return []
        transform_df["_Key"] = pd.to_numeric(transform_no_cols.iloc[:, 0], errors="coerce").fillna(0).astype(int)
        
        # --- FIX: Restore Dynamic Column Selection for Transform DF ---
        # The Transform DataFrame (output of transformer.py) does NOT have "USD - QBO".
        # It has "Amount", "Expense Line Amount", or "Transfer Amount".
        amt_col = "Amount"
        if entity_type == "Purchase": amt_col = "Expense Line Amount"
        elif entity_type == "Transfer": amt_col = "Transfer Amount"
        
        if amt_col not in transform_df.columns:
            logger.warning(f"Raw Comparison Skipped: missing amount column '{amt_col}' for {entity_type}")
            return []

        amt_candidates = transform_df.loc[:, transform_df.columns == amt_col]
        if amt_candidates.empty:
            logger.warning(f"Raw Comparison Skipped: no usable '{amt_col}' column for {entity_type}")
            return []

        if amt_candidates.shape[1] == 1:
            recon_amt = amt_candidates.iloc[:, 0].apply(self._safe_float).abs()
        else:
            recon_amt = amt_candidates.apply(lambda col: col.apply(self._safe_float)).abs().max(axis=1)

        transform_df["_ReconAmt"] = recon_amt
        transform_agg = transform_df.groupby("_Key")["_ReconAmt"].max()

        # 3. Compare & Assign Status
        status_map = {}
        mismatch_count = 0

        for no_val, sheet_amt in transform_agg.items():
            if no_val not in raw_clean.index:
                status_map[no_val] = "Unmatched: Missing in Raw"
                continue

            raw_row = raw_clean.loc[no_val]

            if isinstance(raw_row, pd.DataFrame):
                usd_cols = raw_row.loc[:, raw_row.columns == "USD - QBO"]
                if not usd_cols.empty:
                    flat_vals = pd.Series(usd_cols.to_numpy().ravel())
                    final_raw_val = flat_vals.apply(self._safe_float).abs().max() if not flat_vals.empty else 0
                else:
                    final_raw_val = 0
            else:
                # --- FIX: STRICTLY use "USD - QBO" from Raw ---
                final_raw_val = self._coerce_scalar(raw_row.get("USD - QBO", 0))

            raw_abs = abs(self._safe_float(final_raw_val))
            sheet_abs = abs(self._safe_float(sheet_amt))

            if abs(raw_abs - sheet_abs) > 0.05:
                # logger.info(f"   ❌ MISMATCH [No {no_val}]: Raw={raw_abs:,.2f} vs Sheet={sheet_abs:,.2f}")
                status_map[no_val] = f"Unmatched: Amt Diff ({raw_abs:,.2f} vs {sheet_abs:,.2f})"
                mismatch_count += 1
            else:
                status_map[no_val] = "Matched"

        # if mismatch_count > 0:
        #     logger.info(f"   ⚠️ Found {mismatch_count} value mismatches in {entity_type}")

        # 3b. KZO ONLY: estimate the shifted 'No' for Unmatched rows.
        # KZO's raw 'No' is derived from row position (date + running count of same-date
        # rows), so inserting a row anywhere in the raw tab shifts every later same-date
        # 'No'. The transform row's own Date/Amount are ground truth (captured before any
        # later shift), so we search current raw for an unclaimed row with that same
        # content instead of trying to reverse the 'No' formula.
        client_name_lower = str(client_name).strip().lower()
        is_kzo = bool(client_name_lower) and not (
            any(x in client_name_lower for x in ("kzp", "s5", "umber"))
            or settings.is_kzdw_family(client_name)
        )
        if is_kzo:
            # A shift is usually just a row or two inserted/deleted near the affected 'No',
            # so when exact content matching can't place it, look at nearby raw Nos too
            # (mirrors how an analyst would manually check the neighborhood by hand).
            NEARBY_NO_WINDOW = 10

            transform_date_col = "Payment Date" if entity_type == "Purchase" else "Date"
            transform_date_by_key: dict[int, str] = {}
            if transform_date_col in transform_df.columns:
                date_series = transform_df.loc[:, transform_df.columns == transform_date_col]
                if not date_series.empty:
                    norm_dates = date_series.iloc[:, 0].apply(self._normalize_date_str)
                    transform_date_by_key = norm_dates.groupby(transform_df["_Key"]).first().to_dict()

            claimed_nos = {k for k, v in status_map.items() if v == "Matched"}

            raw_lookup = raw_clean.reset_index()
            raw_lookup["_DateNorm"] = raw_lookup.apply(
                lambda r: self._normalize_date_str(self._row_get_scalar(r, "Date", "")), axis=1
            )
            raw_lookup["_AmtAbs"] = raw_lookup.apply(
                lambda r: abs(self._safe_float(self._row_get_scalar(r, "USD - QBO", 0))), axis=1
            )

            for no_val, status in list(status_map.items()):
                if not status.startswith("Unmatched"):
                    continue
                sheet_abs = abs(self._safe_float(transform_agg.get(no_val, 0)))
                t_date = transform_date_by_key.get(no_val, "")

                candidates: list[int] = []
                if t_date:
                    pool = raw_lookup[
                        (~raw_lookup["_Key"].isin(claimed_nos))
                        & (raw_lookup["_DateNorm"] == t_date)
                        & ((raw_lookup["_AmtAbs"] - sheet_abs).abs() <= 0.05)
                    ]
                    candidates = sorted(set(int(x) for x in pool["_Key"].tolist()))

                approx = False
                if not candidates:
                    # Fallback: same 'No' neighborhood, amount-only match. Covers rows
                    # whose raw Date is blank/unparseable, or a date that just doesn't
                    # line up (source data isn't always clean).
                    nearby_pool = raw_lookup[
                        (~raw_lookup["_Key"].isin(claimed_nos))
                        & (raw_lookup["_Key"] != no_val)
                        & ((raw_lookup["_Key"] - no_val).abs() <= NEARBY_NO_WINDOW)
                        & ((raw_lookup["_AmtAbs"] - sheet_abs).abs() <= 0.05)
                    ]
                    candidates = sorted(set(int(x) for x in nearby_pool["_Key"].tolist()))
                    approx = bool(candidates)

                suffix = " (nearby match, verify)" if approx else ""
                if len(candidates) == 0:
                    status_map[no_val] = f"{status} -> est. No: not found (row may be deleted)"
                elif len(candidates) == 1:
                    status_map[no_val] = f"{status} -> est. No: {candidates[0]}{suffix}"
                else:
                    # Multiple content matches on the same day: a shift only ever moves a
                    # row by a handful of positions, so whichever candidate sits closest to
                    # the old No is overwhelmingly likely to be the right one. Only fall
                    # back to "ambiguous" when two candidates are tied on that distance.
                    min_dist = min(abs(c - no_val) for c in candidates)
                    closest = [c for c in candidates if abs(c - no_val) == min_dist]
                    if len(closest) == 1:
                        status_map[no_val] = f"{status} -> est. No: {closest[0]}{suffix}"
                    else:
                        preview = ", ".join(str(c) for c in candidates[:5])
                        status_map[no_val] = f"{status} -> est. No: ambiguous ({preview}){suffix}"

        # 4. Broadcast
        for idx, row in transform_df.iterrows():
            no_val = int(self._safe_float(self._row_get_scalar(row, "_Key", 0)))
            if no_val == 0: continue
            status = status_map.get(no_val, "Skipped")
            updates.append({"row_idx": idx, "status": status})

        return updates

