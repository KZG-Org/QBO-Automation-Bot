import unittest
from unittest.mock import MagicMock, patch

import gspread
import pandas as pd

import run_ingestion
import run_reconciliation
import run_syncing
from seed_account_map import cell_text, collect_unresolved, decide_learned, learn_account_id
from src.connectors.gsheets_client import GSheetsClient
from src.logic import month_end
from src.logic.syncing import QBOSync
from src.logic.transformer import process_expenses, process_transfers
from src.logic.account_map import (
    ACCOUNT_MAP_HEADERS,
    ACCOUNT_MAP_TAB,
    AccountMapError,
    AccountResolver,
    MapRow,
    fetch_qbo_accounts,
    normalize_qbo_id,
    parse_account_map,
    read_account_map,
    resolve_account,
    write_account_map_changes,
)

ACCOUNTS = {
    "Leading Card:Leading Card ACCT": "148",
    "Leading Card:Leading Card ACCT:Leading Card ACC - 3214": "28",
    "Cash On Hand:CBD Z 1": "210",
    "Professional Fees:Recruitment Fees": "155",
    "Retained Earning": "900",
    "Retained Earnings": "901",
    "Amount Owing by/(to) - KZO group": "910",
    "Amount Owing by/(to) KZO group": "911",
    "Marketing:Growth": "701",
    "Sales:Growth": "702",
}
NOW = "2026-11-18 10:00"


def _resolver(rows=None, client="KZO", accounts=ACCOUNTS):
    return AccountResolver(accounts, client, "R1", rows or [], now=lambda: NOW)


