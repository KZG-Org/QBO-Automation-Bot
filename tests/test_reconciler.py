import unittest
from unittest.mock import MagicMock

import pandas as pd

from src.logic.reconciler import Reconciler


class RawVsTransformEstNoTests(unittest.TestCase):
    def setUp(self):
        self.reconciler = Reconciler(MagicMock())

    def _run(self, raw_rows, transform_rows, entity_type="JournalEntry", client_name="KZO"):
        raw_df = pd.DataFrame(raw_rows)
        transform_df = pd.DataFrame(transform_rows)
        return self.reconciler.reconcile_raw_vs_transform(raw_df, transform_df, entity_type, client_name)

    def test_shifted_row_gets_a_single_est_no(self):
        # Transform captured No=100 for a $50 tx on 2026-07-31. A row was inserted above it
        # in raw, so that same tx now lives at No=101; No=100 in raw is a different, unrelated tx.
        raw_rows = [
            {"No": 100, "Date": "2026-07-31", "USD - QBO": 999.00},
            {"No": 101, "Date": "2026-07-31", "USD - QBO": 50.00},
        ]
        transform_rows = [
            {"No": 100, "Date": "2026-07-31", "Amount": 50.00, "Account": "Bank"},
        ]

        updates = self._run(raw_rows, transform_rows)

        self.assertEqual(len(updates), 1)
        self.assertIn("Unmatched: Amt Diff", updates[0]["status"])
        self.assertIn("est. No: 101", updates[0]["status"])

    def test_no_candidate_reports_not_found(self):
        raw_rows = [
            {"No": 100, "Date": "2026-07-31", "USD - QBO": 999.00},
        ]
        transform_rows = [
            {"No": 100, "Date": "2026-07-31", "Amount": 50.00, "Account": "Bank"},
        ]

        updates = self._run(raw_rows, transform_rows)

        self.assertIn("est. No: not found", updates[0]["status"])

    def test_multiple_candidates_prefer_the_closest_no(self):
        # Row shifts only ever move a row by a handful of positions, so when several raw
        # rows match on Date+Amount, the one numerically closest to the old No wins instead
        # of forcing a manual "ambiguous" look.
        raw_rows = [
            {"No": 7310184, "Date": "2026-07-31", "USD - QBO": 50.00},
            {"No": 7310193, "Date": "2026-07-31", "USD - QBO": 50.00},
        ]
        transform_rows = [
            {"No": 7310192, "Date": "2026-07-31", "Amount": 50.00, "Account": "Bank"},
        ]

        updates = self._run(raw_rows, transform_rows)

        self.assertIn("est. No: 7310193", updates[0]["status"])
        self.assertNotIn("ambiguous", updates[0]["status"])

    def test_equidistant_candidates_are_still_ambiguous(self):
        raw_rows = [
            {"No": 99, "Date": "2026-07-31", "USD - QBO": 50.00},
            {"No": 100, "Date": "2026-07-31", "USD - QBO": 999.00},
            {"No": 101, "Date": "2026-07-31", "USD - QBO": 50.00},
        ]
        transform_rows = [
            {"No": 100, "Date": "2026-07-31", "Amount": 50.00, "Account": "Bank"},
        ]

        updates = self._run(raw_rows, transform_rows)

        self.assertIn("est. No: ambiguous (99, 101)", updates[0]["status"])

    def test_matched_raw_row_is_not_offered_as_a_candidate(self):
        # Raw No=101 is already the correct (Matched) destination for transform No=101.
        # Transform No=100 is unrelated and Unmatched (raw No=100 holds a different amount);
        # without excluding already-claimed raw rows, No=101 would be wrongly re-offered.
        raw_rows = [
            {"No": 100, "Date": "2026-07-31", "USD - QBO": 999.00},
            {"No": 101, "Date": "2026-07-31", "USD - QBO": 50.00},
        ]
        transform_rows = [
            {"No": 100, "Date": "2026-07-31", "Amount": 50.00, "Account": "Bank"},
            {"No": 101, "Date": "2026-07-31", "Amount": 50.00, "Account": "Bank"},
        ]

        updates = self._run(raw_rows, transform_rows)

        statuses = {u["row_idx"]: u["status"] for u in updates}
        self.assertIn("est. No: not found", statuses[0])
        self.assertEqual(statuses[1], "Matched")

    def test_non_kzo_client_is_unaffected(self):
        raw_rows = [
            {"No": 100, "Date": "2026-07-31", "USD - QBO": 999.00},
            {"No": 101, "Date": "2026-07-31", "USD - QBO": 50.00},
        ]
        transform_rows = [
            {"No": 100, "Date": "2026-07-31", "Amount": 50.00, "Account": "Bank"},
        ]

        updates = self._run(raw_rows, transform_rows, client_name="KZP")

        self.assertEqual(updates[0]["status"], "Unmatched: Amt Diff (999.00 vs 50.00)")

    def test_excel_serial_raw_dates_are_parsed(self):
        # Raw is read with value_render_option='UNFORMATTED_VALUE', so real raw Date cells
        # arrive as bare Excel serial numbers, not date strings. Naive pd.to_datetime()
        # silently mis-parses a bare number as Unix-epoch nanoseconds instead of erroring,
        # so this must be handled explicitly or every date+amount match silently fails.
        serial = (pd.Timestamp("2026-07-31") - pd.Timestamp("1899-12-30")).days
        raw_rows = [
            {"No": 100, "Date": float(serial), "USD - QBO": 999.00},
            {"No": 101, "Date": float(serial), "USD - QBO": 50.00},
        ]
        transform_rows = [
            {"No": 100, "Date": "2026-07-31", "Amount": 50.00, "Account": "Bank"},
        ]

        updates = self._run(raw_rows, transform_rows)

        self.assertIn("est. No: 101", updates[0]["status"])
        self.assertNotIn("nearby match", updates[0]["status"])

    def test_nearby_no_fallback_when_date_unparseable(self):
        # Real source data isn't always clean; when the raw/transform Date can't be
        # normalized, fall back to scanning Nos near the old No for an amount-only match
        # -- the same manual check an analyst does by hand.
        raw_rows = [
            {"No": 7310129, "Date": "not-a-date", "USD - QBO": 999.00},
            {"No": 7310130, "Date": "not-a-date", "USD - QBO": 111.00},
            {"No": 7310132, "Date": "not-a-date", "USD - QBO": 14398.50},
        ]
        transform_rows = [
            {"No": 7310131, "Date": "not-a-date", "Amount": 14398.50, "Account": "Bank"},
        ]

        updates = self._run(raw_rows, transform_rows)

        self.assertIn("est. No: 7310132", updates[0]["status"])
        self.assertIn("nearby match", updates[0]["status"])

    def test_nearby_no_fallback_respects_window(self):
        # A candidate far outside the neighborhood window should not be offered even if
        # the amount happens to match -- too likely to be an unrelated coincidence.
        raw_rows = [
            {"No": 7310129, "Date": "not-a-date", "USD - QBO": 999.00},
            {"No": 7320500, "Date": "not-a-date", "USD - QBO": 14398.50},
        ]
        transform_rows = [
            {"No": 7310131, "Date": "not-a-date", "Amount": 14398.50, "Account": "Bank"},
        ]

        updates = self._run(raw_rows, transform_rows)

        self.assertIn("est. No: not found", updates[0]["status"])

    def test_missing_client_name_is_unaffected(self):
        raw_rows = [
            {"No": 100, "Date": "2026-07-31", "USD - QBO": 999.00},
            {"No": 101, "Date": "2026-07-31", "USD - QBO": 50.00},
        ]
        transform_rows = [
            {"No": 100, "Date": "2026-07-31", "Amount": 50.00, "Account": "Bank"},
        ]

        updates = self._run(raw_rows, transform_rows, client_name="")

        self.assertEqual(updates[0]["status"], "Unmatched: Amt Diff (999.00 vs 50.00)")


