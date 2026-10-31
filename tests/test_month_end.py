import unittest
from datetime import datetime

import pandas as pd

from src.logic.month_end import (
    MonthEndInputError,
    changed_rows,
    is_month_end_row,
    parse_status,
    plan_month_end,
    read_source_tab,
    transform_note,
)

HEADER = ["Date", "Memo", "Account", "Amount", "Name", "Location", "Currency Code", "Class", "Status"]
MEMO = "Month-End Sep 26 Closing Reclassification to Countries by Revenue Ratio"
OCT_31 = (pd.Timestamp("2026-10-31") - pd.Timestamp("1899-12-30")).days  # Sheets serial, as read UNFORMATTED
SEP_30 = (pd.Timestamp("2026-09-30") - pd.Timestamp("1899-12-30")).days

# The team's test tab (rows 2-10): one KZO reclass of AI Expenses across countries.
KZO_RECLASS = [
    ("TH", 11702.52), ("PK", 423.64), ("PH", 3540.89), ("PE", 72.69), ("MX", 579.52),
    ("GROUP", -17167.88), ("CO", 66.00), ("BR", 80.16), ("BD", 702.46),
]
LOCS = ["TH", "PK", "PH", "PE", "MX", "GROUP", "CO", "BR", "BD"]
MAPPINGS = {
    "accounts": {"AI Expenses": "1", "Account B": "2"},
    "locations": {loc: f"L{i}" for i, loc in enumerate(LOCS)},
    "classes": {loc: f"C{i}" for i, loc in enumerate(LOCS)},
}
OCT_START = datetime(2026, 10, 1)
OCT_END = datetime(2026, 10, 31, 23, 59, 59, 999999)


def line(date=OCT_31, memo=MEMO, account="AI Expenses", amount=0.0, location="TH",
         currency="USD", cls=None, status="", name=None):
    return [date, memo, account, amount, MEMO if name is None else name, location, currency,
            location if cls is None else cls, status]


def reclass_lines(status=""):
    return [line(amount=amt, location=loc, status=status) for loc, amt in KZO_RECLASS]


def two_account_lines():
    # Rows 11-12 of the test tab: the shape other companies use.
    return [
        line(memo="Month-End", account="AI Expenses", amount=500, location="", cls="", name=""),
        line(memo="Month-End", account="Account B", amount=-500, location="", cls="", name=""),
    ]


def plan(rows, client="KZO", start_jv=1417, jv_in_tab=None, header=HEADER):
    source = read_source_tab([header] + rows)
    return plan_month_end(
        source, client_name=client, month_start=OCT_START, month_end=OCT_END,
        start_jv=start_jv, jv_in_tab=jv_in_tab or set(), qbo_mappings=MAPPINGS,
    )


class IsMonthEndRowTests(unittest.TestCase):
    def test_marker_ignores_case_and_spaces(self):
        self.assertTrue(is_month_end_row(" month-end  JV "))

    def test_country_rows_are_not_month_end(self):
        self.assertFalse(is_month_end_row("TH"))
        self.assertFalse(is_month_end_row(None))


class ParseStatusTests(unittest.TestCase):
    def test_blank_and_error_are_new(self):
        self.assertEqual(parse_status(""), ("new", None))
        self.assertEqual(parse_status("ERROR: Account not found: 'X'"), ("new", None))

    def test_transformed_and_synced_carry_the_number(self):
        self.assertEqual(parse_status("Transformed KZO-JV1418"), ("transformed", "KZO-JV1418"))
        self.assertEqual(parse_status("Synced KZO-JV1418"), ("synced", "KZO-JV1418"))

    def test_other_text_is_unknown(self):
        self.assertEqual(parse_status("done"), ("unknown", None))


class ReadSourceTabTests(unittest.TestCase):
    def test_rows_keep_sheet_row_numbers_across_blank_rows(self):
        blank = [""] * len(HEADER)
        source = read_source_tab([HEADER, line(amount=1), blank, line(amount=-1)])
        self.assertEqual(list(source.df.index), [2, 4])

    def test_status_column_found_by_name(self):
        source = read_source_tab([HEADER, line(amount=1)])
        self.assertEqual(source.status_col, 9)
        self.assertTrue(source.has_status_header)

    def test_missing_status_header_goes_after_last_header(self):
        header = HEADER[:-1] + [""]
        source = read_source_tab([header, line(amount=1)[:-1] + [""]])
        self.assertFalse(source.has_status_header)
        self.assertEqual(source.status_col, 9)

    def test_missing_required_header_stops(self):
        header = ["Date", "Memo", "Acct", "Amount", "Status"]
        with self.assertRaises(MonthEndInputError) as ctx:
            read_source_tab([header, [OCT_31, "m", "AI Expenses", 1, ""]])
        self.assertIn("'Account'", str(ctx.exception))