class AccountResolverTests(unittest.TestCase):
    def test_exact_name_resolves_and_is_remembered(self):
        r = _resolver()
        res = r.resolve("Leading Card:Leading Card ACCT")
        self.assertEqual((res.account_id, res.how), ("148", "exact"))
        [row] = r.pending_rows()
        self.assertEqual(
            (row.sheet_name, row.qbo_account, row.qbo_id, row.type, row.realm_id, row.updated_at),
            ("Leading Card:Leading Card ACCT", "Leading Card:Leading Card ACCT", "148", "Auto", "R1", NOW),
        )

    def test_suffix_name_resolves(self):
        res = _resolver().resolve("Leading Card ACC - 3214")
        self.assertEqual((res.account_id, res.how), ("28", "suffix"))

    def test_renamed_account_uses_the_auto_row(self):
        r = _resolver([MapRow("Leading Card ACCT - 3214", "old name", "28", "Auto", "R1")])
        res = r.resolve("Leading Card ACCT - 3214")
        self.assertEqual((res.account_id, res.how), ("28", "saved"))
        [row] = r.pending_rows()
        self.assertEqual(row.qbo_account, "Leading Card:Leading Card ACCT:Leading Card ACC - 3214")

    def test_exact_name_beats_the_auto_row(self):
        r = _resolver([MapRow("Leading Card ACCT", "x", "28", "Auto", "R1")])
        res = r.resolve("Leading Card ACCT")
        self.assertEqual((res.account_id, res.how), ("148", "suffix"))
        [row] = r.pending_rows()
        self.assertEqual((row.type, row.qbo_id), ("Auto", "148"))

    def test_manual_row_beats_the_exact_name(self):
        r = _resolver([MapRow("Leading Card ACCT", "Leading Card ACC - 3214", "", "Manual", "")])
        res = r.resolve("Leading Card ACCT")
        self.assertEqual((res.account_id, res.how), ("28", "manual"))
        [row] = r.pending_rows()
        self.assertEqual(
            (row.type, row.qbo_id, row.realm_id, row.qbo_account),
            ("Manual", "28", "R1", "Leading Card ACC - 3214"),
        )

    def test_manual_text_beats_its_saved_id(self):
        r = _resolver([MapRow("Foo", "Cash On Hand:CBD Z 1", "28", "Manual", "R1")])
        self.assertEqual(r.account_id("Foo"), "210")

    def test_manual_saved_id_used_when_text_is_gone(self):
        r = _resolver([MapRow("Foo", "Gone Account", "210", "Manual", "R1")])
        res = r.resolve("Foo")
        self.assertEqual((res.account_id, res.how), ("210", "manual"))
        self.assertEqual(r.pending_rows(), [])

    def test_broken_manual_row_is_an_error(self):
        res = _resolver([MapRow("Foo", "Gone Account", "999", "Manual", "R1")]).resolve("Foo")
        self.assertIsNone(res.account_id)
        self.assertEqual(res.how, "manual_broken")
        self.assertEqual(
            res.detail, "Account Map row for 'Foo' points to 'Gone Account', which is not in QBO"
        )

    def test_ambiguous_manual_text_uses_the_saved_id_only_if_it_is_one_of_them(self):
        # Review finding 2: a copied row kept QBO ID 210 (CBD Z 1); its text 'Growth' matches
        # 2 accounts. 210 is not one of them, so the row must stop, not post to 210.
        res = _resolver([MapRow("Foo", "Growth", "210", "Manual", "R1")]).resolve("Foo")
        self.assertEqual(res.how, "manual_broken")
        self.assertIn("matches more than one QBO account", res.detail)
        r = _resolver([MapRow("Foo", "Growth", "702", "Manual", "R1")])
        self.assertEqual(r.account_id("Foo"), "702")

    def test_blank_type_counts_as_manual(self):
        res = _resolver([MapRow("Foo", "Cash On Hand:CBD Z 1", type="")]).resolve("Foo")
        self.assertEqual((res.account_id, res.how), ("210", "manual"))

    def test_other_company_rows_are_ignored(self):
        res = _resolver([MapRow("CBD Z Card 1", "x", "210", "Auto", "R2")]).resolve("CBD Z Card 1")
        self.assertEqual(res.how, "not_found")

    def test_id_without_realm_is_ignored(self):
        res = _resolver([MapRow("CBD Z Card 1", "x", "210", "Auto", "")]).resolve("CBD Z Card 1")
        self.assertEqual(res.how, "not_found")

    def test_inactive_saved_id_is_not_found(self):
        res = _resolver([MapRow("Old Card", "x", "555", "Auto", "R1")]).resolve("Old Card")
        self.assertEqual(res.how, "not_found")

    def test_ambiguous_without_auto_row_is_an_error(self):
        res = _resolver().resolve("Growth")
        self.assertEqual(res.how, "ambiguous")
        self.assertIn("Marketing:Growth", res.detail)
        self.assertIn("Sales:Growth", res.detail)

    def test_ambiguous_with_auto_row_uses_it(self):
        r = _resolver([MapRow("Growth", "Sales:Growth", "702", "Auto", "R1")])
        self.assertEqual(r.account_id("Growth"), "702")

    def test_not_found_lists_closest_names(self):
        res = _resolver().resolve("Recruitment Fee")
        self.assertEqual(res.how, "not_found")
        self.assertEqual(
            res.detail,
            "Closest in QBO: 'Professional Fees:Recruitment Fees'. "
            "Fix the name or add it to the Account Map tab",
        )

    def test_near_identical_siblings_resolve_only_to_themselves(self):
        r = _resolver()
        self.assertEqual(r.account_id("Retained Earning"), "900")
        self.assertEqual(r.account_id("Retained Earnings"), "901")
        self.assertEqual(r.account_id("Amount Owing by/(to) - KZO group"), "910")
        self.assertEqual(r.account_id("Amount Owing by/(to) KZO group"), "911")

    def test_case_and_spaces_hit_the_map_row(self):
        r = _resolver([MapRow("Leading Card ACCT - 3214", "old", "28", "Auto", "R1")])
        self.assertEqual(r.account_id("  leading card  ACCT - 3214 "), "28")

    def test_typed_id_with_decimal_is_normalized(self):
        self.assertEqual(normalize_qbo_id("210.0"), "210")
        self.assertEqual(normalize_qbo_id(" 210 "), "210")
        r = _resolver([MapRow("Foo", "Gone", "210.0", "Manual", "R1")])
        self.assertEqual(r.account_id("Foo"), "210")

    def test_duplicate_rows_manual_wins(self):
        r = _resolver([
            MapRow("Foo", "x", "210", "Auto", "R1", row_num=2),
            MapRow("Foo", "Professional Fees:Recruitment Fees", "", "Manual", "", row_num=3),
        ])
        self.assertEqual(r.account_id("Foo"), "155")

    def test_kzp_alias_still_applies(self):
        r = _resolver(
            client="KZP",
            accounts={"Marketing Income:Growth": "5010", "Marketing Expense:Growth": "6010"},
        )
        self.assertEqual(r.account_id("Growth"), "5010")
        self.assertEqual(r.account_id("Growth - Marketing Expense"), "6010")

    def test_blank_name(self):
        r = _resolver()
        self.assertEqual(r.resolve("").how, "blank")
        self.assertEqual(r.resolve(float("nan")).how, "blank")

    def test_resolve_account_builds_and_caches_a_resolver(self):
        m = {"accounts": {"Cash On Hand:CBD Z 1": "210"}}
        self.assertEqual(resolve_account(m, "CBD Z 1").account_id, "210")
        self.assertIn("account_resolver", m)

    def test_remember_records_an_auto_row(self):
        r = _resolver()
        r.remember("S5A", "210")
        [row] = r.pending_rows()
        self.assertEqual(
            (row.sheet_name, row.type, row.qbo_id, row.qbo_account),
            ("S5A", "Auto", "210", "Cash On Hand:CBD Z 1"),
        )


