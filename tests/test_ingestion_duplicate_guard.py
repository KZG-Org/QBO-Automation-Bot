import unittest
from unittest.mock import MagicMock

import pandas as pd

from config import settings
from run_ingestion import (
    _abs_amounts_by_no,
    _already_transformed_nos,
    _control_cell_ref,
    _heal_last_processed,
    _kzo_checkpoint_cell,
    _kzo_held_note,
    _kzo_last_processed_formula,
    _kzo_missed_nos,
    _delete_retry_rows,
    _pending_after_failed_retry,
    format_month_name,
    get_transform_tab_state,
)

# Control tab header order, as laid out in the live JOBS CONTROL sheet.
CONTROL_COLUMNS = [
    "Client", "Country", "Month", "Last Month Date", "Source File", "Transform File",
    "Tab Name", "Transform", "QBO Sync", "Last Journal No", "Last Expense No",
    "Last Processed Row",
]


def _gs_with_tab(rows, columns=None):
    """A GSheetsClient stub whose single tab holds `rows`."""
    gs = MagicMock()
    df = pd.DataFrame(rows, columns=columns) if rows else pd.DataFrame()
    gs.read_as_df.return_value = df
    return gs


class TransformTabStateTests(unittest.TestCase):
    """The Transform file is the record of what was actually produced."""

    def test_present_nos_reported_when_no_error_rows(self):
        # The common case: a clean tab. Older code returned early here and told the caller
        # nothing, which is why a stale checkpoint could re-transform these rows.
        gs = _gs_with_tab([
            {"No": 8190012, "Exp Ref. No": "KZOPH0826E0027", "Remarks": "Ready to sync"},
            {"No": 8230025, "Exp Ref. No": "KZOPH0826E0034", "Remarks": "Ready to sync"},
        ])

        rows, ids, present, _ = get_transform_tab_state(
            gs, "url", "PH Aug 26 - Expenses", "Exp Ref. No", include_doc_id_match=False
        )

        self.assertEqual(rows, [])
        self.assertEqual(ids, {})
        self.assertEqual(present, {8190012, 8230025})

    def test_error_rows_still_yield_retry_context_plus_present_nos(self):
        gs = _gs_with_tab([
            {"No": 8190012, "Exp Ref. No": "KZOPH0826E0027", "Remarks": "Ready to sync"},
            {"No": 8230025, "Exp Ref. No": "KZOPH0826E0034", "Remarks": "ERROR | Account not found"},
        ])

        rows, ids, present, _ = get_transform_tab_state(
            gs, "url", "PH Aug 26 - Expenses", "Exp Ref. No", include_doc_id_match=False
        )

        self.assertEqual(rows, [3])                              # sheet row of the ERROR row
        self.assertEqual(ids, {8230025: "KZOPH0826E0034"})       # Ref No is reused on rebuild
        self.assertEqual(present, {8190012, 8230025})

    def test_blank_and_zero_nos_are_ignored(self):
        gs = _gs_with_tab([
            {"No": 0, "Ref No": "", "Remarks": ""},
            {"No": "", "Ref No": "", "Remarks": ""},
            {"No": 8230025, "Ref No": "KZOPH0826T0100", "Remarks": "Ready to sync"},
        ])

        _, _, present, _ = get_transform_tab_state(gs, "url", "tab", "Ref No")

        self.assertEqual(present, {8230025})

    def test_missing_tab_reports_nothing_present(self):
        _, _, present, _ = get_transform_tab_state(_gs_with_tab([]), "url", "missing", "Ref No")
        self.assertEqual(present, set())

    def test_tab_without_a_no_column_reports_nothing_present(self):
        gs = _gs_with_tab([{"Ref No": "X", "Remarks": "Ready to sync"}])
        _, _, present, _ = get_transform_tab_state(gs, "url", "tab", "Ref No")
        self.assertEqual(present, set())

    def test_amounts_are_reported_per_no_when_an_amount_column_is_named(self):
        # Journal lines come in +/- pairs per raw row; the row's amount is the largest line.
        gs = _gs_with_tab([
            {"No": 9010087, "Journal No": "KZO-JV1282", "Amount": "-1.51", "Remarks": "Ready to sync"},
            {"No": 9010087, "Journal No": "KZO-JV1282", "Amount": "1.51", "Remarks": "Ready to sync"},
            {"No": 9200073, "Journal No": "KZO-JV1310", "Amount": "75,669.22", "Remarks": "Ready to sync"},
        ])

        _, _, present, amounts = get_transform_tab_state(
            gs, "url", "TH Sep 26 - Journals", "Journal No", amount_col="Amount"
        )

        self.assertEqual(present, {9010087, 9200073})
        self.assertEqual(amounts, {9010087: 1.51, 9200073: 75669.22})

    def test_no_amounts_without_an_amount_column(self):
        gs = _gs_with_tab([{"No": 8230025, "Ref No": "KZOPH0826T0100", "Remarks": "Ready to sync"}])
        _, _, _, amounts = get_transform_tab_state(gs, "url", "tab", "Ref No")
        self.assertEqual(amounts, {})


