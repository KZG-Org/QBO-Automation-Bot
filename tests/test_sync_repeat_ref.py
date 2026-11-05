import unittest
from unittest.mock import MagicMock, patch

import pandas as pd

import run_syncing
from config import settings

TRANSFORM = "https://docs.google.com/spreadsheets/d/transform"
EXP_TAB, TR_TAB = "S5 Oct 26 - Expenses", "S5 Oct 26 - Transfers"


class _FakeUpdater:
    """Stands in for AsyncSheetUpdater: records what each row's Remarks would become."""

    written: dict = {}

    def __init__(self, gs, spreadsheet_url, tab_name, col_ctx, batch_size):
        self.tab = tab_name

    def enqueue(self, item):
        _FakeUpdater.written[(self.tab, item["row_idx"])] = item

    def close(self):
        pass


def _run_sync(exp_rows, tr_rows=(), push_expense=None):
    """Run process_client_sync for one S5 Oct row; returns (engine, row writes, final control update)."""
    ctrl = pd.DataFrame([{
        settings.CTRL_COL_COUNTRY: "S5", settings.CTRL_COL_TRANSFORM_URL: TRANSFORM,
        settings.CTRL_COL_MONTH: "2026-10-01", settings.CTRL_COL_TAB_NAME: "Transaction Records (NEW)",
        settings.CTRL_COL_QBO_SYNC: "SYNC NOW",
    }])
    tabs = {"S5 Oct 26 - Journals": pd.DataFrame(), EXP_TAB: pd.DataFrame(list(exp_rows)), TR_TAB: pd.DataFrame(list(tr_rows))}
    gs = MagicMock()
    gs.read_as_df.return_value = ctrl
    gs.read_as_df_sync.side_effect = lambda url, tab: tabs[tab]

    ids = iter(range(5000, 5100))
    engine = MagicMock()
    engine.get_existing_duplicates.return_value = set()
    engine.push_expense.side_effect = push_expense or (lambda ref, row: {"Purchase": {"Id": str(next(ids))}})
    engine.push_transfer.side_effect = lambda row: {"Transfer": {"Id": str(next(ids))}}
    engine.build_qbo_url.return_value = ""

    control_updates = []
    _FakeUpdater.written = {}
    with patch.object(run_syncing, "QBOSync", return_value=engine), \
         patch.object(run_syncing, "AsyncSheetUpdater", _FakeUpdater), \
         patch.object(run_syncing, "_build_status_col_ctx", return_value={}), \
         patch.object(run_syncing, "_throttle_qbo_call"), \
         patch.object(run_syncing, "_batch_update_control",
                      side_effect=lambda gs, sid, tab, row, cols, upd: control_updates.append(upd)):
        run_syncing.process_client_sync(gs, MagicMock(), "control", "S5", "realm")
    return engine, _FakeUpdater.written, control_updates[-1]


def _exp(no, ref):
    return {"No": no, "Exp Ref. No": ref, "Remarks": "Ready to sync"}


def _tr(no, ref):
    return {"No": no, "Ref No": ref, "Remarks": "Ready to sync"}


class RepeatRefInOneSyncTests(unittest.TestCase):
    """S5 Oct 26: copies of the same row sat side by side as 'Ready to sync'. The QBO check
    runs once before the batch, so every copy was posted. One sync now posts a Ref No once."""

    def test_a_copied_row_is_posted_once(self):
        engine, written, final = _run_sync(
            [_exp(1030035, "S51026E0207"), _exp(1030036, "S51026E0208"), _exp(1030035, "S51026E0207")],
            [_tr(1030384, "S51026T0030"), _tr(1030384, "S51026T0030")],
        )

        self.assertEqual([c.args[0] for c in engine.push_expense.call_args_list], ["S51026E0207", "S51026E0208"])
        self.assertEqual(engine.push_transfer.call_count, 1)
        for item in (written[(EXP_TAB, 2)], written[(TR_TAB, 1)]):
            self.assertTrue(item["status"].startswith("Skipper (Duplicate row"), item["status"])
            self.assertEqual(item["qbo_id"], "")
        self.assertTrue(written[(EXP_TAB, 0)]["status"].startswith("Synced at"))
        self.assertEqual(final[settings.CTRL_COL_QBO_SYNC], "PARTIAL ERROR")  # someone must delete the copy

    def test_a_ref_reused_by_a_different_row_is_held_not_skipped(self):
        # A stale counter can give a new transaction an old Ref No. Skipping it as "already
        # synced" would lose that transaction without a word.
        engine, written, final = _run_sync([_exp(1001003, "S51026E0001"), _exp(1001004, "S51026E0001")])

        self.assertEqual(engine.push_expense.call_count, 1)
        status = written[(EXP_TAB, 1)]["status"]
        self.assertIn("S51026E0001", status)
        self.assertIn("1001003", status)
        # Must not look like a row to retry, re-sync or ignore, or it loops or vanishes.
        for word in ("ERROR", "Ready to sync", "Unbalance", "Skipper"):
            self.assertNotIn(word.lower(), status.lower())
        self.assertEqual(final[settings.CTRL_COL_QBO_SYNC], "PARTIAL ERROR")

    def test_distinct_refs_sync_normally(self):
        engine, _, final = _run_sync([_exp(1, "E1"), _exp(2, "E2")], [_tr(3, "T1")])

        self.assertEqual(engine.push_expense.call_count, 2)
        self.assertEqual(engine.push_transfer.call_count, 1)
        self.assertEqual(final[settings.CTRL_COL_QBO_SYNC], "DONE")

    def test_only_a_successful_post_counts(self):
        # If the first copy failed to post, the next copy may still post.
        calls = []

        def flaky(ref, row):
            calls.append(ref)
            if len(calls) == 1:
                raise RuntimeError("QBO 500")
            return {"Purchase": {"Id": "9"}}

        _, written, _ = _run_sync([_exp(1, "E1"), _exp(1, "E1")], push_expense=flaky)

        self.assertEqual(calls, ["E1", "E1"])
        self.assertTrue(written[(EXP_TAB, 0)]["status"].startswith("ERROR"))
        self.assertTrue(written[(EXP_TAB, 1)]["status"].startswith("Synced at"))


if __name__ == "__main__":
    unittest.main()
