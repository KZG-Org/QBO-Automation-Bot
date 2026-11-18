import unittest

from src.logic.account_map import (
    AccountResolver,
    MapRow,
    normalize_qbo_id,
    resolve_account,
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


if __name__ == "__main__":
    unittest.main()