class AbsAmountsByNoTests(unittest.TestCase):
    def test_largest_absolute_amount_per_no(self):
        nos = pd.Series([9010087, 9010087, 9200073])
        amounts = pd.Series(["-1.51", "1.51", "75,669.22"])
        self.assertEqual(_abs_amounts_by_no(nos, amounts), {9010087: 1.51, 9200073: 75669.22})

    def test_blank_nos_and_unreadable_amounts_are_left_out(self):
        nos = pd.Series([0, "", 9210092, 9210097])
        amounts = pd.Series([5, 5, "n/a", -15963.54])
        self.assertEqual(_abs_amounts_by_no(nos, amounts), {9210097: 15963.54})


class KzoMissedNosTests(unittest.TestCase):
    """Reproduces KZO TH Sep 2026: Sep 27 rows were transformed before Sep 21-26 were ready.

    KZO's raw No starts with the row's date, so once No 9270036 was transformed the checkpoint
    sat above every Sep 21-26 row, and the ones filled in afterwards were never picked up.
    """

    def test_rows_behind_the_checkpoint_missing_from_the_transform_file_are_missed(self):
        transform = {9210092: 21197.08, 9270036: 14513.79}
        raw = {9210079: 103469.00, 9210092: 21197.08, 9270036: 14513.79}

        missed, held = _kzo_missed_nos(
            ready_nos={9210079, 9210092, 9270036},
            last_processed=9270036,
            transformed_nos=set(transform),
            transform_amounts=transform,
            raw_amounts=raw,
        )

        self.assertEqual(missed, {9210079})
        self.assertEqual(held, {})

    def test_rows_above_the_checkpoint_are_left_to_the_new_row_path(self):
        missed, held = _kzo_missed_nos(
            ready_nos={9270085},
            last_processed=9270084,
            transformed_nos={9270084},
            transform_amounts={9270084: 35.0},
            raw_amounts={9270084: 35.0, 9270085: 42.4},
        )

        self.assertEqual(missed, set())
        self.assertEqual(held, {})

    def test_a_renumbered_date_is_held_instead_of_transformed(self):
        # A row inserted on Sep 15 shifts every later Sep 15 No by one: 9150100 now shows the
        # content that was 9150099, and the old 9150100 row now sits at 9150101. Transforming
        # 9150101 would post that row a second time.
        transform = {9150099: 500.0, 9150100: 700.0}
        raw = {9150099: 120.0, 9150100: 500.0, 9150101: 700.0}

        missed, held = _kzo_missed_nos(
            ready_nos={9150099, 9150100, 9150101},
            last_processed=9270084,
            transformed_nos=set(transform),
            transform_amounts=transform,
            raw_amounts=raw,
        )

        self.assertEqual(missed, set())
        self.assertEqual(held, {915: (9150099, {9150101})})

    def test_a_transformed_no_gone_from_raw_holds_its_date(self):
        # A deleted raw row shifts the rest of that date down by one.
        missed, held = _kzo_missed_nos(
            ready_nos={9150100},
            last_processed=9270084,
            transformed_nos={9150101},
            transform_amounts={9150101: 5.0},
            raw_amounts={9150100: 5.0},
        )

        self.assertEqual(missed, set())
        self.assertEqual(held, {915: (9150101, {9150100})})

    def test_other_dates_are_still_picked_up_when_one_date_is_held(self):
        transform = {9150100: 700.0, 9210092: 21197.08}
        raw = {9150100: 500.0, 9150101: 700.0, 9210079: 103469.0, 9210092: 21197.08}

        missed, held = _kzo_missed_nos(
            ready_nos={9150101, 9210079},
            last_processed=9270084,
            transformed_nos=set(transform),
            transform_amounts=transform,
            raw_amounts=raw,
        )

        self.assertEqual(missed, {9210079})
        self.assertEqual(set(held), {915})

    def test_amounts_within_the_reconciler_tolerance_still_match(self):
        missed, held = _kzo_missed_nos(
            ready_nos={9210079},
            last_processed=9270084,
            transformed_nos={9210092},
            transform_amounts={9210092: 21197.08},
            raw_amounts={9210079: 1.0, 9210092: 21197.12},
        )

        self.assertEqual(missed, {9210079})
        self.assertEqual(held, {})

    def test_nothing_missed_when_the_transform_file_has_every_ready_row(self):
        transform = {9210092: 21197.08, 9270036: 14513.79}

        missed, held = _kzo_missed_nos(
            ready_nos=set(transform),
            last_processed=9270036,
            transformed_nos=set(transform),
            transform_amounts=transform,
            raw_amounts=dict(transform),
        )

        self.assertEqual(missed, set())
        self.assertEqual(held, {})

    def test_held_note_names_the_date_the_mismatch_and_the_nos(self):
        note = _kzo_held_note({915: (9150099, {9150102, 9150101}), 1225: (12250003, {12250004})})

        self.assertIn("9/15", note)
        self.assertIn("No 9150099", note)
        self.assertIn("9150101;9150102", note)
        self.assertIn("12/25", note)
        self.assertIn("Pending Amount Nos", note)

    def test_no_note_when_nothing_is_held(self):
        self.assertEqual(_kzo_held_note({}), "")