HEADER = list(ACCOUNT_MAP_HEADERS)


class _FakeGs:
    """Stands in for GSheetsClient: serves the Account Map grid and records writes."""

    def __init__(self, grid):
        self.grid = grid
        self.reads = 0
        self.writes = []

    def read_tab_values(self, sheet_id, tab):
        assert tab == ACCOUNT_MAP_TAB
        self.reads += 1
        if isinstance(self.grid, Exception):
            raise self.grid
        return self.grid

    def write_values_raw(self, sheet_id, tab, updates, create_if_missing=False, freeze_rows=0):
        self.writes.append({"tab": tab, "updates": updates, "create": create_if_missing,
                            "freeze_rows": freeze_rows})


def _ranges(call):
    return [a1 for a1, _ in call["updates"]]


def _auto(name, acc_id="28", row_num=None):
    return MapRow(name, f"Q:{name}", acc_id, "Auto", "R1", "2026-11-18 10:00", row_num)


class AccountMapTabTests(unittest.TestCase):
    def test_parse_reads_columns_by_header_name(self):
        rows = parse_account_map([
            ["Note", "QBO ID", "Sheet Name", "Type", "QBO Account", "Realm ID", "Updated At"],
            ["hi", "28", "Leading Card ACCT - 3214", "Auto", "x", "R1", ""],
        ])
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0].sheet_name, rows[0].qbo_id, rows[0].row_num), ("Leading Card ACCT - 3214", "28", 2))
        self.assertFalse(rows[0].is_manual)

    def test_parse_skips_blank_sheet_names(self):
        rows = parse_account_map([HEADER, ["", "x", "1", "Auto", "R1", ""], ["A", "x", "1", "Auto", "R1", ""]])
        self.assertEqual([r.sheet_name for r in rows], ["A"])
        self.assertEqual(rows[0].row_num, 3)

    def test_missing_tab_reads_as_empty(self):
        self.assertEqual(read_account_map(_FakeGs(None), "sid"), [])

    def test_read_error_propagates(self):
        with self.assertRaises(RuntimeError):
            read_account_map(_FakeGs(RuntimeError("429 quota")), "sid")

    def test_no_changes_means_no_read_and_no_write(self):
        gs = _FakeGs([HEADER])
        self.assertEqual(write_account_map_changes(gs, "sid", []), 0)
        self.assertEqual((gs.reads, gs.writes), (0, []))

    def test_new_rows_go_below_the_last_row_in_one_call(self):
        gs = _FakeGs([HEADER, ["A", "", "", "", "", ""], ["B", "", "", "", "", ""], ["C", "", "", "", "", ""]])
        n = write_account_map_changes(gs, "sid", [_auto("D"), _auto("E")])
        self.assertEqual(n, 2)
        self.assertEqual(len(gs.writes), 1)
        ranges = _ranges(gs.writes[0])
        self.assertIn("A5", ranges)
        self.assertIn("F6", ranges)
        self.assertNotIn("A4", ranges)

    def test_rows_are_matched_by_name_after_a_sort(self):
        gs = _FakeGs([HEADER, ["B", "x", "1", "Auto", "R1", ""], ["A", "x", "1", "Auto", "R1", ""]])
        write_account_map_changes(gs, "sid", [_auto("B", row_num=3)])
        ranges = _ranges(gs.writes[0])
        self.assertIn("C2", ranges)
        self.assertNotIn("C3", ranges)

    def test_manual_change_writes_only_id_realm_and_date(self):
        gs = _FakeGs([HEADER, ["Foo", "Typed name", "", "Manual", "", ""]])
        change = MapRow("Foo", "Typed name", "28", "Manual", "R1", "2026-11-18 10:00", 2)
        write_account_map_changes(gs, "sid", [change])
        self.assertEqual(sorted(_ranges(gs.writes[0])), ["C2", "E2", "F2"])

    def test_auto_change_skips_a_row_the_team_made_manual(self):
        gs = _FakeGs([HEADER, ["Foo", "Typed name", "", "Manual", "", ""]])
        self.assertEqual(write_account_map_changes(gs, "sid", [_auto("Foo", row_num=2)]), 0)
        self.assertEqual(gs.writes, [])

    def test_missing_tab_is_created_with_headers(self):
        gs = _FakeGs(None)
        write_account_map_changes(gs, "sid", [_auto("A")])
        call = gs.writes[0]
        self.assertTrue(call["create"])
        self.assertEqual(call["updates"][0], ("A1", [HEADER]))
        self.assertIn("A2", _ranges(call))

    def test_sorted_tab_without_header_on_row_1_stops_the_read(self):
        # Review finding 1: 'Sort A to Z' with row 1 not frozen moved the header down. Reading it
        # as empty would ignore every Manual row and post to the account the team overrode.
        sorted_grid = [["Foo", "Leading Card ACC - 3214", "", "Manual", "", ""], HEADER]
        with self.assertRaises(AccountMapError):
            read_account_map(_FakeGs(sorted_grid), "sid")

    def test_title_row_above_the_header_stops_the_read(self):
        with self.assertRaises(AccountMapError):
            read_account_map(_FakeGs([["Account Map for KZO"], HEADER, ["A", "x", "1", "Auto", "R1", ""]]), "sid")

    def test_fully_empty_tab_reads_as_empty(self):
        self.assertEqual(read_account_map(_FakeGs([]), "sid"), [])
        self.assertEqual(read_account_map(_FakeGs([["", ""], [""]]), "sid"), [])

    def test_write_refuses_a_tab_without_header_on_row_1(self):
        gs = _FakeGs([["Foo", "x", "", "Manual", "", ""], HEADER])
        with self.assertRaises(AccountMapError):
            write_account_map_changes(gs, "sid", [_auto("New")])
        self.assertEqual(gs.writes, [])

    def test_writes_ask_for_the_header_row_to_be_frozen(self):
        gs = _FakeGs([HEADER])
        write_account_map_changes(gs, "sid", [_auto("A")])
        self.assertEqual(gs.writes[0]["freeze_rows"], 1)

    def test_missing_header_columns_are_added_to_the_right(self):
        gs = _FakeGs([["Sheet Name", "QBO Account", "QBO ID", "Type"], ["A", "x", "1", "Auto"]])
        write_account_map_changes(gs, "sid", [_auto("A", row_num=2)])
        updates = dict(gs.writes[0]["updates"])
        self.assertEqual(updates["E1"], [["Realm ID", "Updated At"]])
        self.assertEqual(updates["E2"], [["R1"]])


