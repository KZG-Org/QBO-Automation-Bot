from __future__ import annotations
import pandas as pd
import re
import difflib
from datetime import datetime
from config import settings
from src.utils.logger import setup_logger
from src.connectors.qbo_client import QBOClient
from src.logic.account_aliases import resolve_account_alias

from dotenv import load_dotenv
load_dotenv("config/secrets.env")

logger = setup_logger("syncing_logic")

def _parse_date_yyyy_mm_dd(val) -> str:
    if pd.isna(val) or val == "":
        return datetime.today().strftime("%Y-%m-%d")
    try:
        dt = pd.to_datetime(val)
        return dt.strftime("%Y-%m-%d")
    except:
        return datetime.today().strftime("%Y-%m-%d")

def _parse_amount(val) -> float:
    try:
        return float(pd.to_numeric(val))
    except:
        return 0.0

def _parse_exchange_rate(val) -> float | None:
    if pd.isna(val) or str(val).strip() == "":
        return None
    text = str(val).replace(",", "").strip()
    parsed = pd.to_numeric(text, errors="coerce")
    if pd.isna(parsed):
        return None
    rate = float(parsed)
    if rate <= 0:
        return None
    return rate

def _normalize_currency_code(val) -> str:
    text = str(val).strip().upper()
    if not text:
        return "USD"
    # Keep common 3-letter ISO codes; map variants containing USD to USD.
    if "USD" in text:
        return "USD"
    return text[:3]

def _infer_currency_from_text(val) -> str | None:
    text = str(val or "").upper()
    if not text:
        return None

    # Exact/contains ISO code cues first.
    direct_codes = [
        "USD", "THB", "VND", "IDR", "SGD", "MYR", "PHP",
        "EUR", "GBP", "AUD", "JPY", "CNY", "HKD", "KRW", "INR", "AED"
    ]
    for code in direct_codes:
        if re.search(rf"\b{code}\b", text):
            return code

    # Common workspace shorthand in account names (e.g. "... TH 2'").
    shorthand_map = {
        " TH ": "THB",
        " VN ": "VND",
        " ID ": "IDR",
        " SG ": "SGD",
        " MY ": "MYR",
        " PH ": "PHP",
        " UK ": "GBP",
        " EU ": "EUR",
        " AU ": "AUD",
        " JP ": "JPY",
        " CN ": "CNY",
        " HK ": "HKD",
        " KR ": "KRW",
        " IN ": "INR",
        " AE ": "AED",
    }
    padded = f" {text} "
    for token, code in shorthand_map.items():
        if token in padded:
            return code

    return None


def _is_kzdw_workspace(client_name: str | None) -> bool:
    # TINDERPAY posts KZDW-style multicurrency transfers from its own QBO company.
    return settings.is_kzdw_family(client_name)

