"""S5's new 'Transactions' tab (2026-S5 Tech P&L Report), used from 16 Nov 2026.

Header on row 1; the QBO columns AK:AQ keep their names. AL is read as an absolute amount
(S5 confirmed 2026-11-24): AN ('Transfer from') always decreases and AO ('Transfer to') always
increases, whatever the sign in the sheet.
"""
import unittest

import pandas as pd

from src.logic.raw_adapter import find_s5_header_row, standardize_raw_df

BASE = 100_000_000  # new-tab No = BASE + Txn ID
from src.logic.transformer import transform_raw

HEADER = [
    "Txn ID", "Invoice Date", "Payment Date", "Due Date", "Doc No", "Type", "Market / Entity",
    "Counterparty", "Category", "Sub-category", "Description", "Ccy", "Amount (Orig)",
    "FX Rate → USD", "Amount USD", "Paid Amount (Orig)", "Paid USD", "Outstanding USD", "Status",
    "Inv Period", "Pay Period", "Category", "Group", "CF Section", "Direction", "Days Overdue",
    "Fund Transfer From", "Bank", "Service From\n(blank = Invoice Date)",
    "Service To\n(blank = Service From)", "Recognition\nMonths", "Monthly Amount USD\n(P&L)",
    "From Idx", "To Idx", "Multi-month? (review)", "", "QBO Import",
    "Final Amount to be take (different currency)", "If Journal/Expense method:\nAnother records",
    "Transfer from", "Transfer to", "Currency", "No",
]


def _row(txn, date, kind, cat, sub, desc, amount, bank, an, ao, fund_from="", typ="Purchase Invoice"):
    r = dict.fromkeys(range(len(HEADER)), "")
    r.update({0: txn, 2: date, 5: typ, 6: "S5", 8: cat, 9: sub, 10: desc, 11: "USDT", 12: amount,
              21: cat, 26: fund_from, 27: bank, 36: kind, 37: amount, 39: an, 40: ao, 41: "USDT", 42: ""})
    return [r[i] for i in range(len(HEADER))]


ROWS = [
    _row(5628, 46342, "Expenses", "Staff Welfare", "Staff Welfare", "Staff birthday", 99,
         "S5 Payroll Cregis | TRC20", "S5 Payroll Cregis | TRC20", "Staff Welfare"),
    _row(5626, 46342, "Journal", "Platform Fees", "Platform Fees - TH", "KZWL fees from 96G1", 85718.34,
         "KZWL | TRC20", "Platform Fees - TH", "KZWL | TRC20", typ="Sales Invoice"),
    _row(5665, 46344, "Journal", "Misc", "Payment/banking", "Card fee", -5,
         "S5 CC Card - 1981", "Payment/banking", "S5 CC Card - 1981"),
    _row(5634, 46343, "Transfer", "Transfer", "Transfer", "Top up card", 30000,
         "S5 CC Card - 1981", "S5 Finance Cregis | TRC20", "S5 CC Card - 1981",
         fund_from="S5 Finance Cregis | TRC20"),
    _row(5700, 46345, "Expenses", "Infra", "Softwares", "Refund of a licence", -50,
         "S5 CC Card - 1981", "S5 CC Card - 1981", "Softwares"),
]
ACCOUNTS = {
    "Payroll:S5 Payroll Cregis | TRC20": "1", "Staff Welfare": "2", "Platform Fees - TH": "3",
    "KZWL | TRC20": "4", "Payment/banking": "5", "Prepaid Card:S5 CC Card - 1981": "6",
    "S5 Finance Cregis | TRC20": "7", "Infra:Softwares": "8",
}


def _grid():
    return [HEADER] + ROWS


def _standardized():
    grid = _grid()
    raw = pd.DataFrame(grid[1:], columns=grid[0])
    return standardize_raw_df(raw, "S5", "2026-11-01")