class WriteValuesRawTests(unittest.TestCase):
    def _gs(self, tab_exists=True):
        gs = GSheetsClient.__new__(GSheetsClient)  # skip OAuth
        sh = MagicMock()
        ws = MagicMock()
        if tab_exists:
            sh.worksheet.return_value = ws
        else:
            sh.worksheet.side_effect = gspread.exceptions.WorksheetNotFound("x")
            sh.add_worksheet.return_value = ws
        gs.open = MagicMock(return_value=sh)
        return gs, sh, ws

    def test_writes_every_range_raw_in_one_call(self):
        gs, _, ws = self._gs()
        gs.write_values_raw("sid", ACCOUNT_MAP_TAB, [("A2", [["=x"]]), ("C2", [["28"]])])
        ws.batch_update.assert_called_once_with(
            [{"range": "A2", "values": [["=x"]]}, {"range": "C2", "values": [["28"]]}],
            value_input_option="RAW",
        )

    def test_creates_the_tab_only_when_asked(self):
        gs, sh, ws = self._gs(tab_exists=False)
        gs.write_values_raw("sid", ACCOUNT_MAP_TAB, [("A1", [HEADER])], create_if_missing=True)
        sh.add_worksheet.assert_called_once()
        ws.batch_update.assert_called_once()

        gs2, _, _ = self._gs(tab_exists=False)
        with self.assertRaises(gspread.exceptions.WorksheetNotFound):
            gs2.write_values_raw("sid", ACCOUNT_MAP_TAB, [("A1", [HEADER])])

    def test_freezes_the_header_row_when_asked(self):
        gs, _, ws = self._gs()
        ws.frozen_row_count = 0
        gs.write_values_raw("sid", ACCOUNT_MAP_TAB, [("A2", [["x"]])], freeze_rows=1)
        ws.freeze.assert_called_once_with(rows=1)

        gs2, _, ws2 = self._gs()
        ws2.frozen_row_count = 1
        gs2.write_values_raw("sid", ACCOUNT_MAP_TAB, [("A2", [["x"]])], freeze_rows=1)
        ws2.freeze.assert_not_called()

    def test_no_updates_makes_no_call(self):
        gs, sh, _ = self._gs()
        gs.write_values_raw("sid", ACCOUNT_MAP_TAB, [])
        gs.open.assert_not_called()