class PlanMonthEndTests(unittest.TestCase):
    def test_kzo_reclass_becomes_one_journal(self):
        result = plan(reclass_lines())
        self.assertEqual(len(result.journals), 9)
        self.assertEqual(set(result.journals["Journal No"]), {"KZO-JV1418"})
        self.assertEqual(result.last_journal_no, 1418)
        self.assertEqual(result.new_journal_nos, ["KZO-JV1418"])
        self.assertEqual(result.status_updates, {r: "Transformed KZO-JV1418" for r in range(2, 11)})

    def test_output_uses_the_normal_journal_columns(self):
        out = plan(reclass_lines()).journals
        self.assertEqual(list(out.columns), [
            "No", "Journal No", "Date", "Memo", "Account", "Amount", "Name", "Location",
            "Currency Code", "Class", "Remarks",
        ])
        first = out.iloc[0]
        self.assertEqual(first["No"], "")
        self.assertEqual(first["Date"], "2026-10-31")
        self.assertEqual(first["Amount"], 11702.52)
        self.assertEqual(first["Class"], "TH")
        self.assertEqual(first["Remarks"], "Ready to sync")

    def test_amounts_are_not_rounded_or_adjusted(self):
        out = plan(reclass_lines()).journals
        self.assertEqual(list(out["Amount"]), [amt for _, amt in KZO_RECLASS])

    def test_kzo_entry_with_two_accounts_fails(self):
        result = plan(two_account_lines())
        self.assertTrue(result.journals.empty)
        self.assertEqual(result.status_updates, {
            2: "ERROR: Journal does not balance (diff 500.00)",
            3: "ERROR: Journal does not balance (diff -500.00)",
        })
        self.assertEqual(result.last_journal_no, 1417)

    def test_kzp_entry_with_two_accounts_is_one_journal(self):
        result = plan(two_account_lines(), client="KZP", start_jv=5)
        self.assertEqual(list(result.journals["Journal No"]), ["KZP-JV0006", "KZP-JV0006"])

    def test_failed_journal_takes_no_number(self):
        bad = [line(memo="bad", account="Nope", amount=10), line(memo="bad", account="Nope", amount=-10, location="PK")]
        result = plan(bad + reclass_lines())
        self.assertEqual(result.new_journal_nos, ["KZO-JV1418"])
        self.assertEqual(result.failed_journal_count, 1)

    def test_unknown_account_names_the_line(self):
        rows = [line(memo="x", account="Nope", amount=10), line(memo="x", account="Nope", amount=-10, location="PK")]
        self.assertEqual(plan(rows).status_updates[2], "ERROR: Account not found: 'Nope'")

    def test_sibling_of_a_bad_line_says_so(self):
        result = plan([line(amount=10), line(amount=-10, location="Mars")])
        self.assertEqual(result.status_updates[3], "ERROR: Location not found: 'Mars'")
        self.assertEqual(result.status_updates[2], "ERROR: Another line in this journal has an error")

    def test_typed_class_must_exist(self):
        result = plan([line(amount=10, cls="Nowhere"), line(amount=-10)])
        self.assertEqual(result.status_updates[2], "ERROR: Class not found: 'Nowhere'")

    def test_more_than_two_decimals_is_stopped(self):
        result = plan([line(amount=11702.5197), line(amount=-11702.5197, location="PK")])
        self.assertEqual(
            result.status_updates[2],
            "ERROR: Amount 11702.5197 has more than 2 decimals. Use ROUND(...,2) in the sheet",
        )

    def test_unbalanced_journal_shows_the_difference(self):
        result = plan([line(amount=10.01), line(amount=-10, location="PK")])
        self.assertEqual(result.status_updates[2], "ERROR: Journal does not balance (diff 0.01)")

    def test_missing_amount_is_an_error(self):
        result = plan([line(amount=""), line(amount=-10, location="PK")])
        self.assertEqual(result.status_updates[2], "ERROR: Missing Amount")

    def test_kzo_rejects_non_usd(self):
        result = plan([line(amount=10, currency="THB"), line(amount=-10, currency="THB", location="PK")])
        self.assertEqual(result.status_updates[2], "ERROR: KZO journals post in USD only (Currency Code 'THB')")

    def test_kzdw_rejects_mixed_currencies(self):
        rows = [line(amount=10, currency="USD"), line(amount=-10, currency="THB", location="PK")]
        result = plan(rows, client="KZDW", start_jv=50)
        self.assertEqual(result.status_updates[2], "ERROR: Mixed currencies in one journal")

    def test_kzdw_adds_currency_exchange(self):
        out = plan([line(amount=10), line(amount=-10, location="PK")], client="KZDW", start_jv=50).journals
        self.assertIn("Currency Exchange", out.columns)
        self.assertEqual(list(out["Currency Exchange"]), [1, 1])
        self.assertEqual(set(out["Journal No"]), {"KZDW-JV0051"})

    def test_s5_leaves_location_and_class_blank(self):
        out = plan([line(amount=10), line(amount=-10, location="PK")], client="S5", start_jv=1).journals
        self.assertEqual(list(out["Location"]), ["", ""])
        self.assertEqual(list(out["Class"]), ["", ""])

    def test_blank_class_uses_location(self):
        out = plan([line(amount=10, cls=""), line(amount=-10, location="PK", cls="")]).journals
        self.assertEqual(list(out["Class"]), ["TH", "PK"])

    def test_transformed_line_missing_from_tab_is_rebuilt_under_its_number(self):
        result = plan(reclass_lines(status="Transformed KZO-JV1400"))
        self.assertEqual(set(result.journals["Journal No"]), {"KZO-JV1400"})
        self.assertEqual(result.status_updates, {})
        self.assertEqual(result.last_journal_no, 1417)
        self.assertEqual(result.rebuilt_journal_nos, ["KZO-JV1400"])

    def test_transformed_line_still_in_tab_is_skipped(self):
        result = plan(reclass_lines(status="Transformed KZO-JV1400"), jv_in_tab={"KZO-JV1400"})
        self.assertTrue(result.journals.empty)

    def test_synced_line_is_skipped(self):
        self.assertTrue(plan(reclass_lines(status="Synced KZO-JV1400")).journals.empty)

    def test_error_line_is_tried_again(self):
        result = plan(reclass_lines(status="ERROR: Account not found: 'AI Expense'"))
        self.assertEqual(result.new_journal_nos, ["KZO-JV1418"])

    def test_other_company_number_in_the_tab_stops_transform(self):
        rows = reclass_lines() + [line(memo="old", amount=1, status="Synced KZP-JV0012")]
        with self.assertRaises(MonthEndInputError) as ctx:
            plan(rows)
        self.assertIn("KZP-JV0012", str(ctx.exception))

    def test_line_outside_the_month_is_left_for_later(self):
        result = plan(reclass_lines() + [line(date=SEP_30, memo="sep", amount=5)])
        self.assertEqual(result.outside_month_count, 1)
        self.assertNotIn(11, result.status_updates)

    def test_text_date_is_read_day_first(self):
        rows = [line(date="31/10/2026", amount=10), line(date="31/10/2026", amount=-10, location="PK")]
        self.assertEqual(list(plan(rows).journals["Date"]), ["2026-10-31", "2026-10-31"])

    def test_text_amounts_with_commas_and_brackets(self):
        rows = [line(amount="17,167.88"), line(amount="(17,167.88)", location="GROUP")]
        self.assertEqual(list(plan(rows).journals["Amount"]), [17167.88, -17167.88])

    def test_rows_to_verify_cover_every_planned_line(self):
        self.assertEqual(sorted(plan(reclass_lines()).rows_to_verify), list(range(2, 11)))


