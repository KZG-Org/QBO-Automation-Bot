import unittest
from unittest.mock import Mock, call, patch

import pandas as pd

import run_ingestion


class KzpSourceHeaderTests(unittest.TestCase):
    REQUIRED_COLUMNS = ["Date", "From Account", "USD - QBO", "QBO Import", "No"]

    def test_reads_new_kzp_layout_from_row_five(self):
        expected = pd.DataFrame(columns=self.REQUIRED_COLUMNS)
        gs = Mock()
        gs.read_as_df.return_value = expected

        result = run_ingestion._read_source_raw_df(gs, "source", "Aug 26", "KZP")

        self.assertIs(result, expected)
        gs.read_as_df.assert_called_once_with(
            "source",
            "Aug 26",
            header_row=5,
            value_render_option="UNFORMATTED_VALUE",
        )

    def test_falls_back_to_legacy_kzp_header_row_four(self):
        invalid_row_five = pd.DataFrame(columns=["", "Unnamed"])
        expected = pd.DataFrame(columns=self.REQUIRED_COLUMNS[:-1])
        gs = Mock()
        gs.read_as_df.side_effect = [invalid_row_five, expected]

        result = run_ingestion._read_source_raw_df(gs, "source", "Jul 26", "KZP")

        self.assertIs(result, expected)
        self.assertEqual(
            gs.read_as_df.call_args_list,
            [
                call(
                    "source",
                    "Jul 26",
                    header_row=5,
                    value_render_option="UNFORMATTED_VALUE",
                ),
                call(
                    "source",
                    "Jul 26",
                    header_row=4,
                    value_render_option="UNFORMATTED_VALUE",
                ),
            ],
        )

    def test_rejects_unrecognized_kzp_headers(self):
        gs = Mock()
        gs.read_as_df.return_value = pd.DataFrame(columns=["", "Unexpected"])

        with self.assertRaisesRegex(ValueError, "KZP raw header not found"):
            run_ingestion._read_source_raw_df(gs, "source", "Aug 26", "KZP")

        self.assertEqual(gs.read_as_df.call_count, 2)


class SourceHeaderRoutingTests(unittest.TestCase):
    """S5 locates its header row; every other client keeps its fixed row."""

    def test_s5_is_routed_to_the_dynamic_header_reader(self):
        expected = pd.DataFrame(columns=["CO", "Date", "Category"])
        gs = Mock()

        with patch.object(run_ingestion, "read_s5_raw_df", return_value=expected) as reader:
            result = run_ingestion._read_source_raw_df(gs, "source", "Records", "S5")

        self.assertIs(result, expected)
        reader.assert_called_once_with(gs, "source", "Records", "S5")
        gs.read_as_df.assert_not_called()

    def test_other_clients_keep_their_fixed_header_rows(self):
        for client_name, header_row in (("KZDW", 5), ("TINDERPAY", 5), ("UMBER", 4), ("KZO", 1)):
            with self.subTest(client=client_name):
                gs = Mock()
                gs.read_as_df.return_value = pd.DataFrame()

                run_ingestion._read_source_raw_df(gs, "source", "Aug 26", client_name)

                gs.read_as_df.assert_called_once_with(
                    "source",
                    "Aug 26",
                    header_row=header_row,
                    value_render_option="UNFORMATTED_VALUE",
                )


class KzdwCoyFilterTests(unittest.TestCase):
    def test_only_dpp_is_kept_by_default(self):
        raw_df = pd.DataFrame({"COY": ["DPP", " dpp ", "TD", "KZDW", "", None]})

        kept, dropped = run_ingestion._filter_kzdw_coy(raw_df, "KZDW")

        self.assertEqual(run_ingestion.KZDW_ALLOWED_COY_VALUES, {"DPP"})
        self.assertEqual(kept.index.tolist(), [0, 1])
        self.assertEqual(dropped, {"TD": 1, "KZDW": 1, "(blank)": 2})

    def test_nothing_dropped_when_every_row_is_dpp(self):
        raw_df = pd.DataFrame({"COY": ["DPP", "DPP"]}, index=[4, 9])

        kept, dropped = run_ingestion._filter_kzdw_coy(raw_df, "KZDW")

        self.assertEqual(kept.index.tolist(), [4, 9])
        self.assertEqual(dropped, {})

    def test_other_clients_are_not_filtered(self):
        raw_df = pd.DataFrame({"COY": ["TD", "KZO", ""]}, index=[4, 9, 11])

        kept, dropped = run_ingestion._filter_kzdw_coy(raw_df, "KZO")

        self.assertIs(kept, raw_df)
        self.assertEqual(dropped, {})

    def test_missing_coy_column_drops_every_kzdw_row(self):
        # No COY means no proof a row is DPP; posting it could double-post a TD row.
        raw_df = pd.DataFrame({"No": [1, 2]})

        kept, dropped = run_ingestion._filter_kzdw_coy(raw_df, "KZDW")

        self.assertTrue(kept.empty)
        self.assertEqual(dropped, {"(blank)": 2})


class DecodeKzoNoTests(unittest.TestCase):
    def test_single_digit_month_decodes_from_the_user_example(self):
        # No=7310132 -> date_str "731" (Jul 31) + run_cnt "0132" (132nd same-date row).
        self.assertEqual(run_ingestion._decode_kzo_no(7310132, expected_month=7), "7/31 seq #132")

    def test_two_digit_month_decodes(self):
        # No=1225045 -> date_str "1225" (Dec 25) + run_cnt "045" (45th same-date row).
        self.assertEqual(run_ingestion._decode_kzo_no(1225045, expected_month=12), "12/25 seq #45")

    def test_ambiguous_prefix_prefers_expected_month(self):
        # "1201234": 3-char reading = Jan 20 (run_cnt 1234); 4-char reading = Dec 1 (run_cnt 234).
        self.assertEqual(run_ingestion._decode_kzo_no(1201234, expected_month=12), "12/1 seq #234")
        self.assertEqual(run_ingestion._decode_kzo_no(1201234, expected_month=1), "1/20 seq #1234")

    def test_ambiguous_prefix_without_expected_month_uses_first_candidate(self):
        self.assertEqual(run_ingestion._decode_kzo_no(1201234, expected_month=None), "1/20 seq #1234")

    def test_wrong_length_is_undecodable(self):
        self.assertIsNone(run_ingestion._decode_kzo_no(123, expected_month=7))
        self.assertIsNone(run_ingestion._decode_kzo_no(12345678, expected_month=7))


if __name__ == "__main__":
    unittest.main()