class HealLastProcessedTests(unittest.TestCase):
    """Reproduces the KZO PH Aug 2026 incident: checkpoint behind the Transform file."""

    def test_checkpoint_is_raised_to_the_transform_files_max_no(self):
        # The Aug 26 run transformed up to No 8230025 and synced it; the checkpoint cell was
        # later back at 8160052, so 115 already-synced rows looked new again.
        self.assertEqual(_heal_last_processed(8160052, {8170001, 8190012, 8230025}), 8230025)

    def test_checkpoint_never_moves_backwards(self):
        # A month whose later rows were removed from the Transform file must not rewind.
        self.assertEqual(_heal_last_processed(8310090, {8170001, 8230025}), 8310090)

    def test_empty_transform_file_leaves_the_checkpoint_alone(self):
        self.assertEqual(_heal_last_processed(8160052, set()), 8160052)

    def test_fresh_row_state_stays_at_zero_when_nothing_transformed(self):
        self.assertEqual(_heal_last_processed(0, set()), 0)


class AlreadyTransformedNosTests(unittest.TestCase):
    def test_rows_present_in_the_transform_file_are_skipped(self):
        selected = {8170001, 8190012, 8240001}
        transformed = {8170001, 8190012}

        self.assertEqual(
            _already_transformed_nos(selected, transformed, retry_nos=set()),
            {8170001, 8190012},
        )

    def test_retry_nos_are_not_skipped(self):
        # Their Transform rows are deleted before the append, so rebuilding them is intended.
        selected = {8170001, 8190012}
        transformed = {8170001, 8190012}

        self.assertEqual(
            _already_transformed_nos(selected, transformed, retry_nos={8190012}),
            {8170001},
        )

    def test_nothing_to_skip_on_a_clean_run(self):
        self.assertEqual(
            _already_transformed_nos({8240001, 8240002}, {8170001}, retry_nos=set()),
            set(),
        )


class _FakeTransformFile:
    """Transform tabs as cell grids (header on row 1). delete_rows_by_tab really removes rows,
    unless `deletes_work` is False (a delete that reports success but leaves the rows)."""

    def __init__(self, tabs, deletes_work=True, fail_read=None):
        self.tabs = {name: [list(r) for r in grid] for name, grid in tabs.items()}
        self.deletes_work = deletes_work
        self.fail_read = fail_read
        self.delete_calls = []

    def read_tab_values(self, url, tab, value_render_option="FORMATTED_VALUE"):
        if tab == self.fail_read:
            raise RuntimeError("APIError 429: Quota exceeded")
        if tab not in self.tabs:
            return None
        return [list(r) for r in self.tabs[tab]]

    def delete_rows_by_tab(self, url, rows_by_tab):
        self.delete_calls.append({tab: list(rows) for tab, rows in rows_by_tab.items()})
        if self.deletes_work:
            for tab, rows in rows_by_tab.items():
                for row_num in sorted(rows, reverse=True):
                    del self.tabs[tab][row_num - 1]