class ChangedRowsTests(unittest.TestCase):
    def test_same_data_is_unchanged(self):
        a = read_source_tab([HEADER] + reclass_lines())
        b = read_source_tab([HEADER] + reclass_lines())
        self.assertEqual(changed_rows(a, b, [2, 3]), [])

    def test_moved_or_edited_rows_are_reported(self):
        a = read_source_tab([HEADER] + reclass_lines())
        edited = reclass_lines()
        edited[1][3] = 999.0
        b = read_source_tab([HEADER] + edited[:-1])
        self.assertEqual(changed_rows(a, b, [3, 10]), [3, 10])


class TransformNoteTests(unittest.TestCase):
    def test_note_names_numbers_errors_and_later_lines(self):
        rows = (reclass_lines() + [line(memo="x", account="Nope", amount=1)]
                + [line(date=SEP_30, memo="s", amount=1)])
        note = transform_note(plan(rows), "Month-end JV_test", "Oct 26")
        self.assertIn("1 journal ready: KZO-JV1418.", note)
        self.assertIn("1 journal has errors: see Status in 'Month-end JV_test'.", note)
        self.assertIn("1 line dated outside Oct 26 left for later.", note)


CTRL_COLUMNS = [
    "Country", "Source File", "Tab Name", "Month", "Last Month Date", "Transform File",
    "Transform", "QBO Sync", "QBO Reconcile", "Last Journal No", "QBO Journal", "Transform Note",
]
SOURCE = "https://docs.google.com/spreadsheets/d/SRC"
TRANSFORM = "https://docs.google.com/spreadsheets/d/TRF"
TAB = "Month-end JV_test"
TAB_JV = "Month-end JV Oct 26 - Journals"


def _ctrl(*extra_rows):
    me = {"Country": "Month-end JV", "Source File": SOURCE, "Tab Name": TAB, "Month": "2026-10-01",
          "Last Month Date": "2026-10-31", "Transform File": TRANSFORM, "Transform": "READY"}
    rows = [me] + list(extra_rows)
    return pd.DataFrame(rows, columns=CTRL_COLUMNS).fillna("")


def _gs(source_reads, transform_values=None, titles=("JOBS CONTROL", "Sample - Journals", "Sample - Journals_monthend")):
    """Sheets stub: successive source reads return successive grids."""
    from unittest.mock import MagicMock
    gs = MagicMock()
    reads = list(source_reads)

    def read_values(url, tab, value_render_option="FORMATTED_VALUE"):
        if url == SOURCE:
            return reads.pop(0) if len(reads) > 1 else reads[0]
        return []

    gs.read_values.side_effect = read_values
    # The Transform tab is read strictly: None = tab missing, an error is raised, not swallowed.
    gs.read_tab_values.side_effect = lambda url, tab, value_render_option="FORMATTED_VALUE": transform_values
    gs.worksheet_titles.return_value = list(titles)
    return gs


def _control_updates_for(gs, row_num):
    merged = {}
    for c in gs.batch_update_cells.call_args_list:
        if c.args[0] == "CTRL":
            for u in c.args[2]:
                if u["row"] == row_num:
                    merged[CTRL_COLUMNS[u["col"] - 1]] = u["val"]
    return merged


