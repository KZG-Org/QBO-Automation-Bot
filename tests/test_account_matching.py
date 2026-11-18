import unittest
from unittest.mock import MagicMock, patch

from src.logic.reconciler import Reconciler
from src.logic.syncing import QBOSync
from src.logic.transformer import find_id_in_map

# Real KZO fully-qualified account names (as stored in the QBO mappings dict), trimmed to the
# "Investment:" parent that exposes the sibling-collision problem.
KZO_INVESTMENT_ACCOUNTS = {
    "Investment:Investment in Anjouan": "1150040014",
    "Investment:Investment in Consultancy Company (ZA)": "1150040041",
    "Investment:Investment in HR Company": "1150040017",
    "Investment:Investment in HR Company (MP)": "1150040042",
    "Investment:Investment in HR Company (ORZ)": "1150040132",
    "Investment:Investment in HR Company (OSR)": "1150040039",
    "Investment:Investment in SA": "1150040074",
    "Investment:Investment in Umber": "1150040019",
}


class AccountMatchingTests(unittest.TestCase):
    def test_leaf_match_resolves_the_exact_sibling(self):
        acc_id = find_id_in_map(
            KZO_INVESTMENT_ACCOUNTS, "Investment in HR Company (ORZ)", allow_fuzzy=False
        )
        self.assertEqual(acc_id, "1150040132")

    def test_each_sibling_resolves_to_its_own_id(self):
        for fqn, expected_id in KZO_INVESTMENT_ACCOUNTS.items():
            leaf = fqn.split(":")[-1]
            with self.subTest(account=leaf):
                self.assertEqual(
                    find_id_in_map(KZO_INVESTMENT_ACCOUNTS, leaf, allow_fuzzy=False), expected_id
                )

    def test_exact_fully_qualified_name_resolves(self):
        acc_id = find_id_in_map(
            KZO_INVESTMENT_ACCOUNTS, "Investment:Investment in HR Company (MP)", allow_fuzzy=False
        )
        self.assertEqual(acc_id, "1150040042")

    def test_missing_account_returns_none_instead_of_a_sibling(self):
        # The regression: with the (ORZ) account absent, fuzzy at cutoff 0.80 scores
        # "Investment:Investment in HR Company (OSR)" at 0.817 and would silently book the
        # money to the wrong company. Accounts must fail instead of guessing.
        without_orz = {
            k: v for k, v in KZO_INVESTMENT_ACCOUNTS.items() if "(ORZ)" not in k
        }

        acc_id = find_id_in_map(without_orz, "Investment in HR Company (ORZ)", allow_fuzzy=False)

        self.assertIsNone(acc_id)

    def test_fuzzy_would_have_picked_the_wrong_sibling(self):
        # Documents exactly what the old behavior did, so the guard above can't be dropped
        # without this failing.
        without_orz = {
            k: v for k, v in KZO_INVESTMENT_ACCOUNTS.items() if "(ORZ)" not in k
        }

        fuzzy_id = find_id_in_map(without_orz, "Investment in HR Company (ORZ)", allow_fuzzy=True)

        self.assertEqual(fuzzy_id, "1150040039")  # (OSR) -- wrong account

    def test_case_and_whitespace_are_normalized(self):
        acc_id = find_id_in_map(
            KZO_INVESTMENT_ACCOUNTS, "  investment  in   hr company (orz) ", allow_fuzzy=False
        )
        self.assertEqual(acc_id, "1150040132")

    def test_hardcoded_replacements_still_apply_without_fuzzy(self):
        mapping = {"Bank:KZO CBD Z": "900"}

        self.assertEqual(find_id_in_map(mapping, "CBD Z Card", allow_fuzzy=False), "900")

    def test_fuzzy_still_available_for_non_account_maps(self):
        # Vendors/classes/locations keep fuzzy -- names there genuinely vary and a wrong
        # guess does not misstate the books.
        vendors = {"Acme Trading Company": "5"}

        self.assertEqual(find_id_in_map(vendors, "Acme Trading Compny", allow_fuzzy=True), "5")

    def test_nested_account_matches_both_suffix_forms(self):
        # KZO's only 3-level account. Analysts write either the bare leaf or the
        # parent-qualified form, so both must resolve.
        nested = {"Marketing:RnD:AI Expenses": "700", "Marketing:Growth": "701"}

        self.assertEqual(find_id_in_map(nested, "AI Expenses", allow_fuzzy=False), "700")
        self.assertEqual(find_id_in_map(nested, "RnD:AI Expenses", allow_fuzzy=False), "700")
        self.assertEqual(find_id_in_map(nested, "Growth", allow_fuzzy=False), "701")

    def test_full_path_still_matches_exactly(self):
        nested = {"Marketing:RnD:AI Expenses": "700"}
        self.assertEqual(
            find_id_in_map(nested, "Marketing:RnD:AI Expenses", allow_fuzzy=False), "700"
        )

    def test_partial_middle_segment_does_not_match(self):
        # "RnD" alone is a parent fragment, not a trailing path -- must not resolve.
        nested = {"Marketing:RnD:AI Expenses": "700"}
        self.assertIsNone(find_id_in_map(nested, "RnD", allow_fuzzy=False))

    def test_same_leaf_under_two_parents_is_refused_for_accounts(self):
        ambiguous = {"Marketing:Growth": "1", "Sales:Growth": "2"}

        self.assertIsNone(find_id_in_map(ambiguous, "Growth", allow_fuzzy=False))

    def test_same_leaf_under_two_parents_still_resolves_for_non_accounts(self):
        # Non-account maps keep the old first-match behavior.
        ambiguous = {"Marketing:Growth": "1", "Sales:Growth": "2"}

        self.assertEqual(find_id_in_map(ambiguous, "Growth", allow_fuzzy=True), "1")

    def test_blank_input_returns_none(self):
        self.assertIsNone(find_id_in_map(KZO_INVESTMENT_ACCOUNTS, "", allow_fuzzy=False))
        self.assertIsNone(find_id_in_map(KZO_INVESTMENT_ACCOUNTS, "   ", allow_fuzzy=False))