RENAMED_ACCOUNTS = {
    "Leading Card:Leading Card ACCT:Leading Card ACC - 3214": "28",
    "Bank:KZO Bank TH": "88",
    "Expense:AI Expenses": "5",
}
RENAMED_ROW = MapRow("Leading Card ACCT - 3214", "old name", "28", "Auto", "R1")


def _mappings(rows):
    return {
        "accounts": RENAMED_ACCOUNTS,
        "locations": {},
        "account_resolver": AccountResolver(RENAMED_ACCOUNTS, "KZO", "R1", rows),
    }


def _raw_transfer(from_name, to_name):
    return pd.DataFrame([{
        "No": 1, "Date": "2026-07-15", "USD - QBO": 100.0, "QBO Method": "Transfer",
        "QBO Transfer Fr": from_name, "QBO Transfer To": to_name,
        "Item Description": "moving funds", "CO": "", "Type": "",
    }])


def _raw_expense(src, exp):
    return pd.DataFrame([{
        "No": 1, "Date": "2026-07-15", "USD - QBO": -50.0, "QBO Method": "Expense",
        "If Journal/Expense Method": src, "Type": exp, "Item Description": "fee", "CO": "",
    }])


class TransformIntegrationTests(unittest.TestCase):
    def test_renamed_account_passes_with_an_auto_row(self):
        out, _ = process_transfers(
            _raw_transfer("Leading Card ACCT - 3214", "KZO Bank TH"), country="TH", start_no=0,
            qbo_mappings=_mappings([RENAMED_ROW]), client_name="KZO",
        )
        self.assertEqual(out["Remarks"].iloc[0], "Ready to sync")

    def test_unknown_account_error_names_the_closest_and_the_tab(self):
        out, _ = process_expenses(
            _raw_expense("Leading Card ACCT - 3214", "AI Expenses"), country="TH", start_no=0,
            qbo_mappings=_mappings([]), client_name="KZO",
        )
        remark = out["Remarks"].iloc[0]
        self.assertTrue(
            remark.startswith("ERROR | Source Account not in QBO: 'Leading Card ACCT - 3214'. Closest in QBO: "),
            remark,
        )
        self.assertIn("add it to the Account Map tab", remark)
        self.assertNotIn("Available:", remark)

    def test_month_end_line_uses_the_auto_row(self):
        line = pd.Series({"Date": "2026-10-31", "Account": "Leading Card ACCT - 3214",
                          "Amount": "100", "Location": "", "Class": "", "Currency Code": "USD"})
        self.assertIsNone(month_end._line_error(line, "KZO", _mappings([RENAMED_ROW])))


