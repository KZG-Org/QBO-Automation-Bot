"""Spot a broken raw 'No' column before Transform and tell the user how to fix it.

Every client numbers its raw rows with one array formula (ARRAYFORMULA / SEQUENCE) at the
top of the 'No' column. Two things break it:

* the formula is blocked: something typed further down the column stops it from filling,
  so the formula cell shows #REF! and no row below it gets a No;
* one row's inputs are broken (a #REF! Date or QBO Import cell), so that row's No is an
  error while the rest fill normally.

Either way ingestion would silently skip rows, so Transform stops and names the cells.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

import pandas as pd
from gspread.utils import rowcol_to_a1

from src.logic.raw_adapter import KZO_CHECK_ALIASES, missing_kzo_headers, raw_no_column

SHEETS_ERROR = re.compile(r"^#(REF!|N/A|VALUE!|NAME\?|DIV/0!|NUM!|NULL!|ERROR!)")
ARRAY_FORMULA = re.compile(r"\b(ARRAYFORMULA|SEQUENCE)\s*\(", re.IGNORECASE)
MAX_LISTED = 10


@dataclass
class NoColumnProblem:
    # (formula cell, cells blocking it), e.g. ("AA5832", ["AA6001", "AA6002"])
    blocked: list = field(default_factory=list)
    # (sheet row, error text) for rows whose own No is an error
    bad_rows: list = field(default_factory=list)


def is_sheets_error(value) -> bool:
    return isinstance(value, str) and bool(SHEETS_ERROR.match(value.strip()))


def diagnose(values_by_row: dict, formulas: list, first_row: int, col_letter: str) -> NoColumnProblem | None:
    """Classify error cells in one column.

    values_by_row: {sheet row: displayed value} for the column (blank rows may be absent).
    formulas: the column read with FORMULA rendering, starting at first_row.
    """
    # UNFORMATTED_VALUE reads give "#REF! (Reference does not exist.)"; keep just the code.
    errors = [(row, SHEETS_ERROR.match(str(v).strip()).group(0)) for row, v in sorted(values_by_row.items()) if is_sheets_error(v)]
    if not errors:
        return None
    error_rows = {row for row, _ in errors}
    array_rows = [first_row + i for i, f in enumerate(formulas) if ARRAY_FORMULA.search(str(f or ""))]
    problem = NoColumnProblem()
    blocked_rows = set()
    for row in array_rows:
        if row not in error_rows:
            continue  # this formula filled normally
        next_array = min((r for r in array_rows if r > row), default=None)
        blockers = [
            f"{col_letter}{first_row + i}"
            for i, f in enumerate(formulas)
            if first_row + i > row and (next_array is None or first_row + i < next_array) and str(f or "").strip()
        ]
        problem.blocked.append((f"{col_letter}{row}", blockers))
        blocked_rows.add(row)
    problem.bad_rows = [(row, err) for row, err in errors if row not in blocked_rows]
    return problem


def _listed(items: list) -> str:
    shown = ", ".join(str(x) for x in items[:MAX_LISTED])
    return shown + (f" (+{len(items) - MAX_LISTED} more)" if len(items) > MAX_LISTED else "")


def problem_message(problem: NoColumnProblem, where: str) -> str:
    parts = []
    for cell, blockers in problem.blocked:
        if blockers:
            parts.append(f"the No formula in {cell} is blocked by text in {_listed(blockers)}. Delete that text")
        else:
            parts.append(f"the No formula in {cell} shows an error. Check that formula")
    if problem.bad_rows:
        rows = [row for row, _ in problem.bad_rows]
        errs = "/".join(sorted({err for _, err in problem.bad_rows}))
        which = "that row" if len(rows) == 1 else "those rows"
        parts.append(
            f"No shows {errs} in {'row' if len(rows) == 1 else 'rows'} {_listed(rows)} "
            f"(usually a broken Date or QBO Import cell). Fix or delete {which}"
        )
    return f"⛔ {where}: " + "; ".join(parts) + ", then set Transform to READY again."


def check_raw_no_column(gs, source_url: str, raw_tab_name: str, raw_df: pd.DataFrame,
                        client_name: str, country: str) -> str | None:
    """User-facing message when the raw tab's No column is broken, else None.

    raw_df is the tab as read (before standardizing), with attrs["header_row"] set by the
    reader. The column's formulas are fetched only when an error is present.
    """
    header_row = raw_df.attrs.get("header_row") if raw_df is not None else None
    if raw_df is None or raw_df.empty or not header_row:
        return None
    is_kzo = "kzo" in str(client_name).lower()
    if is_kzo:
        where = f"{country} COY raw file (tab '{raw_tab_name}')"
    else:
        where = f"{client_name} raw file (tab '{raw_tab_name}')"
    then = "then set Transform to READY again."

    # Use the same column the transform will read 'No' from.
    idx, layout = raw_no_column(raw_df, client_name)
    if is_kzo and layout != "kzo":
        # The legacy positional layout is only right for old tabs whose No really sits there.
        legacy = raw_df.iloc[:, idx] if idx is not None else pd.Series(dtype=object)
        if pd.to_numeric(legacy, errors="coerce").notna().sum() == 0:
            missing = missing_kzo_headers(raw_df) or [KZO_CHECK_ALIASES[0]]
            names = ", ".join(f"'{m}'" for m in missing)
            noun = "the header" if len(missing) == 1 else "the headers"
            hint = " (it must sit just left of the No column)" if KZO_CHECK_ALIASES[0] in missing else ""
            return (f"⛔ {where}: row {header_row} is missing {noun} {names}, so the bot cannot find "
                    f"the No column. Put {'it' if len(missing) == 1 else 'them'} back{hint}, {then}")
    if idx is None:
        return (f"⛔ {where}: row {header_row} has no 'No' header, so the bot cannot number the rows. "
                f"Put it back, {then}")
    column = raw_df.iloc[:, idx]
    values_by_row = {int(i) + header_row + 1: v for i, v in column.items()}
    if not any(is_sheets_error(v) for v in values_by_row.values()):
        return None

    col_letter = re.sub(r"\d", "", rowcol_to_a1(1, idx + 1))
    first_row = header_row + 1
    try:
        cells = gs.open(source_url).worksheet(raw_tab_name).get(f"{col_letter}{first_row}:{col_letter}", value_render_option="FORMULA")
        formulas = [c[0] if c else "" for c in cells]
    except Exception:
        formulas = []  # still report the error rows; blockers need the formulas
    problem = diagnose(values_by_row, formulas, first_row, col_letter)
    return problem_message(problem, where) if problem else None
