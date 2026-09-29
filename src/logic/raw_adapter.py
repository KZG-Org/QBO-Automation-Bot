from __future__ import annotations

import re
from typing import Iterable

import pandas as pd

from config import settings
from src.utils.logger import setup_logger

logger = setup_logger("raw_adapter")

RAW_STANDARD_COLUMNS = [
    "CO",
    "COY",
    "Date",
    "Category",
    "Type",
    "Item Description",
    "TrxHarsh",
    "Account Fr",
    "Account To",
    "Currency",
    "Amount Fr",
    "Currency To",
    "Amount To",
    "Budget",
    "USD - Raw",
    "USD - Actual",
    "USD - Loss",
    "USD - QBO",
    "Reclass",
    "QBO Method",
    "If Journal/Expense Method",
    "QBO Transfer Fr",
    "QBO Transfer To",
    "Check (Internal use)",
    "No",
    "Currency Rate",
]


def _norm_name(name: str) -> str:
    return re.sub(r"\s+", " ", str(name).replace("\n", " ").strip()).lower()


def _find_col(df: pd.DataFrame, aliases: Iterable[str]) -> str | None:
    by_norm = {_norm_name(c): c for c in df.columns}
    for alias in aliases:
        real = by_norm.get(_norm_name(alias))
        # Column names can legitimately be "" (blank header cell), so test for None.
        if real is not None:
            return real
    return None


def _value_series(series: pd.Series) -> pd.Series:
    return series.fillna("").astype(str).str.strip()


def _normalize_currency_series(series: pd.Series) -> pd.Series:
    vals = _value_series(series)
    has_usd = vals.str.contains("USD", case=False, na=False)
    out = vals.where(~has_usd, "USD")
    return out.where(out != "", "USD")


def _normalize_qbo_method_series(series: pd.Series) -> pd.Series:
    vals = _value_series(series)
    low = vals.str.lower()
    out = vals.copy()
    out = out.where(~low.str.contains(r"\b(?:jv|journal)\b", regex=True, na=False), "Journal")
    out = out.where(~low.str.contains("expense", case=False, na=False), "Expenses")
    out = out.where(~low.str.contains("transfer", case=False, na=False), "Transfer")
    out = out.where(~low.str.contains("reclass", case=False, na=False), "Reclass")
    return out


