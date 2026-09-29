import unittest
from unittest.mock import MagicMock

import pandas as pd

from src.logic import no_column_check as ncc
from src.logic import raw_adapter


KZO_HEADER = ["CO", "COY", "Date", "USD - QBO", "QBO Import Method", "Transfer to", "Checking ( For our use only )", ""]


class RawNoColumnTests(unittest.TestCase):
    """raw_adapter.raw_no_column reports the column standardize_raw_df reads 'No' from."""

    def test_kzo_no_column_is_the_blank_header_after_checking(self):
        df = pd.DataFrame(columns=KZO_HEADER)
        self.assertEqual(raw_adapter.raw_no_column(df, "KZO"), (7, "kzo"))

    def test_named_no_header_wins(self):
        self.assertEqual(raw_adapter.raw_no_column(pd.DataFrame(columns=["CO", "Date", "No", "x"]), "KZDW"), (2, "kzdw"))
        df = pd.DataFrame(columns=["CO", "COY", "USD - QBO", "QBO Import Method", "Checking ( For our use only )", " No. "])
        self.assertEqual(raw_adapter.raw_no_column(df, "KZO"), (5, "kzo"))

    def test_kzo_without_the_checking_header_falls_back_to_the_legacy_position(self):
        df = pd.DataFrame(columns=KZO_HEADER[:6] + [""] * 30)
        self.assertEqual(raw_adapter.raw_no_column(df, "KZO"), (24, "legacy"))

    def test_missing_kzo_headers_are_named(self):
        df = pd.DataFrame(columns=KZO_HEADER[:6] + [""] * 30)
        self.assertEqual(raw_adapter.missing_kzo_headers(df), ["Checking ( For our use only )"])


class DiagnoseTests(unittest.TestCase):
    ARRAY = '=ARRAYFORMULA(LET(rngT, V5832:V, rngC, C5832:C, IF(rngT = "", "", 1)))'

    def test_clean_column_has_no_problem(self):
        values = {2: 7010001, 3: 7010002, 4: ""}
        self.assertIsNone(ncc.diagnose(values, [self.ARRAY, "", ""], first_row=2, col_letter="AA"))

    def test_blocked_array_formula_lists_the_cells_in_its_way(self):
        # The formula in AA2 could not fill down: AA5 and AA7 hold typed text.
        values = {2: "#REF!", 5: "old 123", 7: "x"}
        formulas = [self.ARRAY, "", "", "old 123", "", "x"]
        problem = ncc.diagnose(values, formulas, first_row=2, col_letter="AA")
        self.assertEqual(problem.blocked, [("AA2", ["AA5", "AA7"])])
        self.assertEqual(problem.bad_rows, [])

    def test_error_on_a_row_is_reported_by_row(self):
        values = {2: 7010001, 29496: "#REF!"}
        formulas = [self.ARRAY]  # the error cell is a filled value, not the formula
        problem = ncc.diagnose(values, formulas, first_row=2, col_letter="AA")
        self.assertEqual(problem.blocked, [])
        self.assertEqual(problem.bad_rows, [(29496, "#REF!")])

    def test_error_text_is_shortened_to_the_error_code(self):
        # UNFORMATTED_VALUE reads return the code plus Google's explanation.
        problem = ncc.diagnose({29496: "#REF! (Reference does not exist.)"}, [self.ARRAY], first_row=2, col_letter="AA")
        self.assertEqual(problem.bad_rows, [(29496, "#REF!")])

    def test_sequence_formula_counts_as_an_array_formula(self):
        problem = ncc.diagnose({6: "#REF!", 9: "note"}, ["=SEQUENCE(COUNTA(C6:C))", "", "", "note"],
                               first_row=6, col_letter="AF")
        self.assertEqual(problem.blocked, [("AF6", ["AF9"])])


class MessageTests(unittest.TestCase):
    def test_blocked_message_names_country_file_and_cells(self):
        problem = ncc.NoColumnProblem(blocked=[("AA5832", ["AA6001", "AA6002"])], bad_rows=[])
        msg = ncc.problem_message(problem, "TH COY raw file (tab 'Transaction 2026 (COY TH)')")
        self.assertEqual(
            msg,
            "⛔ TH COY raw file (tab 'Transaction 2026 (COY TH)'): the No formula in AA5832 is "
            "blocked by text in AA6001, AA6002. Delete that text, then set Transform to READY again.",
        )

    def test_row_error_message_names_the_rows(self):
        problem = ncc.NoColumnProblem(blocked=[], bad_rows=[(29496, "#REF!")])
        msg = ncc.problem_message(problem, "TH COY raw file (tab 'Transaction 2026 (COY TH)')")
        self.assertEqual(
            msg,
            "⛔ TH COY raw file (tab 'Transaction 2026 (COY TH)'): No shows #REF! in row 29496 "
            "(usually a broken Date or QBO Import cell). Fix or delete that row, then set "
            "Transform to READY again.",
        )

    def test_long_lists_are_shortened(self):
        problem = ncc.NoColumnProblem(blocked=[], bad_rows=[(r, "#N/A") for r in range(100, 115)])
        msg = ncc.problem_message(problem, "KZDW raw file (tab 'x')")
        self.assertIn("in rows 100, 101, 102, 103, 104, 105, 106, 107, 108, 109 (+5 more)", msg)
        self.assertIn("Fix or delete those rows", msg)