def _run(gs, ctrl=None, start_jv=1417):
    from run_ingestion import _process_month_end_row
    ctrl = _ctrl() if ctrl is None else ctrl
    return _process_month_end_row(
        gs, "CTRL", ctrl, ctrl.iloc[0], 2, "KZO", MAPPINGS, TRANSFORM, start_jv,
    )


def _control_updates(gs):
    """{column name: value} merged over every control-sheet write, in order."""
    merged = {}
    for c in gs.batch_update_cells.call_args_list:
        if c.args[0] == "CTRL":
            for u in c.args[2]:
                merged[CTRL_COLUMNS[u["col"] - 1]] = u["val"]
    return merged


class ProcessMonthEndRowTests(unittest.TestCase):
    GRID = [HEADER] + reclass_lines()

    def test_numbers_are_reserved_before_status_and_transform_writes(self):
        gs = _gs([self.GRID])
        self.assertEqual(_run(gs), 1418)
        order = []
        for c in gs.mock_calls:
            name, args = c[0], c[1]
            if name == "batch_update_cells" and args[0] == "CTRL" and any(
                CTRL_COLUMNS[u["col"] - 1] == "Last Journal No" for u in args[2]
            ):
                order.append("reserve")
            elif name == "batch_update_cells" and args[0] == SOURCE:
                order.append("status")
            elif name == "append_or_create_df":
                order.append("append")
        self.assertEqual(order, ["reserve", "status", "append"])

    def test_happy_path_writes_statuses_journals_and_control(self):
        gs = _gs([self.GRID])
        _run(gs)
        status_call = next(c for c in gs.batch_update_cells.call_args_list if c.args[0] == SOURCE)
        self.assertEqual(status_call.args[1], TAB)
        self.assertEqual({(u["row"], u["col"], u["val"]) for u in status_call.args[2]},
                         {(r, 9, "Transformed KZO-JV1418") for r in range(2, 11)})
        append = gs.append_or_create_df.call_args
        self.assertEqual(append.args[1], TAB_JV)
        self.assertEqual(len(append.args[2]), 9)
        self.assertEqual(append.kwargs["template_tab_name"], "Sample - Journals_monthend")
        updates = _control_updates(gs)
        self.assertEqual(updates["Transform"], "DONE")
        self.assertEqual(updates["QBO Journal"], "READY TO SYNC")
        self.assertEqual(updates["Last Journal No"], "1418")
        self.assertIn("KZO-JV1418", updates["Transform Note"])

    def test_template_falls_back_to_sample_journals(self):
        gs = _gs([self.GRID], titles=("JOBS CONTROL", "Sample - Journals"))
        _run(gs)
        self.assertEqual(gs.append_or_create_df.call_args.kwargs["template_tab_name"], "Sample - Journals")

    def test_status_write_failure_copies_nothing(self):
        gs = _gs([self.GRID])

        def batch(url, tab, updates):
            if url == SOURCE:
                raise PermissionError("403 caller does not have permission")
        gs.batch_update_cells.side_effect = batch
        _run(gs)
        gs.append_or_create_df.assert_not_called()
        updates = _control_updates(gs)
        self.assertEqual(updates["Transform"], "ERROR")
        self.assertIn("Editor access", updates["Transform Note"])

    def test_source_changed_during_transform_writes_nothing(self):
        edited = [HEADER] + reclass_lines()
        edited[3][3] = 1.0
        gs = _gs([self.GRID, edited])
        self.assertEqual(_run(gs), 1417)
        self.assertFalse(any(c.args[0] == SOURCE for c in gs.batch_update_cells.call_args_list))
        gs.append_or_create_df.assert_not_called()
        updates = _control_updates(gs)
        self.assertEqual(updates["Transform"], "ERROR")
        self.assertIn("changed during Transform", updates["Transform Note"])

    def test_missing_status_header_is_added(self):
        grid = [HEADER[:-1]] + [r[:-1] for r in reclass_lines()]
        gs = _gs([grid])
        _run(gs)
        gs.update_cell.assert_any_call(SOURCE, TAB, 1, 9, "Status")

    def test_second_month_end_row_for_the_same_month_is_refused(self):
        other = {"Country": "Month-end JV", "Source File": SOURCE, "Tab Name": "x", "Month": "2026-10-01"}
        gs = _gs([self.GRID])
        _run(gs, ctrl=_ctrl(other))
        gs.read_values.assert_not_called()
        updates = _control_updates(gs)
        self.assertEqual(updates["Transform"], "ERROR")
        self.assertIn("row 3", updates["Transform Note"])

    def test_unreadable_transform_tab_stops_before_any_write(self):
        # A 429 on the Transform tab must not look like "no tab yet": every journal not yet
        # synced would be copied a second time and posted with doubled lines.
        gs = _gs([self.GRID])
        gs.read_tab_values.side_effect = RuntimeError("APIError 429: Quota exceeded")
        self.assertEqual(_run(gs), 1417)
        self.assertFalse(any(c.args[0] == SOURCE for c in gs.batch_update_cells.call_args_list))
        gs.append_or_create_df.assert_not_called()
        gs.delete_rows.assert_not_called()
        updates = _control_updates(gs)
        self.assertNotIn("Last Journal No", updates)
        self.assertEqual(updates["Transform"], "ERROR")
        self.assertIn("could not read", updates["Transform Note"])

    def test_lost_status_column_with_journals_in_the_tab_stops(self):
        # Without Status every line looks new: the whole month would get new numbers and post again.
        tab = [["No", "Journal No", "Date", "Memo", "Account", "Amount", "Remarks"],
               ["", "KZO-JV1418", "2026-10-31", MEMO, "AI Expenses", "1", "Synced at 2026-10-31"]]
        grid = [HEADER[:-1]] + [r[:-1] for r in reclass_lines()]
        gs = _gs([grid], transform_values=tab)
        self.assertEqual(_run(gs), 1417)
        gs.update_cell.assert_not_called()
        self.assertFalse(any(c.args[0] == SOURCE for c in gs.batch_update_cells.call_args_list))
        gs.append_or_create_df.assert_not_called()
        updates = _control_updates(gs)
        self.assertEqual(updates["Transform"], "ERROR")
        self.assertIn("Status column is missing", updates["Transform Note"])

    def test_journal_that_failed_at_sync_is_rebuilt_under_its_number(self):
        tab = [["No", "Journal No", "Date", "Memo", "Account", "Amount", "Remarks"],
               ["", "KZO-JV1400", "2026-10-31", MEMO, "AI Expenses", "1", "ERROR: QBO 400"],
               ["", "KZO-JV1400", "2026-10-31", MEMO, "AI Expenses", "-1", "Ready to sync"]]
        gs = _gs([[HEADER] + reclass_lines(status="Transformed KZO-JV1400")], transform_values=tab)
        self.assertEqual(_run(gs), 1417)
        gs.delete_rows.assert_called_once_with(TRANSFORM, TAB_JV, [2, 3])
        self.assertEqual(set(gs.append_or_create_df.call_args.args[2]["Journal No"]), {"KZO-JV1400"})