class QBOSync:
    def __init__(self, client: QBOClient):
        self.client = client
        self.mappings = self._get_qbo_mappings()

    def build_qbo_url(self, entity: str, txn_id: str) -> str:
        """
        Returns a direct QuickBooks URL for a transaction.
        """
        if not txn_id: return ""
        routes = {"Purchase": "expense", "JournalEntry": "journal", "Transfer": "transfer"}
        page = routes.get(entity)
        if not page: return ""
        return f"https://qbo.intuit.com/app/{page}?txnId={txn_id}"

    def _attach_exchange_rate_if_needed(
        self,
        payload: dict,
        txn_currency: str,
        txn_date: str,
        context: str,
        transformed_rate=None,
    ) -> None:
        """
        Attach ExchangeRate for foreign-currency transactions.
        Priority:
        1) transformed Currency Exchange value (when valid)
        2) fallback to QBO historical FX lookup
        """
        ccy = _normalize_currency_code(txn_currency)
        if ccy == "USD":
            return
        transformed_fx = _parse_exchange_rate(transformed_rate)
        if transformed_fx is not None:
            payload["ExchangeRate"] = transformed_fx
            return
        fx_rate = self.client.get_exchange_rate(
            source_currency_code=ccy,
            as_of_date=txn_date,
            target_currency_code="USD",
        )
        if fx_rate is None:
            raise ValueError(
                f"Missing FX rate for {ccy}->USD on {txn_date} ({context}); "
                "skipped to avoid 1:1 exchange posting."
            )
        payload["ExchangeRate"] = fx_rate

    def _get_qbo_mappings(self) -> dict:
        """Fetches Accounts, Locations, Classes, Vendors, and Payment Methods."""
        logger.info(f"🔍 Fetching QBO Mappings for Realm: {self.client.realm_id}...")
        mappings = {
            "accounts": {},
            "accounts_meta": {},
            "locations": {},
            "classes": {},
            "vendors": {},
            "payment_methods": {}
        }
        
        entities = [
            ("Account", "accounts", "Name, FullyQualifiedName, Id, CurrencyRef"),
            ("Department", "locations", "Name, FullyQualifiedName, Id"), 
            ("Class", "classes", "Name, FullyQualifiedName, Id"),
            ("Vendor", "vendors", "DisplayName, Id"),
            ("PaymentMethod", "payment_methods", "Name, Id") 
        ]

        for table, key, fields in entities:
            try:
                data = self.client.query(f"SELECT {fields} FROM {table} MAXRESULTS 1000")
                for item in data:
                    name = item.get("FullyQualifiedName", item.get("Name", item.get("DisplayName")))
                    mappings[key][name] = item["Id"]
                    if table == "Account":
                        acc_currency = _normalize_currency_code(
                            (item.get("CurrencyRef") or {}).get("value", "USD")
                        )
                        mappings["accounts_meta"][item["Id"]] = {"currency": acc_currency}
            except Exception as e:
                logger.error(f"❌ Failed to fetch {table}: {e}")

        return mappings

    # --- UPDATED FIND ID LOGIC (MATCHES TRANSFORMER.PY) ---
    def find_id(self, mapping_key: str, search_name: str, warn_on_missing: bool = True) -> str | None:
        if not search_name or pd.isna(search_name) or str(search_name).strip() == "": return None
        
        mapping_dict = self.mappings.get(mapping_key, {})
        if mapping_key == "accounts":
            # Normalizes ':' spacing and applies the workspace's alias table (KZP's
            # duplicated 'Growth' leaf) -- same rewrite the transform stage did, so a row
            # marked 'Ready to sync' resolves to the same account here.
            clean_name = resolve_account_alias(search_name, getattr(self.client, "client_name", ""))
        else:
            clean_name = re.sub(r'\s+', ' ', str(search_name)).strip()
        
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

        search_lower = clean_name.lower()

        # 1. EXACT MATCH
        for name, qbo_id in mapping_dict.items():
            if name.lower() == search_lower: 
                # logger.info(f"      ✅ [Sync Map] EXACT: '{search_name}' -> '{name}'")
                return qbo_id
        
        # 2. SUFFIX PATH MATCH (Split by :)
        # A sheet value may drop any number of leading parent levels, so compare against every
        # trailing path of the QBO name, not just the final segment:
        #   "Marketing:RnD:AI Expenses" -> "RnD:AI Expenses" and "AI Expenses"
        #   "Fixed Assets:Equipment"    -> "Equipment"
        suffix_hits: dict[str, str] = {}
        for name, qbo_id in mapping_dict.items():
            if ":" not in name:
                continue
            parts = [p.strip() for p in name.split(":")]
            for i in range(1, len(parts)):
                if ":".join(parts[i:]).lower() == search_lower:
                    suffix_hits.setdefault(qbo_id, name)
                    break

        if len(suffix_hits) == 1:
            qbo_id, name = next(iter(suffix_hits.items()))
            logger.info(f"      ✅ [Sync Map] LEAF: '{search_name}' -> '{name}'")
            return qbo_id
        if len(suffix_hits) > 1:
            # Same name under two parents -- guessing is the same class of error as a fuzzy
            # sibling match, so accounts refuse rather than pick one.
            if mapping_key == "accounts":
                logger.error(
                    f"      ❌ [Sync Map] AMBIGUOUS: '{search_name}' matches {sorted(suffix_hits.values())}"
                )
                return None
            return next(iter(suffix_hits))

        # 3. FUZZY MATCH (80%) -- NOT for accounts.
        # Accounts are money destinations, and real charts of accounts contain siblings that
        # differ only by a short code: "Investment in HR Company (ORZ)" vs "(OSR)" scores 0.82
        # against the fully-qualified names, so fuzzy would silently post to the wrong
        # account. Accounts must match exactly or by leaf name; anything else fails loudly and
        # is surfaced as an ERROR remark / raised ValueError for a human to fix in the sheet.
        if mapping_key != "accounts":
            matches = difflib.get_close_matches(clean_name, list(mapping_dict.keys()), n=1, cutoff=0.80)
            if matches:
                best = matches[0]
                logger.info(f"      ✨ [Sync Map] FUZZY (80%): '{search_name}' -> '{best}'")
                return mapping_dict[best]

        if warn_on_missing:
            near = difflib.get_close_matches(clean_name, list(mapping_dict.keys()), n=3, cutoff=0.60)
            hint = f" Closest QBO names: {near}" if near else ""
            logger.warning(
                f"      ❌ [Sync Map] FAILED: Could not find '{search_name}' in {mapping_key}.{hint}"
            )
        return None

    def get_journal_totals(self, doc_nums: list) -> dict[str, list[tuple[str, float]]]:
        """{DocNumber: [(TxnDate, TotalAmt), ...]} for journals already in QBO.

        A list, because QBO allows two journals with one DocNumber. A chunk whose query fails is
        left out (logged), so callers treat those numbers as unknown.
        """
        found: dict[str, list[tuple[str, float]]] = {}
        clean_docs = sorted({str(d).strip() for d in doc_nums if str(d).strip()})
        for i in range(0, len(clean_docs), 50):
            chunk = [d.replace("'", "\\'") for d in clean_docs[i:i + 50]]
            formatted_list = "', '".join(chunk)
            query = (
                "SELECT DocNumber, TxnDate, TotalAmt FROM JournalEntry "
                f"WHERE DocNumber IN ('{formatted_list}')"
            )
            try:
                for item in self.client.query(query):
                    found.setdefault(str(item.get("DocNumber")), []).append(
                        (str(item.get("TxnDate", "")), float(item.get("TotalAmt", 0) or 0))
                    )
            except Exception as e:
                logger.error(f"⚠️ Failed journal detail lookup: {e}")
        return found

    def get_existing_duplicates(
        self,
        entity_type: str,
        doc_nums: list,
        date_start: str | None = None,
        date_end: str | None = None,
    ) -> set:
        """
        Queries QBO to see which IDs already exist.

        date_start/date_end ('YYYY-MM-DD') scope the Transfer lookup to one period; they are
        ignored for JournalEntry/Purchase, which are already scoped by DocNumber.
        """
        if not doc_nums: return set()
        existing = set()
        clean_docs = list(set([str(d).strip() for d in doc_nums if str(d).strip()]))
        
        if entity_type in ["JournalEntry", "Purchase"]:
            chunk_size = 50 
            for i in range(0, len(clean_docs), chunk_size):
                chunk = clean_docs[i:i+chunk_size]
                safe_chunk = [d.replace("'", "\\'") for d in chunk]
                formatted_list = "', '".join(safe_chunk)
                
                query = f"SELECT DocNumber FROM {entity_type} WHERE DocNumber IN ('{formatted_list}')"
                try:
                    results = self.client.query(query)
                    for item in results:
                        existing.add(item.get("DocNumber"))
                except Exception as e:
                    logger.error(f"⚠️ Failed duplicate check {entity_type}: {e}")

        elif entity_type == "Transfer":
            # Transfers carry their doc ref inside PrivateNote, and QBO cannot filter on that
            # field (a `PrivateNote LIKE ...` query is rejected with 400), so the notes have to
            # be pulled down and scanned locally. Scope that pull by TxnDate: without it the
            # paginating client walks the entire Transfer table -- measured at 14,064 rows /
            # 320s for KZO, versus 1,498 rows / 36s for a single month. The old
            # "MAXRESULTS 500" was silently ineffective because query() appends its own
            # STARTPOSITION/MAXRESULTS and keeps paging until a short page.
            try:
                query = "SELECT PrivateNote FROM Transfer"
                if date_start and date_end:
                    query += f" WHERE TxnDate >= '{date_start}' AND TxnDate <= '{date_end}'"
                else:
                    logger.warning(
                        "⚠️ Transfer duplicate check running unscoped; this pulls the whole "
                        "Transfer history and is slow."
                    )
                results = self.client.query(query)
                logger.info(
                    f"   🔍 Transfer duplicate check scanned {len(results)} note(s)"
                    + (f" [{date_start} to {date_end}]" if date_start and date_end else " [ALL]")
                )
                qbo_notes = [str(item.get("PrivateNote", "")) for item in results]

                for doc_ref in clean_docs:
                    if any(doc_ref in note for note in qbo_notes):
                        existing.add(doc_ref)
            except Exception as e:
                logger.error(f"⚠️ Failed duplicate check Transfer: {e}")

        return existing

    def push_journal(self, journal_no: str, group: pd.DataFrame):
        first_row = group.iloc[0]
        line_items = []
        for _, row in group.iterrows():
            amt = _parse_amount(row['Amount'])
            
            # Use updated find_id logic
            acc_id = self.find_id('accounts', row['Account'])
            if not acc_id: raise ValueError(f"Account '{row['Account']}' not found.")
            
            entity_ref = None
            if row.get('Name'):
                ven_id = self.find_id('vendors', row['Name'], warn_on_missing=False)
                if ven_id: entity_ref = {"Type": "Vendor", "EntityRef": {"value": ven_id}}

            loc_id = self.find_id('locations', row.get('Location'))
            class_id = self.find_id('classes', row.get('Class'), warn_on_missing=False)
            line_detail = {
                "PostingType": "Debit" if amt > 0 else "Credit",
                "AccountRef": {"value": acc_id},
            }
            if loc_id:
                line_detail["DepartmentRef"] = {"value": loc_id}
            if class_id:
                line_detail["ClassRef"] = {"value": class_id}
            if entity_ref: line_detail["Entity"] = entity_ref

            line_items.append({
                "Description": str(row.get('Memo') or ""),
                "Amount": abs(amt),
                "DetailType": "JournalEntryLineDetail",
                "JournalEntryLineDetail": line_detail
            })

        txn_date = _parse_date_yyyy_mm_dd(first_row.get('Date'))
        txn_currency = _normalize_currency_code(first_row.get('Currency Code', 'USD'))
        payload = {
            "Line": line_items,
            "DocNumber": str(journal_no),
            "TxnDate": txn_date,
            "PrivateNote": str(first_row.get('Memo', '')),
            "CurrencyRef": {"value": txn_currency}
        }
        self._attach_exchange_rate_if_needed(
            payload,
            txn_currency,
            txn_date,
            f"Journal {journal_no}",
            transformed_rate=first_row.get("Currency Exchange"),
        )
        return self.client.post(f"/v3/company/{self.client.realm_id}/journalentry", payload)

    def push_expense(self, exp_ref_no: str, row: pd.Series):
        pay_acc_id = self.find_id("accounts", row.get("Account (Cr)"))
        exp_acc_id = self.find_id("accounts", row.get("Expense Account (Dr)"))
        
        if not pay_acc_id: raise ValueError(f"Payment Account '{row.get('Account (Cr)')}' missing.")
        if not exp_acc_id: raise ValueError(f"Expense Account '{row.get('Expense Account (Dr)')}' missing.")
        
        payee = str(row.get("Payee (Dummy)") or "Dummy")
        vendor_id = self.find_id("vendors", payee, warn_on_missing=False)
        entity_ref = {'value': vendor_id, 'name': payee, 'type': 'Vendor'} if vendor_id else None

        loc_id = self.find_id('locations', row.get('Location'))
        class_id = self.find_id('classes', row.get('Class'), warn_on_missing=False)
        
        pm_name = row.get("Payment Method") 
        pm_id = self.find_id("payment_methods", pm_name)

        txn_date = _parse_date_yyyy_mm_dd(row.get("Payment Date"))
        txn_currency = _normalize_currency_code(row.get("Currency", "USD"))
        payload = {
            "AccountRef": {"value": pay_acc_id},
            "PaymentType": "Cash",
            "DocNumber": str(exp_ref_no),
            "TxnDate": txn_date,
            "CurrencyRef": {"value": txn_currency},
            "Line": [{
                "DetailType": "AccountBasedExpenseLineDetail",
                "Amount": abs(_parse_amount(row.get("Expense Line Amount"))),
                "AccountBasedExpenseLineDetail": {
                    "AccountRef": {"value": exp_acc_id},
                    **({"ClassRef": {"value": class_id}} if class_id else {})
                },
                "Description": str(row.get("Memo") or "")
            }]
        }

        if entity_ref:
            payload["EntityRef"] = entity_ref
        if loc_id: payload["DepartmentRef"] = {"value": loc_id}
        if pm_id: payload["PaymentMethodRef"] = {"value": pm_id}
        self._attach_exchange_rate_if_needed(
            payload,
            txn_currency,
            txn_date,
            f"Expense {exp_ref_no}",
            transformed_rate=row.get("Currency Exchange"),
        )

        return self.client.post(f"/v3/company/{self.client.realm_id}/purchase", payload)

    def push_transfer(self, row: pd.Series):
        from_name = row.get("Transfer Funds From")
        to_name = row.get("Transfer Funds To")

        from_id = self.find_id("accounts", from_name)
        to_id = self.find_id("accounts", to_name)
        
        if not from_id or not to_id: raise ValueError("Source or Destination Account missing.")

        # QBO rejects a transfer whose two sides land on one account with a bare 400. The
        # transform stage already blocks identical *names*, but two different sheet names can
        # still resolve to one QBO account (explicit replacement, or a leaf name matching a
        # sub-account), so the check has to run again on the resolved Ids.
        if from_id == to_id:
            raise ValueError(
                f"Transfer From and To resolve to the same QBO account "
                f"(Id {from_id}): '{from_name}' -> '{to_name}'. "
                "Money cannot move within one account; fix the From/To columns."
            )

        is_kzdw_workspace = _is_kzdw_workspace(self.client.client_name)
        row_ccy = _normalize_currency_code(row.get("Currency", "USD"))
        from_ccy = "USD"
        to_ccy = "USD"

        ref_no = str(row.get("Ref No", ""))
        memo = str(row.get("Memo", ""))
        # full_memo = f"{ref_no} - {memo}"

        txn_currency = "USD"
        if is_kzdw_workspace:
            accounts_meta = self.mappings.get("accounts_meta", {})
            from_ccy = _normalize_currency_code((accounts_meta.get(from_id) or {}).get("currency", "USD"))
            to_ccy = _normalize_currency_code((accounts_meta.get(to_id) or {}).get("currency", "USD"))

            # Fallback for cases where Account query did not return reliable CurrencyRef.
            inferred_from_ccy = _infer_currency_from_text(from_name)
            inferred_to_ccy = _infer_currency_from_text(to_name)
            if from_ccy == "USD" and inferred_from_ccy and inferred_from_ccy != "USD":
                from_ccy = inferred_from_ccy
            if to_ccy == "USD" and inferred_to_ccy and inferred_to_ccy != "USD":
                to_ccy = inferred_to_ccy

            if from_ccy == to_ccy:
                txn_currency = from_ccy
            elif from_ccy == "USD" and to_ccy != "USD":
                txn_currency = to_ccy
            elif to_ccy == "USD" and from_ccy != "USD":
                txn_currency = from_ccy
            else:
                raise ValueError(
                    f"Transfer currency conflict ({from_ccy} -> {to_ccy}). "
                    "QBO allows only one foreign currency per transfer."
                )

            # Respect sheet currency if it aligns with account currencies.
            if row_ccy == "USD":
                txn_currency = "USD"
            elif row_ccy in {from_ccy, to_ccy}:
                txn_currency = row_ccy

        elif row_ccy != "USD":
            logger.warning(
                f"      [Transfer Currency] Non-KZDW workspace '{self.client.client_name}' "
                f"received row currency '{row_ccy}'. Falling back to USD."
            )

        logger.info(
            f"      [Transfer Currency] Ref={row.get('Ref No','')} "
            f"Workspace={self.client.client_name} "
            f"From={from_ccy} To={to_ccy} Row={row_ccy} -> Txn={txn_currency}"
        )

        payload = {
            "TxnDate": _parse_date_yyyy_mm_dd(row.get("Date")),
            "Amount": abs(_parse_amount(row.get("Transfer Amount"))),
            "FromAccountRef": {"value": from_id},
            "ToAccountRef": {"value": to_id},
            "PrivateNote": memo 
        }
        # Only KZDW handles non-USD transfer + FX attachment.
        if is_kzdw_workspace and txn_currency != "USD":
            payload["CurrencyRef"] = {"value": txn_currency}
            txn_date = payload["TxnDate"]
            transformed_fx = _parse_exchange_rate(row.get("Currency Exchange"))
            if transformed_fx is not None:
                payload["ExchangeRate"] = transformed_fx
            else:
                fx_rate = self.client.get_exchange_rate(
                    source_currency_code=txn_currency,
                    as_of_date=txn_date,
                    target_currency_code="USD",
                )
                if fx_rate is None:
                    raise ValueError(
                        f"Missing FX rate for {txn_currency}->USD on {txn_date}; "
                        "skipped to avoid 1:1 exchange posting."
                    )
                payload["ExchangeRate"] = fx_rate
        return self.client.post(f"/v3/company/{self.client.realm_id}/transfer", payload)