class IngestionAccountMapTests(unittest.TestCase):
    CTRL = pd.DataFrame([{"Country": "TH", "Transform": "READY", "Last Journal No": "0",
                          "Pending Nos Note": ""}])

    def _run(self, read_map):
        gs = MagicMock()
        gs.read_as_df.return_value = self.CTRL.copy()
        sync = MagicMock()
        sync.mappings = {"accounts": {"Bank:KZO Bank TH": "88"}, "locations": {}}
        with patch.object(run_ingestion, "QBOSync", return_value=sync), \
             patch.object(run_ingestion, "read_account_map", side_effect=read_map), \
             patch.object(run_ingestion, "write_account_map_changes") as write, \
             patch.object(run_ingestion, "_batch_update_control", side_effect=RuntimeError("stop")) as ctrl:
            try:
                run_ingestion.process_client_control_sheet(gs, MagicMock(), "ctrl", "KZO", "R1")
            except RuntimeError:
                pass
        return sync, write, ctrl

    def test_the_map_is_saved_even_when_a_row_stops_the_run(self):
        sync, write, ctrl = self._run(lambda gs, sid: [])
        ctrl.assert_called()  # the loop started
        write.assert_called_once()
        self.assertIs(write.call_args[0][2].__class__, list)
        self.assertIsInstance(sync.mappings["account_resolver"], AccountResolver)

    def test_ingestion_stops_when_the_map_cannot_be_read(self):
        def boom(gs, sid):
            raise RuntimeError("429 quota")
        _, write, ctrl = self._run(boom)
        ctrl.assert_not_called()
        write.assert_not_called()


def _sync_engine(accounts, rows=None):
    client = MagicMock()
    client.client_name = "KZO"
    client.realm_id = "R1"
    with patch.object(QBOSync, "_get_qbo_mappings", return_value={}):
        sync = QBOSync(client)
    sync.mappings = {"accounts": accounts, "accounts_meta": {}}
    if rows is not None:
        sync.use_account_map(rows)
    return sync


class SyncIntegrationTests(unittest.TestCase):
    def test_sync_resolves_a_renamed_account_through_the_map(self):
        # Renamed in QBO after Transform: Transform saved the Auto row, Sync uses it.
        sync = _sync_engine(RENAMED_ACCOUNTS, [RENAMED_ROW])
        self.assertEqual(sync.find_id("accounts", "Leading Card ACCT - 3214"), "28")

    def test_cbd_z_card_is_no_longer_rewritten_at_sync(self):
        sync = _sync_engine({"Bank:KZO CBD Z": "900"}, [])
        self.assertIsNone(sync.find_id("accounts", "CBD Z Card"))

    def test_replacing_mappings_rebuilds_the_resolver(self):
        sync = _sync_engine({"Bank:A": "1"}, [])
        self.assertEqual(sync.find_id("accounts", "A"), "1")
        sync.mappings = {"accounts": {"Bank:B": "2"}, "accounts_meta": {}}
        self.assertIsNone(sync.find_id("accounts", "A"))
        self.assertEqual(sync.find_id("accounts", "B"), "2")

    def _run_sync(self, read_map):
        gs = MagicMock()
        gs.read_as_df.return_value = pd.DataFrame([{"Country": "TH", "QBO Sync": "SYNC NOW",
                                                    "Transform File": "url", "Month": "2026-11-01"}])
        engine = MagicMock()
        with patch.object(run_syncing, "QBOSync", return_value=engine), \
             patch.object(run_syncing, "read_account_map", side_effect=read_map), \
             patch("src.logic.account_map.write_account_map_changes") as write, \
             patch.object(run_syncing, "_batch_update_control", side_effect=RuntimeError("stop")) as ctrl:
            try:
                run_syncing.process_client_sync(gs, MagicMock(), "ctrl", "KZO", "R1")
            except RuntimeError:
                pass
        return engine, write, ctrl

    def test_sync_loads_the_map_and_never_writes_it(self):
        rows = [RENAMED_ROW]
        engine, write, ctrl = self._run_sync(lambda gs, sid: rows)
        engine.use_account_map.assert_called_once_with(rows)
        ctrl.assert_called()  # reached the control rows
        write.assert_not_called()

    def test_sync_stops_when_the_map_cannot_be_read(self):
        def boom(gs, sid):
            raise RuntimeError("429 quota")
        _, _, ctrl = self._run_sync(boom)
        ctrl.assert_not_called()