class DeleteRetryRowsTests(unittest.TestCase):
    """Retry Nos are exempt from the append guard only because their old rows were deleted.
    S5 Oct 26: the deletes failed silently and each retry run added another copy."""

    TABS = ["Journals", "Expenses", "Transfers"]

    def _file(self, **kw):
        return _FakeTransformFile({
            "Journals": [["No", "Journal No"], ["1030007", "S5-JV0264"], ["1030007", "S5-JV0264"]],
            "Expenses": [["No", "Exp Ref. No"], ["1030035", "E0207"], ["1099999", "E0999"], ["1030035", "E0207"]],
            "Transfers": [["No", "Ref No"], ["1030384", "T0030"]],
        }, **kw)

    def test_every_tab_goes_in_one_delete_call(self):
        f = self._file()

        _delete_retry_rows(f, "url", self.TABS, {1030007, 1030035, 1030384})

        # One call for all tabs: Sheets applies it all or not at all, so no tab is half done.
        self.assertEqual(f.delete_calls, [{"Journals": [3, 2], "Expenses": [4, 2], "Transfers": [2]}])
        self.assertEqual(f.tabs["Expenses"], [["No", "Exp Ref. No"], ["1099999", "E0999"]])
        self.assertEqual(f.tabs["Journals"], [["No", "Journal No"]])

    def test_raises_when_a_row_survives_the_delete(self):
        f = self._file(deletes_work=False)

        with self.assertRaises(RuntimeError) as ctx:
            _delete_retry_rows(f, "url", self.TABS, {1030035})
        self.assertIn("1030035", str(ctx.exception))

    def test_a_failed_read_raises_before_anything_is_deleted(self):
        # read_as_df would have turned this into an empty tab and skipped the delete.
        f = self._file(fail_read="Expenses")

        with self.assertRaises(RuntimeError):
            _delete_retry_rows(f, "url", self.TABS, {1030007, 1030035})
        self.assertEqual(f.delete_calls, [])

    def test_a_missing_tab_holds_nothing(self):
        f = self._file()
        del f.tabs["Transfers"]

        _delete_retry_rows(f, "url", self.TABS, {1030035})

        self.assertEqual(f.delete_calls, [{"Expenses": [4, 2]}])

    def test_nothing_read_or_deleted_without_retry_nos(self):
        f = self._file(fail_read="Expenses")  # any read would raise

        _delete_retry_rows(f, "url", self.TABS, set())

        self.assertEqual(f.delete_calls, [])

    def test_failed_retry_nos_join_pending_amount_nos(self):
        # Some rows may already be gone (lost reply). Pending Amount Nos makes the next run
        # rebuild those; step 11b skips any still present.
        self.assertEqual(
            _pending_after_failed_retry({1013002}, {1030035, 1030007}),
            "1013002;1030007;1030035",
        )


class KzoCheckpointFormulaTests(unittest.TestCase):
    """KZO stores 'Last Processed Row' as a live formula over its Transform tabs."""

    def test_transform_file_column_resolves_to_its_a1_ref(self):
        # 'Transform File' is column F in the live control sheet.
        self.assertEqual(
            _control_cell_ref(CONTROL_COLUMNS, settings.CTRL_COL_TRANSFORM_URL, 54), "F54"
        )
        self.assertEqual(
            _control_cell_ref(CONTROL_COLUMNS, settings.CTRL_COL_TRANSFORM_URL, 53), "F53"
        )

    def test_missing_column_has_no_ref(self):
        self.assertIsNone(_control_cell_ref(["Country"], settings.CTRL_COL_TRANSFORM_URL, 54))

    def test_formula_reads_all_three_tabs_of_the_row_month(self):
        formula = _kzo_last_processed_formula("F54", "PH Aug 26")

        self.assertTrue(formula.startswith("=IFERROR(MAX(UNIQUE(VSTACK("))
        for tab in ("Journals", "Expenses", "Transfers"):
            self.assertIn(f'IMPORTRANGE(F54, "PH Aug 26 - {tab}!A2:A")', formula)
        # Every range is guarded, so a tab that does not exist yet reads 0, not #REF!.
        self.assertEqual(formula.count("IFERROR("), 4)

    def test_tab_names_follow_the_month_and_year(self):
        # The prefix comes from the row's own Country + Month, so Sep and a year roll are
        # picked up with no edit to the formula.
        self.assertEqual(format_month_name("2026-09-01"), "Sep 26")
        self.assertEqual(format_month_name("2027-01-01"), "Jan 27")
        self.assertIn(
            'IMPORTRANGE(F60, "PH Jan 27 - Journals!A2:A")',
            _kzo_last_processed_formula("F60", "PH Jan 27"),
        )

    def test_kzo_gets_the_formula_and_others_keep_the_number(self):
        kzo = _kzo_checkpoint_cell(True, CONTROL_COLUMNS, 54, "PH Aug 26", 8230025)
        other = _kzo_checkpoint_cell(False, CONTROL_COLUMNS, 7, "TH Aug 26", 8230146)

        self.assertTrue(str(kzo).startswith("="))
        self.assertEqual(other, 8230146)  # KZP / S5 / UMBER / KZDW keep the snapshot

    def test_falls_back_to_the_number_without_a_transform_file_column(self):
        self.assertEqual(
            _kzo_checkpoint_cell(True, ["Country", "Month"], 54, "PH Aug 26", 8230025), 8230025
        )


if __name__ == "__main__":
    unittest.main()