class ControlSheetRunTests(unittest.TestCase):
    """The real per-client loop, with Sheets and QBO stubbed."""

    def _run_control_sheet(self, ctrl, grid, append=None):
        from unittest.mock import MagicMock, patch
        from run_ingestion import process_client_control_sheet

        gs = MagicMock()
        gs.read_as_df.return_value = ctrl
        gs.worksheet_titles.return_value = ["Sample - Journals"]
        gs.read_values.side_effect = lambda url, tab, value_render_option="FORMATTED_VALUE": (
            grid if url == SOURCE else []
        )
        gs.read_tab_values.return_value = None  # no Transform tab yet
        if append:
            gs.append_or_create_df.side_effect = append
        qbo = MagicMock()
        qbo.get_max_journal_number.return_value = 1371
        with patch("run_ingestion.QBOSync") as sync_cls:
            sync_cls.return_value.mappings = MAPPINGS
            process_client_control_sheet(gs, qbo, "CTRL", "KZO", "realm")
        return gs

    def test_two_month_end_rows_in_one_run_never_share_a_number(self):
        nov_30 = (pd.Timestamp("2026-11-30") - pd.Timestamp("1899-12-30")).days
        nov = [line(date=nov_30, memo="Nov close", amount=amt, location=loc) for loc, amt in KZO_RECLASS]
        grid = [HEADER] + reclass_lines() + nov
        base = {"Country": "Month-end JV", "Source File": SOURCE, "Tab Name": TAB,
                "Transform": "READY", "Last Journal No": ""}
        ctrl = pd.DataFrame([
            {"Country": "TH", "Month": "2026-10-01", "Transform": "DONE", "Last Journal No": 1417},
            dict(base, Month="2026-10-01", **{"Transform File": TRANSFORM}),
            dict(base, Month="2026-11-01", **{"Transform File": TRANSFORM + "2"}),
        ], columns=CTRL_COLUMNS).fillna("")

        gs = self._run_control_sheet(ctrl, grid)

        appended = {c.args[0]: set(c.args[2]["Journal No"]) for c in gs.append_or_create_df.call_args_list}
        self.assertEqual(appended, {TRANSFORM: {"KZO-JV1418"}, TRANSFORM + "2": {"KZO-JV1419"}})
        tabs = {c.args[0]: c.args[1] for c in gs.append_or_create_df.call_args_list}
        self.assertEqual(tabs[TRANSFORM], "Month-end JV Oct 26 - Journals")
        self.assertEqual(tabs[TRANSFORM + "2"], "Month-end JV Nov 26 - Journals")

    def test_failure_after_numbering_still_moves_the_counter_on(self):
        # Oct numbers KZO-JV1418 and writes its Status, then its Transform write fails. The Nov
        # row in the same run must not take 1418 again.
        nov_30 = (pd.Timestamp("2026-11-30") - pd.Timestamp("1899-12-30")).days
        nov = [line(date=nov_30, memo="Nov close", amount=amt, location=loc) for loc, amt in KZO_RECLASS]
        base = {"Country": "Month-end JV", "Source File": SOURCE, "Tab Name": TAB, "Transform": "READY"}
        ctrl = pd.DataFrame([
            {"Country": "TH", "Month": "2026-10-01", "Transform": "DONE", "Last Journal No": 1417},
            dict(base, Month="2026-10-01", **{"Transform File": TRANSFORM}),
            dict(base, Month="2026-11-01", **{"Transform File": TRANSFORM + "2"}),
        ], columns=CTRL_COLUMNS).fillna("")

        def append(url, tab, df, **kwargs):
            if url == TRANSFORM:
                raise RuntimeError("APIError 503: backend error")

        gs = self._run_control_sheet(ctrl, [HEADER] + reclass_lines() + nov, append=append)

        nov_call = next(c for c in gs.append_or_create_df.call_args_list if c.args[0] == TRANSFORM + "2")
        self.assertEqual(set(nov_call.args[2]["Journal No"]), {"KZO-JV1419"})
        oct_updates = _control_updates_for(gs, 3)
        self.assertEqual(oct_updates["Last Journal No"], "1418")
        self.assertEqual(oct_updates["Transform"], "ERROR")
        self.assertIn("Run Transform again", oct_updates["Transform Note"])

    def test_month_end_row_skips_the_raw_pipeline(self):
        from unittest.mock import patch
        ctrl = pd.DataFrame([{"Country": "Month-end JV", "Source File": SOURCE, "Tab Name": TAB,
                              "Month": "2026-10-01", "Transform File": TRANSFORM, "Transform": "READY"}],
                            columns=CTRL_COLUMNS).fillna("")
        with patch("run_ingestion._read_source_raw_df") as raw_read, \
                patch("run_ingestion.check_raw_no_column") as no_check:
            gs = self._run_control_sheet(ctrl, [HEADER] + reclass_lines())
        raw_read.assert_not_called()
        no_check.assert_not_called()
        # No control row holds a number here, so the start is QBO's highest (1371).
        self.assertEqual(set(gs.append_or_create_df.call_args.args[2]["Journal No"]), {"KZO-JV1372"})