# KZP's chart of accounts grew a second "Growth" leaf, so the bare name is ambiguous. The
# sheet convention is: bare = income, " - Marketing Expense" suffix = expense.
KZP_GROWTH_ACCOUNTS = {
    "Marketing Income:Growth": "5010",
    "Marketing Expense:Growth": "6010",
    "Marketing Income:Affiliates": "5011",
}


class KzpGrowthAliasTests(unittest.TestCase):
    def test_bare_growth_resolves_to_marketing_income(self):
        self.assertEqual(
            find_id_in_map(KZP_GROWTH_ACCOUNTS, "Growth", allow_fuzzy=False, client_name="KZP"),
            "5010",
        )

    def test_suffixed_growth_resolves_to_marketing_expense(self):
        self.assertEqual(
            find_id_in_map(
                KZP_GROWTH_ACCOUNTS,
                "Growth - Marketing Expense",
                allow_fuzzy=False,
                client_name="KZP",
            ),
            "6010",
        )

    def test_qualified_form_with_analyst_spacing_resolves(self):
        # QBO's FullyQualifiedName has no spaces around ':', analysts type them anyway.
        self.assertEqual(
            find_id_in_map(
                KZP_GROWTH_ACCOUNTS, "Marketing Expense: Growth", allow_fuzzy=False, client_name="KZP"
            ),
            "6010",
        )

    def test_alias_survives_a_deeper_parent_in_qbo(self):
        # The alias target still goes through suffix-path matching, so QBO nesting the pair one
        # level deeper does not break it.
        deeper = {
            "Income:Marketing Income:Growth": "5010",
            "Expenses:Marketing Expense:Growth": "6010",
        }
        self.assertEqual(
            find_id_in_map(deeper, "Growth", allow_fuzzy=False, client_name="KZP"), "5010"
        )
        self.assertEqual(
            find_id_in_map(
                deeper, "Growth - Marketing Expense", allow_fuzzy=False, client_name="KZP"
            ),
            "6010",
        )

    def test_other_kzp_accounts_are_untouched_by_the_alias_table(self):
        self.assertEqual(
            find_id_in_map(KZP_GROWTH_ACCOUNTS, "Affiliates", allow_fuzzy=False, client_name="KZP"),
            "5011",
        )

    def test_alias_does_not_leak_into_other_workspaces(self):
        # KZO/KZDW/S5/UMBER keep the AMBIGUOUS refusal -- the convention is KZP's alone.
        for workspace in ("KZO", "KZDW", "S5", "UMBER", ""):
            with self.subTest(workspace=workspace):
                self.assertIsNone(
                    find_id_in_map(
                        KZP_GROWTH_ACCOUNTS, "Growth", allow_fuzzy=False, client_name=workspace
                    )
                )


