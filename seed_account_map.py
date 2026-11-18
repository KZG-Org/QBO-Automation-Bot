"""One-off: fill each client's 'Account Map' tab from its past Transform rows.

Design: docs/superpowers/specs/2026-11-18-account-map-design.md, section 5.6.

For every month a client's Control Sheet lists, every account name in its Transform tabs:
  - resolves today by the exact / suffix-path rule -> Auto row;
  - does not resolve today but has synced rows -> learn the account QBO actually posted to
    (up to 3 synced rows). All agree -> Auto row. They disagree -> listed as NOT SEEDED.

Prints the plan. Writes only with --write.

Usage (repo root on the server):
    venv/bin/python seed_account_map.py [--client NAME] [--write]
"""

from __future__ import annotations

import argparse
import time
from collections import defaultdict

from dotenv import load_dotenv

load_dotenv("config/secrets.env")

import pandas as pd  # noqa: E402

from config import settings  # noqa: E402
from src.connectors.gsheets_client import GSheetsClient  # noqa: E402
from src.connectors.qbo_client import QBOClient  # noqa: E402
from src.logic.account_map import (  # noqa: E402
    AccountResolver,
    fetch_qbo_accounts,
    map_key,
    normalize_qbo_id,
    read_account_map,
    write_account_map_changes,
)
from src.logic.month_end import is_month_end_row  # noqa: E402

TAB_COLUMNS = {
    "Journals": ["Account"],
    "Expenses": ["Account (Cr)", "Expense Account (Dr)"],
    "Transfers": ["Transfer Funds From", "Transfer Funds To"],
}
QBO_ENTITY = {"Journals": "journalentry", "Expenses": "purchase", "Transfers": "transfer"}
SAMPLES_PER_NAME = 3
SHEETS_PAUSE_SECONDS = 1.2  # stay under the Sheets read quota


def _ref_id(ref) -> str | None:
    return normalize_qbo_id((ref or {}).get("value")) or None


def learn_account_id(kind: str, column: str, txn: dict, sheet_amount: float | None) -> str | None:
    """The account QBO posted for this sheet column, or None when it cannot be told apart."""
    if kind == "Expenses" and column == "Account (Cr)":
        return _ref_id(txn.get("AccountRef"))
    if kind == "Expenses" and column == "Expense Account (Dr)":
        lines = [l for l in txn.get("Line", []) if l.get("DetailType") == "AccountBasedExpenseLineDetail"]
        if len(lines) != 1:
            return None
        return _ref_id(lines[0].get("AccountBasedExpenseLineDetail", {}).get("AccountRef"))
    if kind == "Transfers" and column == "Transfer Funds From":
        return _ref_id(txn.get("FromAccountRef"))
    if kind == "Transfers" and column == "Transfer Funds To":
        return _ref_id(txn.get("ToAccountRef"))
    if kind == "Journals" and column == "Account":
        if not sheet_amount:
            return None
        side = "Debit" if sheet_amount > 0 else "Credit"
        hits = [
            l for l in txn.get("Line", [])
            if l.get("JournalEntryLineDetail", {}).get("PostingType") == side
            and abs(float(l.get("Amount", 0) or 0) - abs(sheet_amount)) < 0.01
        ]
        if len(hits) != 1:
            return None
        return _ref_id(hits[0]["JournalEntryLineDetail"].get("AccountRef"))
    return None


def cell_text(value) -> str:
    """A sheet cell as trimmed text; empty for None / NaN / pd.NA."""
    if value is None:
        return ""
    try:
        if pd.isna(value):
            return ""
    except (TypeError, ValueError):
        pass
    text = str(value).strip()
    return "" if text.lower() in ("nan", "none") else text


def decide_learned(learned: list[str | None], active_ids: set[str]) -> tuple[str | None, str]:
    """(account Id to seed, '') or (None, why not).

    A row that told nothing (None: deleted in QBO, or 2 lines with the same amount) is not a
    disagreement. Two different Ids are.
    """
    if not learned:
        return None, "no synced rows"
    ids = sorted({i for i in learned if i})
    if not ids:
        return None, "no row told the account"
    if len(ids) > 1:
        return None, f"posted to {ids}"
    if ids[0] not in active_ids:
        return None, f"account {ids[0]} is not active in QBO"
    return ids[0], ""


def collect_unresolved(entries, resolver: AccountResolver, limit: int = SAMPLES_PER_NAME) -> dict:
    """Names that do not resolve today, grouped by Account Map key, with synced-row samples.

    `entries` yields (kind, column, name, QBO ID, amount). Spellings that differ only in case or
    spaces share one map row, so their samples are pooled and judged together (two spellings
    that posted to two accounts must not be seeded). Each spelling gets its own `limit`.
    Names that do resolve are left to the resolver, which records their Auto rows.
    """
    groups: dict[str, dict] = {}
    taken: dict[tuple[str, str], int] = defaultdict(int)
    for kind, col, raw_name, qbo_id, amount in entries:
        name = cell_text(raw_name)
        if not name or resolver.resolve(name).ok:
            continue
        key = map_key(name)
        group = groups.setdefault(key, {"name": name, "samples": []})
        qid = normalize_qbo_id(qbo_id)
        if qid and taken[(key, name)] < limit:
            taken[(key, name)] += 1
            group["samples"].append((kind, col, qid, amount))
    return groups