def _tab_lines(jv, amounts, remarks="Ready to sync", date="2026-10-31"):
    return [{"No": "", "Journal No": jv, "Date": date, "Memo": MEMO, "Account": "AI Expenses",
             "Amount": a, "Name": "", "Location": "TH", "Currency Code": "USD", "Class": "TH",
             "Remarks": remarks} for a in amounts]


class SyncHelperTests(unittest.TestCase):
    def test_same_date_and_memo_is_the_same_journal(self):
        # Matched on what push_journal writes (TxnDate, PrivateNote = first line's Memo), not on
        # the total: an edited rebuild of a posted journal is still the same journal, so it is
        # skipped, never re-posted under a new number.
        from src.logic.month_end import find_number_clashes
        to_sync = pd.DataFrame(_tab_lines("KZO-JV1418", [100.0, 50.5, -150.5]))
        qbo = {"KZO-JV1418": [("2026-10-31", "  " + MEMO.upper() + " ")]}
        self.assertEqual(find_number_clashes(to_sync, {"KZO-JV1418"}, qbo), set())

    def test_different_memo_or_date_is_a_clash(self):
        from src.logic.month_end import find_number_clashes
        to_sync = pd.DataFrame(_tab_lines("KZO-JV1418", [100.0, -100.0]) + _tab_lines("KZO-JV1419", [5.0, -5.0]))
        qbo = {"KZO-JV1418": [("2026-10-31", "Received from Gpay-Native XOJILI")],
               "KZO-JV1419": [("2026-10-30", MEMO)]}
        self.assertEqual(find_number_clashes(to_sync, {"KZO-JV1418", "KZO-JV1419"}, qbo),
                         {"KZO-JV1418", "KZO-JV1419"})

    def test_one_matching_record_among_duplicates_is_enough(self):
        from src.logic.month_end import find_number_clashes
        to_sync = pd.DataFrame(_tab_lines("KZO-JV1418", [100.0, -100.0]))
        qbo = {"KZO-JV1418": [("2026-09-30", "other"), ("2026-10-31", MEMO)]}
        self.assertEqual(find_number_clashes(to_sync, {"KZO-JV1418"}, qbo), set())

    def test_unknown_qbo_details_fall_back_to_todays_skip(self):
        # The detail lookup failed: keep today's behaviour (skip) rather than flag everything.
        from src.logic.month_end import find_number_clashes
        to_sync = pd.DataFrame(_tab_lines("KZO-JV1418", [100.0, -100.0]))
        self.assertEqual(find_number_clashes(to_sync, {"KZO-JV1418"}, {}), set())

    def test_source_updates_mark_only_posted_journals(self):
        # A clashed journal keeps 'Transformed <JV>' in the source: sync never writes ERROR there
        # (spec 6.6), so nothing renumbers it without a person deciding.
        from src.logic.month_end import sync_status_updates
        grid = ([HEADER] + reclass_lines(status="Transformed KZO-JV1418")
                + [line(memo="b", amount=1, status="Transformed KZO-JV1419")]
                + [line(memo="c", amount=1, status="Synced KZO-JV1300")])
        updates = sync_status_updates(read_source_tab(grid), {"KZO-JV1418", "KZO-JV1300"})
        by_row = {u["row"]: u["val"] for u in updates}
        self.assertEqual(by_row, {r: "Synced KZO-JV1418" for r in range(2, 11)})
        self.assertEqual({u["col"] for u in updates}, {9})

    def test_journals_already_synced_in_the_tab(self):
        from src.logic.month_end import synced_journal_nos
        df = pd.DataFrame(_tab_lines("KZO-JV1400", [1, -1], remarks="Synced at 2026-10-31 10:00:00")
                          + _tab_lines("KZO-JV1401", [1, -1], remarks="Skipper (Already synced in QBO at x)")
                          + _tab_lines("KZO-JV1402", [1, -1]))
        self.assertEqual(synced_journal_nos(df), {"KZO-JV1400", "KZO-JV1401"})

    def test_get_journal_details_reads_date_and_memo(self):
        from unittest.mock import MagicMock
        from src.logic.syncing import QBOSync
        engine = QBOSync.__new__(QBOSync)
        engine.client = MagicMock()
        engine.client.query.return_value = [
            {"DocNumber": "KZO-JV1418", "TxnDate": "2026-10-31", "PrivateNote": MEMO},
            {"DocNumber": "KZO-JV1418", "TxnDate": "2026-09-30"},  # no memo in QBO
        ]
        details = engine.get_journal_details(["KZO-JV1418"])
        self.assertEqual(details, {"KZO-JV1418": [("2026-10-31", MEMO), ("2026-09-30", "")]})
        query = engine.client.query.call_args.args[0]
        self.assertIn("SELECT DocNumber, TxnDate, PrivateNote FROM JournalEntry", query)
        self.assertIn("WHERE DocNumber IN ('KZO-JV1418')", query)