class KzpGrowthAliasSyncTests(unittest.TestCase):
    """The sync stage must resolve the same way, or a 'Ready to sync' row posts elsewhere."""

    def _sync(self, client_name: str) -> QBOSync:
        client = MagicMock()
        client.client_name = client_name
        with patch.object(QBOSync, "_get_qbo_mappings", return_value={}):
            sync = QBOSync(client)
        sync.mappings = {"accounts": KZP_GROWTH_ACCOUNTS, "accounts_meta": {}}
        return sync

    def test_sync_resolves_both_growth_forms_for_kzp(self):
        sync = self._sync("KZP")
        self.assertEqual(sync.find_id("accounts", "Growth"), "5010")
        self.assertEqual(sync.find_id("accounts", "Growth - Marketing Expense"), "6010")

    def test_sync_still_refuses_ambiguous_growth_for_kzo(self):
        sync = self._sync("KZO")
        self.assertIsNone(sync.find_id("accounts", "Growth"))


class KzpGrowthAliasReconcileTests(unittest.TestCase):
    def _reconciler(self, client_name: str) -> Reconciler:
        client = MagicMock()
        client.client_name = client_name
        return Reconciler(client)

    def test_each_growth_form_matches_only_its_own_account(self):
        rec = self._reconciler("KZP")

        self.assertTrue(rec._is_account_match("Growth", "Marketing Income:Growth"))
        self.assertFalse(rec._is_account_match("Growth", "Marketing Expense:Growth"))
        self.assertTrue(
            rec._is_account_match("Growth - Marketing Expense", "Marketing Expense:Growth")
        )
        self.assertFalse(
            rec._is_account_match("Growth - Marketing Expense", "Marketing Income:Growth")
        )

    def test_bare_qbo_name_falls_back_to_a_leaf_comparison(self):
        # QBO does not always report the fully qualified path. With no parent in the payload the
        # two 'Growth' leaves cannot be told apart, so compare leaves instead of raising a
        # mismatch the payload cannot prove.
        rec = self._reconciler("KZP")

        self.assertTrue(rec._is_account_match("Growth", "Growth"))
        self.assertTrue(rec._is_account_match("Growth - Marketing Expense", "Growth"))
        self.assertFalse(rec._is_account_match("Growth - Marketing Expense", "Affiliates"))

    def test_unaliased_accounts_keep_suffix_and_fuzzy_matching(self):
        rec = self._reconciler("KZP")

        self.assertTrue(rec._is_account_match("Equipment", "Fixed Assets:Equipment"))
        self.assertTrue(rec._is_account_match("Fixed Assets:Equipment", "Fixed Assets:Equipment"))
        self.assertTrue(rec._is_account_match("Affiliate", "Marketing Income:Affiliates"))


class LeadingCardRenameTests(unittest.TestCase):
    """QBO renamed 'Leading Card - 1238' to 'Leading Card MKT - 1238' (account 11191).

    The old hardcoded rewrite 'Leading Card MKT - 1238' -> 'Leading Card - 1238' then sent every
    sheet row to a name QBO no longer has, so the row failed 'Account not found' at all three
    stages. The sheet name must resolve to the account as QBO names it now.
    """

    QBO_ACCOUNTS = {"Employee Cash Advances:Leading Card MKT - 1238": "11191"}

    def test_transform_resolves_the_mkt_name(self):
        self.assertEqual(
            find_id_in_map(self.QBO_ACCOUNTS, "Leading Card MKT - 1238", allow_fuzzy=False),
            "11191",
        )

    def test_sync_resolves_the_mkt_name(self):
        client = MagicMock()
        client.client_name = "KZO"
        with patch.object(QBOSync, "_get_qbo_mappings", return_value={}):
            sync = QBOSync(client)
        sync.mappings = {"accounts": self.QBO_ACCOUNTS, "accounts_meta": {}}

        self.assertEqual(sync.find_id("accounts", "Leading Card MKT - 1238"), "11191")

    def test_reconcile_matches_the_mkt_name(self):
        client = MagicMock()
        client.client_name = "KZO"
        rec = Reconciler(client)

        self.assertTrue(
            rec._is_account_match(
                "Leading Card MKT - 1238", "Employee Cash Advances:Leading Card MKT - 1238"
            )
        )


if __name__ == "__main__":
    unittest.main()