class ReconcileAccountMapTests(unittest.TestCase):
    def test_fetch_qbo_accounts_keys_by_full_name(self):
        client = MagicMock()
        client.query.return_value = [
            {"Id": 28, "Name": "Leading Card ACC - 3214",
             "FullyQualifiedName": "Leading Card:Leading Card ACCT:Leading Card ACC - 3214"},
            {"Id": "88", "Name": "KZO Bank TH"},
        ]
        self.assertEqual(
            fetch_qbo_accounts(client),
            {"Leading Card:Leading Card ACCT:Leading Card ACC - 3214": "28", "KZO Bank TH": "88"},
        )
        self.assertIn("FROM Account", client.query.call_args[0][0])

    def _run_reconcile(self, read_map):
        gs = MagicMock()
        gs.read_as_df.return_value = pd.DataFrame([{"Country": "TH", "QBO Reconcile": "RECONCILE NOW",
                                                    "Transform File": "url", "Month": "2026-11-01"}])
        with patch.object(run_reconciliation, "read_account_map", side_effect=read_map), \
             patch.object(run_reconciliation, "fetch_qbo_accounts", return_value={"Bank:A": "1"}), \
             patch.object(run_reconciliation, "Reconciler") as rec_cls, \
             patch.object(run_reconciliation, "_batch_update_control", side_effect=RuntimeError("stop")) as ctrl:
            try:
                run_reconciliation.process_client_reconcile(gs, MagicMock(), "ctrl", "KZO", "R1")
            except RuntimeError:
                pass
        return rec_cls, ctrl

    def test_reconcile_gets_a_resolver_with_the_map(self):
        rec_cls, ctrl = self._run_reconcile(lambda gs, sid: [RENAMED_ROW])
        resolver = rec_cls.call_args.kwargs["account_resolver"]
        self.assertIsInstance(resolver, AccountResolver)
        self.assertEqual(resolver.account_id("Leading Card ACCT - 3214"), None)  # 28 not in {"Bank:A"}
        self.assertEqual(resolver.account_id("A"), "1")
        ctrl.assert_called()

    def test_reconcile_stops_when_the_map_cannot_be_read(self):
        def boom(gs, sid):
            raise RuntimeError("429 quota")
        rec_cls, ctrl = self._run_reconcile(boom)
        rec_cls.assert_not_called()
        ctrl.assert_not_called()


def _jline(side, amount, acc_id):
    return {"Amount": amount, "DetailType": "JournalEntryLineDetail",
            "JournalEntryLineDetail": {"PostingType": side, "AccountRef": {"value": acc_id}}}


def _eline(acc_id):
    return {"Amount": 50, "DetailType": "AccountBasedExpenseLineDetail",
            "AccountBasedExpenseLineDetail": {"AccountRef": {"value": acc_id}}}