class CheckRawNoColumnTests(unittest.TestCase):
    def _raw(self, header, rows, header_row):
        df = pd.DataFrame(rows, columns=header)
        df.index = range(len(rows))
        df.attrs["header_row"] = header_row
        return df

    @staticmethod
    def _kzo_row(no):
        return ["TH", "TH", "2026-09-01", 10, "Journal", "Bank", "", no]

    def test_clean_raw_needs_no_extra_read(self):
        gs = MagicMock()
        raw = self._raw(KZO_HEADER, [self._kzo_row(7010001), self._kzo_row(7010002)], 1)
        self.assertIsNone(ncc.check_raw_no_column(gs, "src", "Transaction 2026 (COY TH)", raw, "KZO", "TH"))
        gs.open.assert_not_called()

    def test_kzo_problem_names_the_country_and_reads_the_formulas_once(self):
        gs = MagicMock()
        ws = gs.open.return_value.worksheet.return_value
        ws.get.return_value = [["=ARRAYFORMULA(1)"], [""], ["typed"]]
        raw = self._raw(KZO_HEADER, [self._kzo_row("#REF!"), self._kzo_row(""), self._kzo_row("typed")], 1)
        msg = ncc.check_raw_no_column(gs, "src", "Transaction 2026 (COY TH)", raw, "KZO", "TH")
        ws.get.assert_called_once_with("H2:H", value_render_option="FORMULA")
        self.assertEqual(
            msg,
            "⛔ TH COY raw file (tab 'Transaction 2026 (COY TH)'): the No formula in H2 is "
            "blocked by text in H4. Delete that text, then set Transform to READY again.",
        )

    def test_kzo_missing_checking_header_is_reported(self):
        # TH on 2026-09-29: row 1 lost 'Checking ( For our use only )', so the adapter would
        # fall back to the legacy layout and read No from "Transfer to".
        header = KZO_HEADER[:6] + [""] * 30
        rows = [["TH", "TH", "2026-09-01", 10, "Journal", "Bank A"] + [""] * 20 + [7010001] + [""] * 9]
        msg = ncc.check_raw_no_column(MagicMock(), "src", "Transaction 2026 (COY TH)", self._raw(header, rows, 1), "KZO", "TH")
        self.assertEqual(
            msg,
            "⛔ TH COY raw file (tab 'Transaction 2026 (COY TH)'): row 1 is missing the header "
            "'Checking ( For our use only )', so the bot cannot find the No column. Put it back "
            "(it must sit just left of the No column), then set Transform to READY again.",
        )

    def test_legacy_kzo_tab_with_numbers_in_the_legacy_no_column_is_left_alone(self):
        header = ["h%d" % i for i in range(26)]
        rows = [["x"] * 24 + [7010001, ""], ["x"] * 24 + [7010002, ""]]
        self.assertIsNone(ncc.check_raw_no_column(MagicMock(), "src", "old", self._raw(header, rows, 1), "KZO", "TH"))

    def test_missing_no_header_is_reported_for_other_clients(self):
        raw = self._raw(["CO", "Final Amount to be take (different currency)", "Transfer from", "Transfer to"], [["TH", 1, "a", "b"]], 5)
        msg = ncc.check_raw_no_column(MagicMock(), "src", "2026 transactions (new)", raw, "KZDW", "ALL")
        self.assertEqual(
            msg,
            "⛔ KZDW raw file (tab '2026 transactions (new)'): row 5 has no 'No' header, so the bot "
            "cannot number the rows. Put it back, then set Transform to READY again.",
        )

    def test_other_clients_name_the_client_file(self):
        gs = MagicMock()
        gs.open.return_value.worksheet.return_value.get.return_value = [["=ARRAYFORMULA(1)"], [""]]
        raw = self._raw(["CO", "No"], [["TH", 9010001], ["TH", "#N/A"]], 5)
        msg = ncc.check_raw_no_column(gs, "src", "2026 transactions (new)", raw, "KZDW", "ALL")
        self.assertTrue(msg.startswith("⛔ KZDW raw file (tab '2026 transactions (new)'): No shows #N/A in row 7 "), msg)

    def test_without_a_known_header_row_the_check_is_skipped(self):
        raw = pd.DataFrame([["#REF!"]], columns=["No"])
        self.assertIsNone(ncc.check_raw_no_column(MagicMock(), "src", "t", raw, "KZDW", "ALL"))


if __name__ == "__main__":
    unittest.main()