def _amount(value) -> float | None:
    try:
        return float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return None


def _read_tab(gs, url, tab) -> pd.DataFrame:
    time.sleep(SHEETS_PAUSE_SECONDS)
    try:
        return gs.read_as_df(url, tab)
    except Exception as e:  # noqa: BLE001
        print(f"   ! cannot read '{tab}': {e}")
        return pd.DataFrame()


def _transform_tabs(ctrl: pd.DataFrame):
    """(Transform File, tab name, kind) for every control row with a file and a month."""
    seen = set()
    for _, r in ctrl.iterrows():
        url = str(r.get(settings.CTRL_COL_TRANSFORM_URL, "")).strip()
        month = str(r.get(settings.CTRL_COL_MONTH, "")).strip()
        country = str(r.get(settings.CTRL_COL_COUNTRY, "")).strip()
        if not url or not month or not country:
            continue
        try:
            label = pd.to_datetime(month).strftime("%b %y")
        except (ValueError, TypeError):
            continue
        if is_month_end_row(country, r.get(settings.CTRL_COL_TAB_NAME, "")):
            country = settings.MONTH_END_COUNTRY
        for kind in TAB_COLUMNS:
            tab = f"{country} {label} - {kind}"
            if (url, tab) not in seen:
                seen.add((url, tab))
                yield url, tab, kind


def seed_client(gs, qbo: QBOClient, client: str, realm: str, sheet_id: str, write: bool) -> None:
    print(f"\n=== {client}")
    qbo.set_company(realm)
    accounts = fetch_qbo_accounts(qbo)
    resolver = AccountResolver(accounts, client, realm, read_account_map(gs, sheet_id))
    ctrl = gs.read_as_df(sheet_id, settings.CONTROL_TAB_NAME)

    tabs_read = []

    def entries():
        for url, tab, kind in _transform_tabs(ctrl):
            df = _read_tab(gs, url, tab)
            if df.empty:
                continue
            tabs_read.append(tab)
            for _, row in df.iterrows():
                for col in TAB_COLUMNS[kind]:
                    yield kind, col, row.get(col, ""), row.get("QBO ID", ""), _amount(row.get("Amount"))

    groups = collect_unresolved(entries(), resolver)
    print(f"   tabs read: {len(tabs_read)}, names not resolving today: {len(groups)}")

    active_ids = set(accounts.values())
    not_seeded: dict[str, str] = {}
    for key in sorted(groups):
        name = groups[key]["name"]
        learned: list[str | None] = []
        for kind, col, qbo_id, amount in groups[key]["samples"]:
            try:
                data = qbo._get(f"/v3/company/{qbo.realm_id}/{QBO_ENTITY[kind]}/{qbo_id}"
                                f"?minorversion={settings.QBO_MINOR_VERSION}")
                txn = next((v for v in data.values() if isinstance(v, dict)), {})
                learned.append(learn_account_id(kind, col, txn, amount))
            except Exception as e:  # noqa: BLE001
                learned.append(None)
                print(f"   ! cannot read QBO {QBO_ENTITY[kind]} {qbo_id}: {str(e)[:120]}")
        acc_id, why = decide_learned(learned, active_ids)
        if acc_id:
            resolver.remember(name, acc_id)
        else:
            not_seeded[name] = why

    pending = resolver.pending_rows()
    for row in sorted(pending, key=lambda r: r.sheet_name.lower()):
        print(f"   {row.type:6} '{row.sheet_name}' -> '{row.qbo_account}' ({row.qbo_id})")
    for name, why in not_seeded.items():
        print(f"   NOT SEEDED: '{name}' ({why})")
    print(f"   rows to write: {len(pending)}")
    if write and pending:
        print(f"   written: {write_account_map_changes(gs, sheet_id, pending)}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Fill the Account Map tab from past Transform rows.")
    parser.add_argument("--client", default="", help="Only this client (Master Sheet 'Client Name').")
    parser.add_argument("--write", action="store_true", help="Write the rows (default: print only).")
    args = parser.parse_args()

    gs = GSheetsClient()
    qbo = QBOClient(gs_client=gs)
    master = gs.read_as_df(settings.MASTER_SHEET_ID, settings.MASTER_TAB_NAME)
    master.columns = [" ".join(str(c).replace("\n", " ").split()) for c in master.columns]
    for _, m in master.iterrows():
        if str(m.get(settings.MST_COL_STATUS, "")).strip().lower() != "active":
            continue
        client = str(m.get(settings.MST_COL_CLIENT, "")).strip()
        if args.client and settings.normalize_workspace_name(client) != settings.normalize_workspace_name(args.client):
            continue
        if not settings.is_allowed_workspace(client):
            continue
        realm = str(m.get(settings.MST_COL_REALM_ID, "")).strip()
        sheet_id = str(m.get(settings.MST_COL_SHEET_ID, "")).strip()
        if not realm or not sheet_id:
            continue
        try:
            seed_client(gs, qbo, client, realm, sheet_id, args.write)
        except Exception as e:  # noqa: BLE001
            print(f"   ! {client} failed: {e}")


if __name__ == "__main__":
    main()
