import unittest
from unittest.mock import MagicMock

import gspread

from src.connectors.gsheets_client import GSheetsClient
from src.logic.account_map import (
    ACCOUNT_MAP_HEADERS,
    ACCOUNT_MAP_TAB,
    AccountResolver,
    MapRow,
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

    def write_values_raw(self, sheet_id, tab, updates, create_if_missing=False):
        self.writes.append({"tab": tab, "updates": updates, "create": create_if_missing})


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

    def test_no_updates_makes_no_call(self):
        gs, sh, _ = self._gs()
        gs.write_values_raw("sid", ACCOUNT_MAP_TAB, [])
        gs.open.assert_not_called()


if __name__ == "__main__":
    unittest.main()
