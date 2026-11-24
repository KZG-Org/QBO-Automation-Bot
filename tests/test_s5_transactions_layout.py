"""S5's new 'Transactions' tab (2026-S5 Tech P&L Report), used from 16 Nov 2026.

Header on row 1; the QBO columns AK:AQ keep their names. AL is the invoice amount =M: positive
for most rows, negative for Misc bank fees posted as Journal (money out of the bank). AN/AO
read "move AL from AN to AO", so a negative AL moves the amount the other way.
"""
import unittest

import pandas as pd

from src.logic.raw_adapter import find_s5_header_row, standardize_raw_df
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
              21: cat, 26: fund_from, 27: bank, 36: kind, 37: amount, 39: an, 40: ao, 41: "USDT", 42: txn})
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
        staff = std.loc[5628]
        self.assertEqual(staff["Date"], 46342)
        self.assertEqual(staff["CO"], "S5")
        self.assertEqual(staff["Type"], "Staff Welfare")  # Sub-category, not 'Purchase Invoice'
        self.assertEqual(staff["Item Description"], "Staff birthday")
        self.assertEqual(staff["QBO Method"], "Expenses")

    def test_expense_amount_follows_the_old_sign_convention(self):
        std = _standardized().set_index("No")
        self.assertEqual(std.loc[5628, "USD - QBO"], -99)        # money out
        self.assertEqual(std.loc[5628, "QBO Transfer Fr"], "S5 Payroll Cregis | TRC20")
        self.assertEqual(std.loc[5700, "USD - QBO"], 50)         # refund: money back in
        self.assertEqual(std.loc[5700, "QBO Transfer Fr"], "S5 CC Card - 1981")

    def test_negative_journal_moves_the_other_way(self):
        std = _standardized().set_index("No")
        fee = std.loc[5665]
        self.assertEqual((fee["QBO Transfer Fr"], fee["QBO Transfer To"]), ("S5 CC Card - 1981", "Payment/banking"))
        self.assertEqual(fee["USD - QBO"], 5)
        rev = std.loc[5626]
        self.assertEqual((rev["QBO Transfer Fr"], rev["QBO Transfer To"]), ("Platform Fees - TH", "KZWL | TRC20"))

    def test_transfer_keeps_from_and_to(self):
        tr = _standardized().set_index("No").loc[5634]
        self.assertEqual((tr["QBO Transfer Fr"], tr["QBO Transfer To"]),
                         ("S5 Finance Cregis | TRC20", "S5 CC Card - 1981"))
        self.assertEqual(tr["USD - QBO"], 30000)

    def test_empty_no_column_stops_instead_of_numbering_rows(self):
        # The old tab numbered rows 1, 2, 3 when No was empty. On this tab that would give
        # Nov 1-15 rows (already posted from the old tab) fresh Nos and post them twice.
        grid = _grid()
        raw = pd.DataFrame([r[:-1] + [""] for r in grid[1:]], columns=grid[0])
        with self.assertRaises(ValueError) as ctx:
            standardize_raw_df(raw, "S5", "2026-11-01")
        self.assertIn("No", str(ctx.exception))

    def test_transform_posts_fees_out_of_the_bank_and_expenses_positive(self):
        std = _standardized()
        std["No"] = pd.to_numeric(std["No"])
        result = transform_raw(std, "S5", 0, 0, 0, {"accounts": ACCOUNTS, "locations": {}}, {}, "S5")

        jv = result.journals
        fee = jv[jv["No"] == 5665].set_index("Account")["Amount"]
        self.assertEqual(fee["S5 CC Card - 1981"], -5)   # credit the card
        self.assertEqual(fee["Payment/banking"], 5)      # debit the fee
        rev = jv[jv["No"] == 5626].set_index("Account")["Amount"]
        self.assertEqual(rev["KZWL | TRC20"], 85718.34)
        self.assertEqual(rev["Platform Fees - TH"], -85718.34)

        exp = result.expenses.set_index("No")
        self.assertEqual(exp.loc[5628, "Expense Line Amount"], 99)
        self.assertEqual(exp.loc[5628, "Account (Cr)"], "S5 Payroll Cregis | TRC20")
        self.assertEqual(exp.loc[5628, "Expense Account (Dr)"], "Staff Welfare")
        self.assertEqual(exp.loc[5700, "Expense Line Amount"], -50)

        tr = result.withdraw.set_index("No")
        self.assertEqual(tr.loc[5634, "Transfer Funds From"], "S5 Finance Cregis | TRC20")
        self.assertEqual(tr.loc[5634, "Transfer Funds To"], "S5 CC Card - 1981")
        self.assertEqual(tr.loc[5634, "Transfer Amount"], 30000)
        for frame in (jv, result.expenses, result.withdraw):
            self.assertTrue(frame["Remarks"].eq("Ready to sync").all(), frame["Remarks"].tolist())


if __name__ == "__main__":
    unittest.main()