class ProcessClientSyncTests(unittest.TestCase):
    """run_syncing.process_client_sync with Sheets and QBO stubbed."""

    def _sync(self, country, tab_df, existing, qbo_details):
        from unittest.mock import MagicMock, patch
        from run_syncing import process_client_sync

        ctrl = pd.DataFrame([{"Country": country, "Source File": SOURCE, "Tab Name": TAB, "Month": "2026-10-01",
                              "Transform File": TRANSFORM, "QBO Sync": "SYNC NOW"}],
                            columns=CTRL_COLUMNS + ["QBO Expense", "QBO Transfer", "Last Sync At"]).fillna("")
        gs = MagicMock()
        gs.read_as_df.return_value = ctrl
        gs.read_as_df_sync.side_effect = lambda url, tab: tab_df if tab.endswith("- Journals") else pd.DataFrame()
        source_grid = [HEADER] + reclass_lines(status="Transformed KZO-JV1418") + \
            [line(memo="b", amount=1, location="TH", status="Transformed KZO-JV1400"),
             line(memo="b", amount=-1, location="PK", status="Transformed KZO-JV1400")]
        gs.read_values.side_effect = lambda url, tab, value_render_option="FORMATTED_VALUE": source_grid
        updater = MagicMock()
        with patch("run_syncing.QBOSync") as sync_cls, \
                patch("run_syncing.AsyncSheetUpdater", return_value=updater), \
                patch("run_syncing._build_status_col_ctx", return_value={}), \
                patch("run_syncing._throttle_qbo_call"):
            engine = sync_cls.return_value
            engine.get_existing_duplicates.return_value = set(existing)
            engine.get_journal_details.return_value = qbo_details
            engine.push_journal.return_value = {"JournalEntry": {"Id": "77"}}
            engine.build_qbo_url.return_value = "link"
            process_client_sync(gs, MagicMock(), "CTRL", "KZO", "realm")
        remarks = {}
        for c in updater.enqueue.call_args_list:
            remarks.setdefault(tab_df.loc[c.args[0]["row_idx"], "Journal No"], set()).add(c.args[0]["status"])
        source_writes = {u["row"]: u["val"] for c in gs.batch_update_cells.call_args_list
                         if c.args[0] == SOURCE for u in c.args[2]}
        return engine, remarks, source_writes, _control_updates_sync(gs)

    def test_month_end_clash_is_stopped_and_the_rest_is_posted(self):
        tab_df = pd.DataFrame(_tab_lines("KZO-JV1418", [amt for _, amt in KZO_RECLASS])
                              + _tab_lines("KZO-JV1400", [1.0, -1.0]))
        engine, remarks, source_writes, control = self._sync(
            "Month-end JV", tab_df, existing={"KZO-JV1400"},
            qbo_details={"KZO-JV1400": [("2026-10-31", "Received from Gpay-Native XOJILI")]},
        )
        engine.push_journal.assert_called_once()
        self.assertEqual(engine.push_journal.call_args.args[0], "KZO-JV1418")
        self.assertEqual(remarks["KZO-JV1400"], {"ERROR: KZO-JV1400 is already used in QBO by a different journal"})
        self.assertEqual(source_writes, {r: "Synced KZO-JV1418" for r in range(2, 11)})
        self.assertEqual(control["QBO Journal"], "SYNC FAIL")
        self.assertEqual(control["QBO Sync"], "PARTIAL ERROR")

    def test_month_end_journal_already_in_qbo_with_same_date_and_memo_is_marked_synced(self):
        tab_df = pd.DataFrame(_tab_lines("KZO-JV1400", [1.0, -1.0]))
        engine, remarks, source_writes, control = self._sync(
            "Month-end JV", tab_df, existing={"KZO-JV1400"},
            qbo_details={"KZO-JV1400": [("2026-10-31", MEMO)]},
        )
        engine.push_journal.assert_not_called()
        self.assertTrue(all(s.startswith("Skipper") for s in remarks["KZO-JV1400"]))
        self.assertEqual({source_writes[11], source_writes[12]}, {"Synced KZO-JV1400"})
        self.assertEqual(control["QBO Journal"], "SYNCED")

    def test_normal_rows_keep_todays_sync(self):
        tab_df = pd.DataFrame(_tab_lines("KZO-JV1400", [1.0, -1.0]))
        engine, remarks, source_writes, _ = self._sync(
            "TH", tab_df, existing={"KZO-JV1400"}, qbo_details={"KZO-JV1400": [("2026-01-01", "x")]},
        )
        engine.get_journal_details.assert_not_called()
        self.assertTrue(all(s.startswith("Skipper") for s in remarks["KZO-JV1400"]))
        self.assertEqual(source_writes, {})