class SeedLearnTests(unittest.TestCase):
    def test_expense_payment_account_comes_from_the_purchase(self):
        txn = {"AccountRef": {"value": "88"}, "Line": [_eline("5")]}
        self.assertEqual(learn_account_id("Expenses", "Account (Cr)", txn, None), "88")

    def test_expense_account_comes_from_the_only_expense_line(self):
        txn = {"AccountRef": {"value": "88"}, "Line": [_eline("5")]}
        self.assertEqual(learn_account_id("Expenses", "Expense Account (Dr)", txn, None), "5")

    def test_two_expense_lines_learn_nothing(self):
        txn = {"AccountRef": {"value": "88"}, "Line": [_eline("5"), _eline("6")]}
        self.assertIsNone(learn_account_id("Expenses", "Expense Account (Dr)", txn, None))

    def test_transfer_sides_come_from_from_and_to(self):
        txn = {"FromAccountRef": {"value": "1"}, "ToAccountRef": {"value": "2"}}
        self.assertEqual(learn_account_id("Transfers", "Transfer Funds From", txn, None), "1")
        self.assertEqual(learn_account_id("Transfers", "Transfer Funds To", txn, None), "2")

    def test_journal_line_is_found_by_amount_and_side(self):
        txn = {"Line": [_jline("Debit", 100.0, "7"), _jline("Credit", 100.0, "8")]}
        self.assertEqual(learn_account_id("Journals", "Account", txn, 100.0), "7")
        self.assertEqual(learn_account_id("Journals", "Account", txn, -100.0), "8")

    def test_two_journal_lines_with_the_same_amount_learn_nothing(self):
        txn = {"Line": [_jline("Debit", 100.0, "7"), _jline("Debit", 100.0, "9"), _jline("Credit", 200.0, "8")]}
        self.assertIsNone(learn_account_id("Journals", "Account", txn, 100.0))

    def test_unknown_column_learns_nothing(self):
        self.assertIsNone(learn_account_id("Journals", "Memo", {"Line": []}, 1.0))

    def test_empty_cells_read_as_blank(self):
        # KZO dry run 2026-11-18: an empty cell came back as pd.NA and `value or ""` raised.
        self.assertEqual(cell_text(pd.NA), "")
        self.assertEqual(cell_text(float("nan")), "")
        self.assertEqual(cell_text(None), "")
        self.assertEqual(cell_text("  Leading Card ACC  "), "Leading Card ACC")

    def test_rows_that_cannot_be_read_do_not_block_the_others(self):
        # S5 dry run: one 'Recruitment Fee' purchase was deleted in QBO (None), the others
        # posted to 155. A row that tells nothing is not a disagreement.
        self.assertEqual(decide_learned(["155", None], {"155"}), ("155", ""))

    def test_rows_posted_to_two_accounts_are_not_seeded(self):
        acc_id, why = decide_learned(["148", "28"], {"148", "28"})
        self.assertIsNone(acc_id)
        self.assertEqual(why, "posted to ['148', '28']")

    def test_nothing_learned_is_not_seeded(self):
        self.assertEqual(decide_learned([], {"1"}), (None, "no synced rows"))
        self.assertEqual(decide_learned([None], {"1"}), (None, "no row told the account"))

    def test_two_spellings_of_one_name_are_judged_together(self):
        # Review finding 3: 'Leading Card ACC' posted to 148, 'leading card ACC' posted to 28.
        # One map row covers both spellings, so their samples must be pooled.
        resolver = _resolver()
        entries = [
            ("Transfers", "Transfer Funds To", "Leading Card ACC", "55127", None),
            ("Expenses", "Account (Cr)", "leading card  ACC", "55337", None),
        ]
        groups = collect_unresolved(entries, resolver)
        self.assertEqual(list(groups), ["leading card acc"])
        self.assertEqual(groups["leading card acc"]["name"], "Leading Card ACC")
        self.assertEqual(len(groups["leading card acc"]["samples"]), 2)

    def test_each_spelling_gets_its_own_sample_quota(self):
        resolver = _resolver()
        entries = [("Expenses", "Account (Cr)", "Old Card", str(i), None) for i in range(5)]
        entries.append(("Expenses", "Account (Cr)", "old card", "99", None))
        samples = collect_unresolved(entries, resolver)["old card"]["samples"]
        self.assertEqual([s[2] for s in samples], ["0", "1", "2", "99"])

    def test_names_that_resolve_today_are_not_collected(self):
        resolver = _resolver()
        groups = collect_unresolved([("Expenses", "Account (Cr)", "CBD Z 1", "1", None)], resolver)
        self.assertEqual(groups, {})
        self.assertEqual(resolver.pending_rows()[0].qbo_id, "210")

    def test_an_inactive_learned_account_is_not_seeded(self):
        self.assertEqual(decide_learned(["9"], {"1"}), (None, "account 9 is not active in QBO"))


if __name__ == "__main__":
    unittest.main()