def _clean_headers(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    out.columns = [re.sub(r"\s+", " ", str(c).replace("\n", " ").strip()) for c in out.columns]
    # Deduplicate column names: keep first occurrence, rename subsequent ones with suffix
    seen: dict[str, int] = {}
    new_cols = []
    for col in out.columns:
        if col in seen:
            seen[col] += 1
            new_cols.append(f"{col}._dup{seen[col]}")
        else:
            seen[col] = 0
            new_cols.append(col)
    out.columns = new_cols
    return out


def _parse_amount_series(series: pd.Series) -> pd.Series:
    if pd.api.types.is_numeric_dtype(series):
        return pd.to_numeric(series, errors="coerce").fillna(0.0)

    s = series.astype(str).str.strip()
    s = s.str.replace(",", "", regex=False)
    s = s.str.replace("(", "-", regex=False).str.replace(")", "", regex=False)
    s = s.str.replace(r"[^0-9\.\-]", "", regex=True)
    return pd.to_numeric(s, errors="coerce").fillna(0.0)


def _coerce_standard_numeric_cols(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    for col in ["USD - QBO", "Amount Fr", "Amount To", "Currency Rate"]:
        if col in out.columns:
            out[col] = _parse_amount_series(out[col])
    if "No" in out.columns:
        out["No"] = pd.to_numeric(out["No"], errors="coerce").fillna(0)
    return out


def _normalize_kzp_date(date_series: pd.Series, raw_month: str) -> pd.Series:
    out = date_series.astype(str).str.strip()
    is_blank = out.eq("") | out.str.lower().isin(["nan", "nat", "none"])
    has_year = out.str.contains(r"\b\d{4}\b", regex=True, na=False)
    is_numeric = pd.to_numeric(date_series, errors="coerce").notna()

    target_year = None
    dt = pd.to_datetime(raw_month, errors="coerce")
    if pd.notna(dt):
        target_year = int(dt.year)

    if target_year is not None:
        needs_year = (~is_blank) & (~has_year) & (~is_numeric)
        out = out.where(~needs_year, out + f" {target_year}")
    return out


def _standardize_legacy(df: pd.DataFrame) -> pd.DataFrame:
    out = df.iloc[:, : len(RAW_STANDARD_COLUMNS)].copy()
    while out.shape[1] < len(RAW_STANDARD_COLUMNS):
        out[f"_pad_{out.shape[1]}"] = ""
    out.columns = RAW_STANDARD_COLUMNS
    return _coerce_standard_numeric_cols(out)


KZO_CHECK_ALIASES = ["Checking ( For our use only )", "Check (Internal use)", "Check"]

KZO_METHOD_ALIASES = [
    "QBO Import Method (Journal/Expenses/Transfer)",
    "QBO Import Method",
    "QBO Method",
]

# Canonical name -> source header aliases for the KZO country tabs. Mapping by header
# name (instead of position) keeps the pipeline stable when analysts insert helper
# columns such as "Transacted Amount Check" / "Variance Check" mid-sheet.
KZO_COLUMN_ALIASES: dict[str, list[str]] = {
    "CO": ["CO"],
    "COY": ["COY"],
    "Date": ["Date"],
    "Category": ["Category"],
    "Type": ["Sub Category", "Type"],
    "Item Description": ["Item Description"],
    "TrxHarsh": ["TrxHash", "Trx Hash", "TrxHarsh"],
    "Account Fr": ["From Account", "Account Fr"],
    "Account To": ["To Account", "Account To"],
    "Currency": ["Currency From", "Currency"],
    "Amount Fr": ["Amount From", "Amount Fr"],
    "Currency To": ["Currency To"],
    "Amount To": ["Amount To"],
    "Budget": ["Budget Rate", "Budget"],
    "USD - Raw": ["USD", "USD - Raw"],
    "USD - Actual": ["Actual USD Transacted", "USD - Actual"],
    "USD - Loss": ["Realised Loss", "Realized Loss", "USD - Loss"],
    "USD - QBO": ["USD - QBO"],
    "Reclass": ["Reclass/To check", "Reclass"],
    "QBO Method": KZO_METHOD_ALIASES,
    "If Journal/Expense Method": [
        "If Journal/Expense method: Another records",
        "If Journal/Expense Method",
    ],
    "QBO Transfer Fr": ["Transfer from", "Transfer From", "QBO Transfer Fr"],
    "QBO Transfer To": ["Transfer to", "Transfer To", "QBO Transfer To"],
    "Check (Internal use)": KZO_CHECK_ALIASES,
}


def _resolve_kzo_no_col(df: pd.DataFrame) -> str | None:
    """Locate the row-number column of a KZO country tab.

    Exports frequently ship the row-number column with an empty header cell (it sits
    right of "Checking ( For our use only )"), so a name lookup alone is not enough.
    """
    named = _find_col(df, ["No", "No."])
    if named is not None:
        return named

    columns = list(df.columns)
    anchor = _find_col(df, KZO_CHECK_ALIASES)
    if anchor is None or anchor not in columns:
        return None

    position = columns.index(anchor) + 1
    if position >= len(columns):
        return None
    return columns[position]


def _resolve_kzo_co_col(df: pd.DataFrame) -> str | None:
    """Locate the country column of a KZO country tab.

    Some country tabs ship column A with an empty header cell -- the PH tab does -- exactly
    like the row-number column. CO always sits immediately left of COY, so anchor on that
    rather than requiring the header text.
    """
    named = _find_col(df, ["CO"])
    if named is not None:
        return named

    columns = list(df.columns)
    anchor = _find_col(df, ["COY"])
    if anchor is None or anchor not in columns:
        return None

    position = columns.index(anchor) - 1
    if position < 0:
        return None
    return columns[position]


def _standardize_kzo(df: pd.DataFrame) -> pd.DataFrame:
    """Map the header-based KZO country layout (BR/TH/... share the same headers)."""
    idx = df.index

    out = pd.DataFrame(index=idx)
    for canonical_name, source_aliases in KZO_COLUMN_ALIASES.items():
        source_col = _find_col(df, source_aliases)
        out[canonical_name] = df[source_col] if source_col is not None else ""

    co_col = _resolve_kzo_co_col(df)
    out["CO"] = df[co_col] if co_col is not None else ""

    no_col = _resolve_kzo_no_col(df)
    out["No"] = df[no_col] if no_col is not None else ""

    # KZO posts in USD; exchange rates are only consumed by the KZDW path.
    out["Currency Rate"] = 0.0
    return _coerce_standard_numeric_cols(out.reindex(columns=RAW_STANDARD_COLUMNS, fill_value=""))


def _standardize_kzp(df: pd.DataFrame, raw_month: str) -> pd.DataFrame:
    idx = df.index

    month_col = _find_col(df, ["Month"])
    co_col = _find_col(df, ["CO"])
    coy_col = _find_col(df, ["COY"])
    date_col = _find_col(df, ["Date"])
    category_col = _find_col(df, ["Category"])
    type_col = _find_col(df, ["Type", "Sub Category"])
    desc_col = _find_col(df, ["Item Description"])
    trx_hash_col = _find_col(df, ["Trx Hash", "TrxHarsh"])
    currency_col = _find_col(df, ["Currency", "Currency From"])
    transfer_from_col = _find_col(
        df,
        ["Fund Transfer From", "Transfer from", "Transfer From", "If Transfer method: Fund Transfer From", "From Account"],
    )
    transfer_to_col = _find_col(df, ["Transfer to", "Transfer To", "Transfer to ((Can copy from column H )", "To Account"])
    bank_col = _find_col(df, ["From Account", "Bank", "Bank ( To )", "Bank (To)", "Bank To"])
    account_to_col = _find_col(df, ["To Account", "If Journal/Expense method: Another records", "If Journal/Expense Method"])
    amount_col = _find_col(df, ["USD - QBO", "In/Out (USD)", "In/Out", "Amount From", "USD"])
    check_col = _find_col(df, ["Check", "Checking ( For our use only )"])
    method_col = _find_col(df, ["QBO Import", "QBO Import Method (Journal/Expenses/Transfer)", "QBO Method"])
    no_col = _find_col(df, ["No"])

    def col_or_empty(col_name: str | None) -> pd.Series:
        if col_name and col_name in df.columns:
            return df[col_name]
        return pd.Series([""] * len(df), index=idx)

    amount = _parse_amount_series(col_or_empty(amount_col))
    date_series = _normalize_kzp_date(col_or_empty(date_col), raw_month)
    account_from = _value_series(col_or_empty(bank_col))
    account_to = _value_series(col_or_empty(account_to_col))
    account_to_filled = account_to.where(account_to != "", account_from)
    transfer_from = _value_series(col_or_empty(transfer_from_col))
    transfer_to = _value_series(col_or_empty(transfer_to_col))
    transfer_from_filled = transfer_from.where(transfer_from != "", account_from)
    transfer_to_filled = transfer_to.where(transfer_to != "", account_to_filled)
    type_vals = col_or_empty(type_col)
    type_vals = type_vals.where(_value_series(type_vals) != "", account_to_filled)

    no_series = pd.to_numeric(col_or_empty(no_col), errors="coerce")
    if no_series.fillna(0).eq(0).all():
        no_series = pd.Series(range(1, len(df) + 1), index=idx, dtype="float64")

    out = pd.DataFrame(index=idx)
    # KZP Location/Class should come from CO (not COY).
    out["CO"] = col_or_empty(co_col)
    out["COY"] = col_or_empty(coy_col)
    out["Date"] = date_series
    out["Category"] = col_or_empty(category_col)
    out["Type"] = type_vals
    out["Item Description"] = col_or_empty(desc_col)
    out["TrxHarsh"] = col_or_empty(trx_hash_col)
    # KZP journals/expenses should source account from the "from" side.
    out["Account Fr"] = account_from
    out["Account To"] = account_to_filled
    out["Currency"] = col_or_empty(currency_col).replace("", "USD")
    out["Amount Fr"] = amount
    out["Currency To"] = ""
    out["Amount To"] = 0.0
    out["Budget"] = 0.0
    out["USD - Raw"] = amount
    out["USD - Actual"] = amount
    out["USD - Loss"] = 0.0
    out["USD - QBO"] = amount
    out["Reclass"] = ""
    out["QBO Method"] = col_or_empty(method_col)
    out["If Journal/Expense Method"] = account_from.where(account_from != "", account_to_filled)
    out["QBO Transfer Fr"] = transfer_from_filled
    out["QBO Transfer To"] = transfer_to_filled
    out["Check (Internal use)"] = col_or_empty(check_col)
    out["No"] = no_series
    out["Currency Rate"] = 0.0

    # Remove month/header separator rows from exports (if present), keep real data rows.
    if month_col:
        category_clean = out["Category"].astype(str).str.strip()
        out = out[category_clean != ""].copy()

    return _coerce_standard_numeric_cols(out)


def _standardize_kzdw(df: pd.DataFrame) -> pd.DataFrame:
    idx = df.index

    co_col = _find_col(df, ["CO"])
    coy_col = _find_col(df, ["COY"])
    date_col = _find_col(df, ["Date"])
    category_col = _find_col(df, ["Category"])
    type_col = _find_col(df, ["Type"])
    desc_col = _find_col(df, ["Item Description"])
    trx_hash_col = _find_col(df, ["Trx Hash", "TrxHarsh"])
    amount_col = _find_col(df, ["Final Amount to be take (different currency)", "USD - QBO", "USD"])
    currency_col = _find_col(df, ["Currency"])
    currency_rate_col = _find_col(df, ["Currency Rate", "Currency Exchange", "Exchange Rate"])
    method_col = _find_col(df, ["QBO Import", "QBO Method", "QBO Import Method (Journal/Expenses/Transfer)"])
    acc_debit_col = _find_col(df, ["If Journal/Expense method: Another records", "If Journal/Expense Method"])
    transfer_from_col = _find_col(df, ["Transfer from", "Transfer From", "If Transfer method: Fund Transfer From", "Fund Transfer From"])
    transfer_to_col = _find_col(df, ["Transfer to", "Transfer To", "Transfer to ((Can copy from column H )"])
    check_col = _find_col(df, ["Checking ( For our use only )", "Check (Internal use)", "Check"])
    no_col = _find_col(df, ["No"])

    def col_or_empty(col_name: str | None) -> pd.Series:
        if col_name and col_name in df.columns:
            return df[col_name]
        return pd.Series([""] * len(df), index=idx)

    amount = _parse_amount_series(col_or_empty(amount_col))
    currency_rate = _parse_amount_series(col_or_empty(currency_rate_col))
    currency = _normalize_currency_series(col_or_empty(currency_col))
    acc_v = _value_series(col_or_empty(acc_debit_col))
    transfer_to = _value_series(col_or_empty(transfer_to_col))
    transfer_from = _value_series(col_or_empty(transfer_from_col))
    # KZDW mapping rules from source sheet:
    # 1) Prefill Transfer From/To with V ("If Journal/Expense ... Another records")
    # 2) Reverse Journal account direction (Debit/Credit) for KZDW.
    transfer_from_filled = transfer_from.where(transfer_from != "", acc_v)
    transfer_to_filled = transfer_to.where(transfer_to != "", acc_v)
    debit_account = transfer_from_filled
    credit_account = transfer_to_filled
    type_vals = transfer_to_filled

    no_series = pd.to_numeric(col_or_empty(no_col), errors="coerce")
    if no_series.fillna(0).eq(0).all():
        no_series = pd.Series(range(1, len(df) + 1), index=idx, dtype="float64")

    out = pd.DataFrame(index=idx)
    out["CO"] = col_or_empty(co_col)
    out["COY"] = col_or_empty(coy_col)
    out["Date"] = col_or_empty(date_col)
    out["Category"] = col_or_empty(category_col)
    out["Type"] = type_vals
    out["Item Description"] = col_or_empty(desc_col)
    out["TrxHarsh"] = col_or_empty(trx_hash_col)
    # Debit account for Journal/Expense.
    out["Account Fr"] = debit_account
    out["Account To"] = transfer_to_filled
    out["Currency"] = currency
    out["Amount Fr"] = amount
    out["Currency To"] = ""
    out["Amount To"] = 0.0
    out["Budget"] = 0.0
    out["USD - Raw"] = amount
    out["USD - Actual"] = amount
    out["USD - Loss"] = 0.0
    out["USD - QBO"] = amount
    out["Reclass"] = ""
    out["QBO Method"] = col_or_empty(method_col)
    # Credit/source account for Journal/Expense: X -> V (reversed for KZDW).
    out["If Journal/Expense Method"] = credit_account
    out["QBO Transfer Fr"] = transfer_from_filled
    out["QBO Transfer To"] = transfer_to_filled
    out["Check (Internal use)"] = col_or_empty(check_col)
    out["No"] = no_series
    out["Currency Rate"] = currency_rate

    sub_category_col = "Type"
    
    if sub_category_col:
        out["Category"] = out["Category"].where(
            _value_series(out["Category"]) != "",
            col_or_empty(sub_category_col),
        )

    return _coerce_standard_numeric_cols(out)


# --- S5 header discovery -----------------------------------------------------------
# S5 alone keeps a wallet-balance block above its transaction grid, so its header row
# drifts by a row or two whenever an analyst adds or removes a wallet line. It sat on
# row 19 until Sep 2026, when an inserted wallet line pushed it to row 20. Because
# _standardize_s5 drops every row that has no Date and no Category, reading the wrong
# row does not fail loudly -- it yields zero rows, which run_ingestion reports as
# "DONE (Empty)" as though the tab had no data. So find the row by its content.
#
# Only S5 uses this. KZO / KZP / KZDW / UMBER keep their own header handling.

S5_DEFAULT_HEADER_ROW = 19

# The header has always been in the high teens / low twenties; scan well past that so a
# larger edit to the balances block above it is still found.
S5_HEADER_SEARCH_ROWS = 40

# A row must carry one alias from every group to be the header. Date and Category are
# required because _standardize_s5 filters on them -- requiring them here is what stops
# a wrong row from silently emptying the frame. The rest mirror the `has_s5_shape` test
# in standardize_raw_df, so a row this accepts is one that path will also recognise.
# (has_s5_shape itself is deliberately left looser: it also classifies non-S5 clients
# that happen to arrive in the S5 layout, and must not start demanding Date/Category.)
S5_HEADER_ALIAS_GROUPS: tuple[tuple[str, ...], ...] = (
    ("CO",),
    ("Date",),
    ("Category",),
    ("Bank", "Bank/Crypto"),
    ("QBO Import", "QBO Way"),
    ("Final Amount to be take (different currency)", "Final Amount"),
)


def row_is_s5_header(cells: Iterable) -> bool:
    """True when a row of raw cell values reads as the S5 transaction-grid header."""
    names = {_norm_name(c) for c in cells if str(c).strip() != ""}
    return all(
        any(_norm_name(alias) in names for alias in group)
        for group in S5_HEADER_ALIAS_GROUPS
    )


def find_s5_header_row(values: list[list], search_rows: int = S5_HEADER_SEARCH_ROWS) -> int | None:
    """1-based row number of the S5 header in a raw cell grid, or None if not found."""
    for row_number, row in enumerate(values[:search_rows], start=1):
        if row_is_s5_header(row):
            return row_number
    return None


def read_s5_raw_df(gs, source_url: str, raw_tab_name: str, client_name: str = "S5") -> pd.DataFrame:
    """Read the S5 raw tab, locating its header row rather than assuming row 19.

    Takes the sheets handle rather than living in the connector because where the S5
    header sits is S5 domain knowledge, not a Sheets concern. The grid is fetched once
    and the frame built from it, so finding the header costs no extra API call.
    """
    values = gs.read_values(source_url, raw_tab_name, value_render_option="UNFORMATTED_VALUE")
    if not values:
        return pd.DataFrame()

    header_row = find_s5_header_row(values)
    if header_row is None:
        raise ValueError(
            f"S5 raw header not found in the first {S5_HEADER_SEARCH_ROWS} rows of "
            f"'{raw_tab_name}'. Expected one row carrying CO, Date, Category, Bank, "
            f"QBO Import (or QBO Way) and Final Amount. Check that the transaction grid "
            f"header is intact and no column was renamed."
        )

    if header_row != S5_DEFAULT_HEADER_ROW:
        logger.info(
            "   [%s] S5 source header found on row %s (was row %s); the balances block "
            "above the grid has changed height.",
            client_name,
            header_row,
            S5_DEFAULT_HEADER_ROW,
        )

    df = gs.df_from_values(values, header_row)
    df.attrs["header_row"] = header_row  # lets callers map frame rows back to sheet rows
    return df


def _standardize_s5(
    df: pd.DataFrame,
    prefer_secondary_date: bool = False,
    preserve_source_currency: bool = False,
) -> pd.DataFrame:
    idx = df.index

    co_col = _find_col(df, ["CO"])
    coy_col = _find_col(df, ["COY"])
    date_col = _find_col(df, ["Date"])
    date_col_secondary = _find_col(df, ["Date._dup1", "Date.1"])
    category_col = _find_col(df, ["Category"])
    type_col = _find_col(df, ["Type", "Sub Category", "Sub-category"])
    desc_col = _find_col(df, ["Item Description"])
    trx_hash_col = _find_col(df, ["Trx Hash", "TrxHarsh"])
    currency_col = _find_col(df, ["Currency", "Currency From"])
    bank_col = _find_col(df, ["Bank", "Bank/Crypto", "From Account", "Bank ( To )", "Bank (To)", "Bank To"])
    amount_final_col = _find_col(df, ["Final Amount to be take (different currency)", "Final Amount", "USD - QBO"])
    amount_inout_col = _find_col(df, ["In/Out (USD)", "In/Out", "Amount From", "USD"])
    transfer_from_col = _find_col(
        df,
        ["Transfer from", "Transfer From", "If Transfer method: Fund Transfer From", "Fund Transfer From", "Transfer from"],
    )
    transfer_to_col = _find_col(df, ["Transfer to", "Transfer To", "Transfer to ((Can copy from column H )", "To Account"])
    acc_cr_col = _find_col(df, ["If Journal/Expense method: Another records", "If Journal/Expense Method"])
    method_col = _find_col(df, ["QBO Import", "QBO Method", "QBO Import Method (Journal/Expenses/Transfer)"])
    method_fallback_col = _find_col(df, ["QBO Way"])
    check_col = _find_col(df, ["QBO Post", "Check", "Checking ( For our use only )"])
    no_col = _find_col(df, ["No"])

    def col_or_empty(col_name: str | None) -> pd.Series:
        if col_name and col_name in df.columns:
            return df[col_name]
        return pd.Series([""] * len(df), index=idx)
    
    chosen_date_col = date_col_secondary if (prefer_secondary_date and date_col_secondary) else date_col

    amount_final_raw = col_or_empty(amount_final_col)
    amount_inout_raw = col_or_empty(amount_inout_col)
    amount_final = _parse_amount_series(amount_final_raw)
    amount_inout = _parse_amount_series(amount_inout_raw)
    amount = amount_final.where(_value_series(amount_final_raw) != "", amount_inout)

    method_primary = _value_series(col_or_empty(method_col))
    method_fallback = _value_series(col_or_empty(method_fallback_col))
    method = method_primary.where(method_primary != "", method_fallback)
    method = _normalize_qbo_method_series(method)

    bank_vals = _value_series(col_or_empty(bank_col))
    acc_cr_vals = _value_series(col_or_empty(acc_cr_col))
    transfer_from_vals = _value_series(col_or_empty(transfer_from_col))
    transfer_to_vals = _value_series(col_or_empty(transfer_to_col))
    type_vals = _value_series(col_or_empty(type_col))

    transfer_from_filled = transfer_from_vals.where(transfer_from_vals != "", bank_vals)
    transfer_from_filled = transfer_from_filled.where(transfer_from_filled != "", acc_cr_vals)
    transfer_to_filled = transfer_to_vals.where(transfer_to_vals != "", type_vals)
    transfer_to_filled = transfer_to_filled.where(transfer_to_filled != "", bank_vals)

    no_series = pd.to_numeric(col_or_empty(no_col), errors="coerce")
    if no_series.fillna(0).eq(0).all():
        no_series = pd.Series(range(1, len(df) + 1), index=idx, dtype="float64")

    co_vals = _value_series(col_or_empty(co_col))
    coy_vals = _value_series(col_or_empty(coy_col))
    coy_vals = coy_vals.where(coy_vals != "", co_vals)

    out = pd.DataFrame(index=idx)
    out["CO"] = co_vals
    out["COY"] = coy_vals
    out["Date"] = col_or_empty(chosen_date_col)
    out["Category"] = col_or_empty(category_col)
    out["Type"] = col_or_empty(type_col)
    out["Item Description"] = col_or_empty(desc_col)
    out["TrxHarsh"] = col_or_empty(trx_hash_col)
    out["Account Fr"] = bank_vals.where(bank_vals != "", transfer_from_filled)
    out["Account To"] = transfer_to_filled
    if preserve_source_currency:
        raw_currency = _value_series(col_or_empty(currency_col)).str.upper()
        out["Currency"] = raw_currency.where(raw_currency != "", "USD")
    else:
        out["Currency"] = "USD"  # S5 always uses USD in QBO
    out["Amount Fr"] = amount
    out["Currency To"] = ""
    out["Amount To"] = 0.0
    out["Budget"] = 0.0
    out["USD - Raw"] = amount
    out["USD - Actual"] = amount
    out["USD - Loss"] = 0.0
    out["USD - QBO"] = amount
    out["Reclass"] = ""
    out["QBO Method"] = method
    out["If Journal/Expense Method"] = transfer_from_filled
    out["QBO Transfer Fr"] = transfer_from_filled
    out["QBO Transfer To"] = transfer_to_filled
    out["Check (Internal use)"] = col_or_empty(check_col)
    out["No"] = no_series
    out["Currency Rate"] = 0.0

    category_clean = _value_series(out["Category"])
    date_clean = _value_series(out["Date"])
    out = out[(category_clean != "") & (date_clean != "")].copy()

    return _coerce_standard_numeric_cols(out)


def standardize_raw_df(raw_df: pd.DataFrame, client_name: str, raw_month: str) -> pd.DataFrame:
    """
    Convert incoming raw data into the canonical schema expected by
    transform/reconcile logic. Handles the header-based KZO country layout, the
    legacy positional KZO layout, and custom KZP/KZDW/S5/UMBER layouts.
    """
    if raw_df is None or raw_df.empty:
        return pd.DataFrame(columns=RAW_STANDARD_COLUMNS)

    cleaned = _clean_headers(raw_df)
    layout = _raw_layout(cleaned, client_name)
    if "kzo" in str(client_name).lower() and layout != "kzo":
        # The legacy positional layout maps columns by index, so a KZO tab that lands here
        # by accident produces silently wrong data (most visibly No=0 for every row) rather
        # than an error. Say so.
        logger.warning(
            "⚠️ [%s] KZO header shape not recognised; falling back to the legacy positional "
            "layout. Check the raw tab headers: CO/COY, USD - QBO, the QBO Import Method "
            "column and 'Checking ( For our use only )'. Columns found: %s",
            client_name,
            list(cleaned.columns)[:28],
        )
    if layout == "kzo":
        return _standardize_kzo(cleaned)
    if layout == "umber":
        # Umber exports have two "Date" columns; Q (the second Date) is the source-of-truth date.
        return _standardize_s5(cleaned, prefer_secondary_date=True, preserve_source_currency=True)
    if layout == "s5":
        return _standardize_s5(cleaned)
    if layout == "kzp":
        return _standardize_kzp(cleaned, raw_month)
    if layout == "kzdw":
        return _standardize_kzdw(cleaned)
    return _standardize_legacy(cleaned)


def _raw_layout(cleaned: pd.DataFrame, client_name: str) -> str:
    """Which standardizer reads this raw tab: kzdw / kzo / umber / s5 / kzp / legacy."""
    client_lower = str(client_name).lower()
    is_kzp_client = "kzp" in client_lower
    is_s5_client = "s5" in client_lower
    is_kzdw_client = settings.is_kzdw_family(client_name)  # KZDW or TINDERPAY
    is_umber_client = "umber" in client_lower
    is_kzo_client = "kzo" in client_lower
    # All KZO country tabs share one header set, so map them by name rather than by
    # position. "No" and "CO" are deliberately not required by header text: their header
    # cells are often blank (No on most tabs, CO on PH), so both are resolved positionally
    # from a neighbouring anchor instead. Requiring the literal "CO" header here used to
    # drop the whole PH tab into the legacy positional layout, which reads "No" from column
    # 24 ("Transfer to") -- every No came out 0 and every transformed row reconciled as
    # "Missing in Raw".
    has_kzo_header_shape = (
        _resolve_kzo_co_col(cleaned) is not None
        and _find_col(cleaned, ["COY"]) is not None
        and _find_col(cleaned, ["USD - QBO"]) is not None
        and _find_col(cleaned, KZO_METHOD_ALIASES) is not None
        and _find_col(cleaned, KZO_CHECK_ALIASES) is not None
    )
    has_kzp_shape = (
        (
            _find_col(cleaned, ["In/Out (USD)"]) is not None
            and _find_col(cleaned, ["Bank"]) is not None
            and _find_col(cleaned, ["USD - QBO"]) is None
        )
        or (
            _find_col(cleaned, ["USD - QBO"]) is not None
            and _find_col(cleaned, ["QBO Import"]) is not None
            and _find_col(cleaned, ["From Account"]) is not None
        )
    )
    has_kzdw_shape = (
        _find_col(cleaned, ["Final Amount to be take (different currency)"]) is not None
        and _find_col(cleaned, ["Transfer from"]) is not None
        and _find_col(cleaned, ["Transfer to"]) is not None
    )
    has_s5_shape = (
        _find_col(cleaned, ["CO"]) is not None
        and (
            _find_col(cleaned, ["QBO Way"]) is not None
            or _find_col(cleaned, ["QBO Import"]) is not None
        )
        and _find_col(cleaned, ["Bank", "Bank/Crypto"]) is not None
        and (
            _find_col(cleaned, ["Final Amount to be take (different currency)"]) is not None
            or _find_col(cleaned, ["Final Amount"]) is not None
        )
    )

    if is_kzdw_client:
        return "kzdw"
    if is_kzo_client and has_kzo_header_shape:
        return "kzo"
    if is_umber_client:
        return "umber"
    if is_s5_client or has_s5_shape:
        return "s5"
    if is_kzp_client or has_kzp_shape:
        return "kzp"
    if has_kzdw_shape:
        return "kzdw"
    return "legacy"


def raw_no_column(raw_df: pd.DataFrame, client_name: str) -> tuple[int | None, str]:
    """(0-based position of the column standardize_raw_df reads 'No' from, layout name).

    Position is None when the layout has no 'No' column to read. Only the headers are used.
    """
    cleaned = _clean_headers(raw_df.iloc[:0])
    layout = _raw_layout(cleaned, client_name)
    if layout == "legacy":
        pos = RAW_STANDARD_COLUMNS.index("No")
        return (pos if pos < cleaned.shape[1] else None), layout
    name = _resolve_kzo_no_col(cleaned) if layout == "kzo" else _find_col(cleaned, ["No"])
    return (list(cleaned.columns).index(name) if name is not None else None), layout


def missing_kzo_headers(raw_df: pd.DataFrame) -> list[str]:
    """Headers the KZO layout needs that this raw tab's header row lacks."""
    cleaned = _clean_headers(raw_df.iloc[:0])
    needed = [
        ("CO", _resolve_kzo_co_col(cleaned)),
        ("COY", _find_col(cleaned, ["COY"])),
        ("USD - QBO", _find_col(cleaned, ["USD - QBO"])),
        ("QBO Import Method", _find_col(cleaned, KZO_METHOD_ALIASES)),
        (KZO_CHECK_ALIASES[0], _find_col(cleaned, KZO_CHECK_ALIASES)),
    ]
    return [label for label, found in needed if found is None]