class S5TransactionsLayoutTests(unittest.TestCase):
    def test_header_on_row_1_is_found(self):
        self.assertEqual(find_s5_header_row(_grid()), 1)

    def test_columns_map_to_the_standard_names(self):
        std = _standardized().set_index("No")
        staff = std.loc[BASE + 5628]
        self.assertEqual(staff["Date"], 46342)
        self.assertEqual(staff["CO"], "S5")
        self.assertEqual(staff["Type"], "Staff Welfare")  # Sub-category, not 'Purchase Invoice'
        self.assertEqual(staff["Item Description"], "Staff birthday")
        self.assertEqual(staff["QBO Method"], "Expenses")

    def test_expense_amount_follows_the_old_sign_convention(self):
        std = _standardized().set_index("No")
        self.assertEqual(std.loc[BASE + 5628, "USD - QBO"], -99)        # money out
        self.assertEqual(std.loc[BASE + 5628, "QBO Transfer Fr"], "S5 Payroll Cregis | TRC20")
        self.assertEqual(std.loc[BASE + 5700, "USD - QBO"], -50)        # negative AL: same as +50
        self.assertEqual(std.loc[BASE + 5700, "QBO Transfer Fr"], "S5 CC Card - 1981")

    def test_negative_journal_keeps_from_and_to(self):
        # S5: "Transferring $4.13 to S5 Card - 8574 increases the balance, while transferring
        # from Payment/Banking decreases it" -- AL is absolute, AN/AO are never swapped.
        std = _standardized().set_index("No")
        fee = std.loc[BASE + 5665]
        self.assertEqual((fee["QBO Transfer Fr"], fee["QBO Transfer To"]), ("Payment/banking", "S5 CC Card - 1981"))
        self.assertEqual(fee["USD - QBO"], 5)
        rev = std.loc[BASE + 5626]
        self.assertEqual((rev["QBO Transfer Fr"], rev["QBO Transfer To"]), ("Platform Fees - TH", "KZWL | TRC20"))

    def test_transfer_keeps_from_and_to(self):
        tr = _standardized().set_index("No").loc[BASE + 5634]
        self.assertEqual((tr["QBO Transfer Fr"], tr["QBO Transfer To"]),
                         ("S5 Finance Cregis | TRC20", "S5 CC Card - 1981"))
        self.assertEqual(tr["USD - QBO"], 30000)

    def test_no_is_the_txn_id_and_column_aq_is_not_needed(self):
        # AQ is left empty in every test row: the Txn ID is the row id.
        std = _standardized()
        self.assertEqual(sorted(std["No"].tolist()), [BASE + t for t in (5626, 5628, 5634, 5665, 5700)])

    def test_new_ids_are_above_every_old_tab_no(self):
        # Row 10 (Nov) keeps its Transform file, whose Nos reach 1115003; the bot never lowers
        # an S5 checkpoint below the file's top No. Old Nos are at most 12319999 (Dec 31 + count).
        self.assertTrue((_standardized()["No"] > 12_319_999).all())

    def test_rows_before_16_nov_2026_get_no_row_id(self):
        # Nov 1-15 were posted from the old tab; Txn 5660 is an Aug row added later.
        grid = _grid() + [
            _row(5620, 46341, "Expenses", "Infra", "Softwares", "15 Nov licence", 10,
                 "S5 CC Card - 1981", "S5 CC Card - 1981", "Softwares"),
            _row(5660, 46253, "Journal", "Platform Fees", "Platform Fees - TH", "Aug fees", 7,
                 "KZWL | TRC20", "Platform Fees - TH", "KZWL | TRC20"),
        ]
        raw = pd.DataFrame(grid[1:], columns=grid[0])
        std = standardize_raw_df(raw, "S5", "2026-11-01").set_index("Item Description")
        self.assertEqual(std.loc["15 Nov licence", "No"], 0)
        self.assertEqual(std.loc["Aug fees", "No"], 0)
        self.assertEqual(std.loc["Staff birthday", "No"], BASE + 5628)

    def test_missing_txn_ids_stop_instead_of_numbering_rows(self):
        # Numbering rows 1, 2, 3 (the old tab's fallback) would re-post Nov 1-15.
        grid = _grid()
        raw = pd.DataFrame([[""] + r[1:] for r in grid[1:]], columns=grid[0])
        with self.assertRaises(ValueError) as ctx:
            standardize_raw_df(raw, "S5", "2026-11-01")
        self.assertIn("Txn ID", str(ctx.exception))

    def test_transform_posts_absolute_amounts_from_an_to_ao(self):
        std = _standardized()
        std["No"] = pd.to_numeric(std["No"])
        result = transform_raw(std, "S5", 0, 0, 0, {"accounts": ACCOUNTS, "locations": {}}, {}, "S5")

        jv = result.journals
        fee = jv[jv["No"] == BASE + 5665].set_index("Account")["Amount"]
        self.assertEqual(fee["S5 CC Card - 1981"], 5)    # To: increases
        self.assertEqual(fee["Payment/banking"], -5)     # From: decreases
        rev = jv[jv["No"] == BASE + 5626].set_index("Account")["Amount"]
        self.assertEqual(rev["KZWL | TRC20"], 85718.34)
        self.assertEqual(rev["Platform Fees - TH"], -85718.34)

        exp = result.expenses.set_index("No")
        self.assertEqual(exp.loc[BASE + 5628, "Expense Line Amount"], 99)
        self.assertEqual(exp.loc[BASE + 5628, "Account (Cr)"], "S5 Payroll Cregis | TRC20")
        self.assertEqual(exp.loc[BASE + 5628, "Expense Account (Dr)"], "Staff Welfare")
        self.assertEqual(exp.loc[BASE + 5700, "Expense Line Amount"], 50)

        tr = result.withdraw.set_index("No")
        self.assertEqual(tr.loc[BASE + 5634, "Transfer Funds From"], "S5 Finance Cregis | TRC20")
        self.assertEqual(tr.loc[BASE + 5634, "Transfer Funds To"], "S5 CC Card - 1981")
        self.assertEqual(tr.loc[BASE + 5634, "Transfer Amount"], 30000)
        for frame in (jv, result.expenses, result.withdraw):
            self.assertTrue(frame["Remarks"].eq("Ready to sync").all(), frame["Remarks"].tolist())


if __name__ == "__main__":
    unittest.main()
