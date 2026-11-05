import unittest
from unittest.mock import MagicMock

import pandas as pd

from src.connectors.gsheets_client import GSheetsClient


def _client_with_tab(grid, row_count=1000):
    """A GSheetsClient whose open() returns a spreadsheet holding one existing tab."""
    gs = GSheetsClient.__new__(GSheetsClient)  # skip OAuth
    sh = MagicMock()
    sh.id = "sid"
    ws = MagicMock()
    ws.row_count = row_count
    ws.get_all_values.return_value = grid
    sh.worksheet.return_value = ws
    gs.open = MagicMock(return_value=sh)
    return gs, ws


class AppendBelowLastRowTests(unittest.TestCase):
    """S5 Oct 26: Sheets' append found a 'table' that ended early and wrote JV0267 over two
    existing journal lines (and put T0030 in the middle of Transfers on 4 Nov). New rows now
    go straight below the last row that holds anything."""

    DF = pd.DataFrame([{"No": 1006003, "Journal No": "S5-JV0267"}, {"No": 1006003, "Journal No": "S5-JV0267"}])

    def test_rows_go_below_the_last_used_row_even_with_a_blank_row_inside(self):
        grid = [["No", "Journal No"], ["1030012", "S5-JV0264"], ["", ""], ["1031001", "S5-JV0265"]]
        gs, ws = _client_with_tab(grid)

        gs.append_or_create_df("sid", "S5 Oct 26 - Journals", self.DF)

        ws.append_rows.assert_not_called()
        ws.add_rows.assert_not_called()
        ws.update.assert_called_once()
        kwargs = ws.update.call_args.kwargs
        self.assertEqual(kwargs["range_name"], "A5")
        self.assertEqual(kwargs["values"], [[1006003, "S5-JV0267"], [1006003, "S5-JV0267"]])
        self.assertEqual(kwargs["value_input_option"], "USER_ENTERED")

    def test_grid_rows_are_added_when_the_tab_is_full(self):
        grid = [["No", "Journal No"], ["1", "a"], ["2", "b"]]
        gs, ws = _client_with_tab(grid, row_count=4)

        gs.append_or_create_df("sid", "tab", self.DF)

        ws.add_rows.assert_called_once_with(1)  # rows 4 and 5 are needed, the grid has 4
        self.assertEqual(ws.update.call_args.kwargs["range_name"], "A4")

    def test_a_fully_empty_tab_is_written_from_row_1(self):
        # gspread returns [[]] for a tab with no values at all.
        gs, ws = _client_with_tab([[]])

        gs.append_or_create_df("sid", "tab", self.DF)

        self.assertEqual(ws.update.call_args.kwargs["range_name"], "A1")

    def test_empty_frame_writes_nothing(self):
        gs, ws = _client_with_tab([["No"]])

        gs.append_or_create_df("sid", "tab", pd.DataFrame())

        gs.open.assert_not_called()
        ws.update.assert_not_called()


if __name__ == "__main__":
    unittest.main()