class ReconcileTests(unittest.TestCase):
    def _reconcile(self, country):
        from unittest.mock import MagicMock, patch
        from run_reconciliation import process_client_reconcile

        ctrl = pd.DataFrame([{"Country": country, "Source File": SOURCE, "Tab Name": TAB, "Month": "2026-10-01",
                              "Transform File": TRANSFORM, "QBO Reconcile": "RECONCILE NOW"}],
                            columns=CTRL_COLUMNS).fillna("")
        tab_df = pd.DataFrame(_tab_lines("KZO-JV1418", [1.0, -1.0], remarks="Synced at x"))
        gs = MagicMock()
        gs.read_as_df.return_value = ctrl
        gs.read_as_df_sync.side_effect = lambda url, tab: tab_df if tab.endswith("- Journals") else pd.DataFrame()
        with patch("run_reconciliation.Reconciler") as rec_cls, \
                patch("run_reconciliation.RECONCILE_ENABLE_RAW_CHECK", True):
            rec_cls.return_value.reconcile_journals.return_value = [{"row_idx": 0, "status": "Matched"}]
            process_client_reconcile(gs, MagicMock(), "CTRL", "KZO", "realm")
        return gs, rec_cls.return_value

    def test_month_end_row_skips_the_raw_check_but_reconciles_with_qbo(self):
        gs, reconciler = self._reconcile("Month-end JV")
        source_reads = [c for c in gs.read_as_df.call_args_list if c.args[0] == SOURCE]
        self.assertEqual(source_reads, [])
        reconciler.reconcile_journals.assert_called_once()
        reconciler.reconcile_raw_vs_transform.assert_not_called()
        tab_read = [c.args[1] for c in gs.read_as_df_sync.call_args_list]
        self.assertIn("Month-end JV Oct 26 - Journals", tab_read)

    def test_country_rows_still_read_the_raw_tab(self):
        gs, _ = self._reconcile("TH")
        self.assertTrue(any(c.args[0] == SOURCE for c in gs.read_as_df.call_args_list))


def _control_updates_sync(gs):
    cols = CTRL_COLUMNS + ["QBO Expense", "QBO Transfer", "Last Sync At"]
    merged = {}
    for c in gs.batch_update_cells.call_args_list:
        if c.args[0] == "CTRL":
            for u in c.args[2]:
                merged[cols[u["col"] - 1]] = u["val"]
    return merged


class ReadTabValuesTests(unittest.TestCase):
    def _client(self, worksheet):
        from unittest.mock import MagicMock
        from src.connectors.gsheets_client import GSheetsClient
        client = GSheetsClient.__new__(GSheetsClient)
        sheet = MagicMock()
        sheet.worksheet.side_effect = worksheet
        client.open = MagicMock(return_value=sheet)
        return client

    def test_missing_tab_is_none(self):
        import gspread

        def worksheet(name):
            raise gspread.exceptions.WorksheetNotFound(name)
        self.assertIsNone(self._client(worksheet).read_tab_values("url", "tab"))

    def test_other_errors_are_raised_not_hidden(self):
        def worksheet(name):
            raise RuntimeError("connection reset")
        with self.assertRaises(RuntimeError):
            self._client(worksheet).read_tab_values("url", "tab")

    def test_existing_tab_returns_its_grid(self):
        from unittest.mock import MagicMock
        ws = MagicMock()
        ws.get_all_values.return_value = [["No", "Journal No"]]
        self.assertEqual(self._client(lambda name: ws).read_tab_values("url", "tab"), [["No", "Journal No"]])


class ReserveJournalNumbersTests(unittest.TestCase):
    """Normal rows: Last Journal No is saved right after numbering, before the Transform write."""

    def test_writes_the_rows_last_number_and_raises_the_run_counter(self):
        from unittest.mock import MagicMock
        from run_ingestion import _reserve_journal_numbers
        gs = MagicMock()
        ctrl = _ctrl()
        self.assertEqual(_reserve_journal_numbers(gs, "CTRL", ctrl, 2, 1425, 1417), 1425)
        self.assertEqual(_control_updates(gs), {"Last Journal No": "1425"})

    def test_run_counter_never_moves_back(self):
        # A later row in the same run that minted nothing must not lower the counter that the
        # next row starts from.
        from unittest.mock import MagicMock
        from run_ingestion import _reserve_journal_numbers
        self.assertEqual(_reserve_journal_numbers(MagicMock(), "CTRL", _ctrl(), 2, 1400, 1417), 1417)


if __name__ == "__main__":
    unittest.main()