class JournalLineMatchingTests(unittest.TestCase):
    """reconcile_journals pairs each sheet line with one QBO line of the same journal."""

    @staticmethod
    def _line(posting, amount, account, description=""):
        return {"Amount": amount, "DetailType": "JournalEntryLineDetail", "Description": description,
                "JournalEntryLineDetail": {"PostingType": posting, "AccountRef": {"name": account}}}

    def _run(self, sheet_lines, qbo_lines):
        client = MagicMock()
        client.client_name = "KZDW"
        client.query.return_value = [{"Id": "11999", "DocNumber": "KZDW-JV0666", "TxnDate": "2026-09-11", "Line": qbo_lines}]
        df = pd.DataFrame([{"Journal No": "KZDW-JV0666", "Date": "9/11/2026", "Account": line[0], "Amount": line[1],
                            "Memo": line[2] if len(line) > 2 else "", "QBO ID": "11999"}
                           for line in sheet_lines])
        updates = Reconciler(client).reconcile_journals(df, "2026-09-01")
        return [u["status"] for u in sorted(updates, key=lambda u: u["row_idx"])]

    def test_fallback_of_an_earlier_row_does_not_steal_a_later_exact_match(self):
        # KZDW-JV0666 after its TD lines were removed from QBO: rows 2-3 are still in the
        # sheet, and row 3 (FIN OPS 400,000) used to claim QBO's FIN OPS 29,347.18 line through
        # the account-only fallback, leaving the real row 5 with "No matching line".
        statuses = self._run(
            [("KZDW DPP TH 2", -300.0), ("Payment/Banking", 300.0),
             ("Amount Owing (by)/to KZO", -400000.0), ("KZDW FIN OPS TRC 1", 400000.0),
             ("System Fees Rebate from HengSlip", -29347.18), ("KZDW FIN OPS TRC 1", 29347.18)],
            [self._line("Credit", 300.0, "Cash on Hand:KZDW DPP TH 2"),
             self._line("Debit", 300.0, "Miscellaneous:Payment/Banking"),
             self._line("Credit", 29347.18, "DPP:System Fees Rebate from HengSlip"),
             self._line("Debit", 29347.18, "Cash on Hand:KZDW FIN OPS TRC 1")],
        )
        self.assertEqual([statuses[i] for i in (0, 1, 4, 5)], ["✅ Matched"] * 4)
        self.assertTrue(statuses[2].startswith("❌ No matching line"))
        self.assertTrue(statuses[3].startswith("❌ No matching line"))

    def test_fallbacks_still_report_account_and_amount_mismatches(self):
        statuses = self._run(
            [("KZDW DPP TH 2", -300.0), ("Payment/Banking", 250.0), ("Exchange Loss", 50.0)],
            [self._line("Credit", 300.0, "Cash on Hand:KZDW DPP TH 2"),
             self._line("Debit", 250.0, "Miscellaneous:Payment/Banking"),
             self._line("Debit", 50.0, "Payment:Exchange Gain")],
        )
        self.assertEqual(statuses[0], "✅ Matched")
        self.assertEqual(statuses[1], "✅ Matched")
        self.assertTrue(statuses[2].startswith("❌ Account Mismatch"), statuses[2])

    def test_amount_mismatch_is_reported_when_only_the_account_matches(self):
        statuses = self._run(
            [("KZDW DPP TH 2", -300.0), ("Payment/Banking", 310.0)],
            [self._line("Credit", 300.0, "Cash on Hand:KZDW DPP TH 2"),
             self._line("Debit", 300.0, "Miscellaneous:Payment/Banking")],
        )
        self.assertEqual(statuses[0], "✅ Matched")
        self.assertTrue(statuses[1].startswith("❌ Amount Mismatch"), statuses[1])

    def test_amount_mismatch_pairs_with_the_closest_amount(self):
        # Both sheet amounts were edited; each should be compared with its nearest QBO line,
        # not with whichever same-account line happens to come first in QBO.
        statuses = self._run(
            [("KZDW FIN OPS TRC 1", 29000.0), ("KZDW FIN OPS TRC 1", 400000.0)],
            [self._line("Debit", 399000.0, "Cash on Hand:KZDW FIN OPS TRC 1"),
             self._line("Debit", 29347.18, "Cash on Hand:KZDW FIN OPS TRC 1")],
        )
        self.assertEqual(statuses, ["❌ Amount Mismatch (Sheet: 29000.0 vs QBO: 29347.18)",
                                    "❌ Amount Mismatch (Sheet: 400000.0 vs QBO: 399000.0)"])

    def test_account_mismatch_prefers_the_line_with_the_same_description(self):
        statuses = self._run(
            [("Exchange Loss", 50.0, "Weekly exchange loss from TH PGW")],
            [self._line("Debit", 50.0, "Payment:Bank Charges", "Transfer service fee"),
             self._line("Debit", 50.0, "Payment:Exchange Gain", "Weekly exchange loss from TH PGW")],
        )
        self.assertEqual(statuses, ["❌ Account Mismatch (Sheet: 'Exchange Loss' vs QBO: 'exchange gain')"])


if __name__ == "__main__":
    unittest.main()
