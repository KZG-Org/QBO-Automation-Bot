import unittest
from unittest.mock import MagicMock

from src.connectors.gsheets_client import GSheetsClient


def _client_with_tab(sheet_id=42):
    """A GSheetsClient whose open() returns a spreadsheet stub with one worksheet."""
    gs = GSheetsClient.__new__(GSheetsClient)  # skip OAuth
    sh = MagicMock()
    ws = MagicMock()
    ws.id = sheet_id
    sh.worksheet.return_value = ws
    gs.open = MagicMock(return_value=sh)
    return gs, sh, ws


class DeleteRowsTests(unittest.TestCase):
    """S5 Oct 26: 392 one-by-one deletes hit the Sheets write quota, every failure was
    swallowed, and the retry path appended rebuilt rows next to the undeleted ones."""

    def test_all_rows_go_in_one_batch_update_bottom_up(self):
        gs, sh, ws = _client_with_tab()

        gs.delete_rows("sid", "Expenses", [5, 3, 4, 10, 4])

        sh.batch_update.assert_called_once()
        ws.delete_rows.assert_not_called()
        ranges = [r["deleteDimension"]["range"] for r in sh.batch_update.call_args[0][0]["requests"]]
        # Rows 3-5 merge into one range; ranges run bottom-up so earlier deletes never shift later ones.
        self.assertEqual(
            [(r["sheetId"], r["dimension"], r["startIndex"], r["endIndex"]) for r in ranges],
            [(42, "ROWS", 9, 10), (42, "ROWS", 2, 5)],
        )

    def test_failure_is_raised_not_swallowed(self):
        gs, sh, _ = _client_with_tab()
        sh.batch_update.side_effect = RuntimeError("429 Quota exceeded")

        with self.assertRaises(RuntimeError):
            gs.delete_rows("sid", "Expenses", [2, 3])

    def test_several_tabs_share_one_batch_update(self):
        gs = GSheetsClient.__new__(GSheetsClient)
        sh = MagicMock()
        sheets = {"Journals": MagicMock(id=1), "Expenses": MagicMock(id=2)}
        sh.worksheet.side_effect = lambda name: sheets[name]
        gs.open = MagicMock(return_value=sh)

        gs.delete_rows_by_tab("sid", {"Journals": [2, 3], "Expenses": [7]})

        sh.batch_update.assert_called_once()
        ranges = [r["deleteDimension"]["range"] for r in sh.batch_update.call_args[0][0]["requests"]]
        self.assertEqual(
            [(r["sheetId"], r["startIndex"], r["endIndex"]) for r in ranges],
            [(1, 1, 3), (2, 6, 7)],
        )

    def test_no_rows_makes_no_call(self):
        gs, sh, _ = _client_with_tab()

        gs.delete_rows("sid", "Expenses", [])

        gs.open.assert_not_called()
        sh.batch_update.assert_not_called()


if __name__ == "__main__":
    unittest.main()
