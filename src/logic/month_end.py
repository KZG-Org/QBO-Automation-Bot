"""Month-end JV rows: hand-entered journal lines posted through Transform and SYNC NOW.

A control row whose Country is `Month-end JV` points at a source tab of journal lines
(Date, Memo, Account, Amount, Name, Location, Currency Code, Class, Status). Transform groups
the lines into journals, checks them, numbers them in the client's normal journal sequence and
writes them to the row's own Transform file. The source `Status` column is the record that ties
a line to its journal: `Transformed <JV>` -> `Synced <JV>`, or `ERROR: <reason>`.

Everything here is pure (grid in, plan out) so it can be tested without Sheets or QBO. The
runners do the reads and writes. See README "Month-end JV rows".
"""

from __future__ import annotations

import math
import numbers
import re
from dataclasses import dataclass, field
from datetime import datetime

import pandas as pd

from config import settings
from src.logic.syncing import _parse_date_yyyy_mm_dd
from src.logic.transformer import (
    _build_id_prefixes,
    _is_kzdw_case,
    _is_kzo_case,
    _is_s5_or_umber_case,
    _normalize_currency_code,
    find_id_in_map,
    journal_columns,
)

REQUIRED_HEADERS = ("Date", "Memo", "Account", "Amount")
OPTIONAL_HEADERS = ("Name", "Location", "Currency Code", "Class")
STATUS_HEADER = "Status"
_KNOWN_HEADERS = {h.lower(): h for h in REQUIRED_HEADERS + OPTIONAL_HEADERS + (STATUS_HEADER,)}

# Values compared by the "source changed during Transform" check.
_VERIFY_COLUMNS = ("Date", "Account", "Amount")

_STATUS_RE = re.compile(r"^(transformed|synced)\s+(\S+)\s*$", re.IGNORECASE)
_SIBLING_ERROR = "Another line in this journal has an error"


class MonthEndInputError(Exception):
    """The source tab cannot be planned at all. The message is shown in Transform Note."""


def _norm_text(value) -> str:
    if value is None:
        return ""
    try:
        if pd.isna(value):  # NaN and pd.NA (blank control cells read as NA)
            return ""
    except (TypeError, ValueError):
        pass
    return " ".join(str(value).split())


def _has_month_end_marker(value) -> bool:
    return "monthend" in re.sub(r"[\s_\-]+", "", _norm_text(value).lower())


def is_month_end_row(country, tab_name="") -> bool:
    """A Month-end JV control row: Country or Tab Name contains 'Month-end'.

    Any spelling counts ('Month-end', 'Monthend', 'Month end', 'month_end'), so a row whose
    Country was left blank is still recognised by its tab ('Month-end JV_test').
    """
    return _has_month_end_marker(country) or _has_month_end_marker(tab_name)


def parse_status(value) -> tuple[str, str | None]:
    """('new' | 'transformed' | 'synced' | 'unknown', journal no or None)."""
    text = _norm_text(value)
    if text == "" or text.upper().startswith("ERROR"):
        return "new", None
    match = _STATUS_RE.match(text)
    if match:
        return match.group(1).lower(), match.group(2)
    return "unknown", None


@dataclass
class SourceTab:
    df: pd.DataFrame  # index = sheet row number; canonical column names
    status_col: int  # 1-based sheet column of Status (where it is, or where it will be added)
    has_status_header: bool


def read_source_tab(values: list[list]) -> SourceTab:
    """Build the source frame from a raw cell grid (header on row 1)."""
    if not values:
        raise MonthEndInputError("the tab is empty or missing (header must be on row 1)")
    header = [_norm_text(h) for h in values[0]]
    positions: dict[str, int] = {}
    for idx, name in enumerate(header):
        canonical = _KNOWN_HEADERS.get(name.lower())
        if canonical and canonical not in positions:
            positions[canonical] = idx

    for name in REQUIRED_HEADERS:
        if name not in positions:
            raise MonthEndInputError(
                f"row 1 is missing the header '{name}'. Headers must be: "
                "Date, Memo, Account, Amount, Name, Location, Currency Code, Class, Status"
            )

    has_status = STATUS_HEADER in positions
    if has_status:
        status_col = positions[STATUS_HEADER] + 1
    else:
        last_filled = max(i for i, h in enumerate(header) if h)
        status_col = last_filled + 2

    columns = list(REQUIRED_HEADERS + OPTIONAL_HEADERS + (STATUS_HEADER,))
    records, index = [], []
    for offset, row in enumerate(values[1:]):
        record = {}
        for name in columns:
            pos = positions.get(name)
            record[name] = row[pos] if pos is not None and pos < len(row) else ""
        if all(_norm_text(record[name]) == "" for name in REQUIRED_HEADERS + OPTIONAL_HEADERS):
            continue
        records.append(record)
        index.append(offset + 2)
    df = pd.DataFrame(records, index=index, columns=columns)
    return SourceTab(df=df, status_col=status_col, has_status_header=has_status)


def _parse_date(value) -> pd.Timestamp | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, numbers.Real):
        if math.isnan(value) or not -60000 <= value <= 120000:
            return None
        return (pd.Timestamp("1899-12-30") + pd.Timedelta(days=float(value))).normalize()
    if isinstance(value, (datetime, pd.Timestamp)):
        return pd.Timestamp(value).normalize()
    text = _norm_text(value)
    if not text:
        return None
    parsed = pd.to_datetime(text, dayfirst=True, errors="coerce")
    return None if pd.isna(parsed) else parsed.normalize()


def _parse_amount(value) -> float | None:
    """Number as typed. None when blank. Raises ValueError when not a number."""
    if isinstance(value, bool):
        raise ValueError(value)
    if isinstance(value, numbers.Real):
        return None if math.isnan(value) else float(value)
    text = _norm_text(value)
    if not text:
        return None
    negative = text.startswith("(") and text.endswith(")")
    cleaned = text.strip("()").replace(",", "").replace("$", "").strip()
    number = float(cleaned)
    return -number if negative else number


def _has_more_than_2_decimals(amount: float) -> bool:
    cents = amount * 100
    return abs(cents - round(cents)) >= 1e-6


def _fix_grp(text: str) -> str:
    return "GROUP" if text.upper() == "GRP" else text


@dataclass
class MonthEndPlan:
    journals: pd.DataFrame
    status_updates: dict[int, str]
    last_journal_no: int
    new_journal_nos: list[str] = field(default_factory=list)
    rebuilt_journal_nos: list[str] = field(default_factory=list)
    failed_journal_count: int = 0
    error_line_count: int = 0
    outside_month_count: int = 0
    unknown_status_count: int = 0
    rows_to_verify: list[int] = field(default_factory=list)


def _line_error(row: pd.Series, client_name: str, qbo_mappings: dict) -> str | None:
    if _parse_date(row["Date"]) is None:
        return "Date missing or not readable"
    account = _norm_text(row["Account"])
    if not account:
        return "Missing Account"
    try:
        amount = _parse_amount(row["Amount"])
    except ValueError:
        return f"Amount '{_norm_text(row['Amount'])}' is not a number"
    if amount is None or amount == 0:
        return "Missing Amount"
    if _has_more_than_2_decimals(amount):
        return f"Amount {_norm_text(row['Amount'])} has more than 2 decimals. Use ROUND(...,2) in the sheet"
    if not find_id_in_map(qbo_mappings.get("accounts", {}), account, allow_fuzzy=False, client_name=client_name):
        return f"Account not found: '{account}'"
    if not _is_s5_or_umber_case(client_name):
        location = _fix_grp(_norm_text(row["Location"]))
        if location and not find_id_in_map(qbo_mappings.get("locations", {}), location):
            return f"Location not found: '{location}'"
        # Only a typed Class is checked; a Class copied from Location is left to sync, as on
        # normal rows.
        cls = _fix_grp(_norm_text(row["Class"]))
        if cls and not find_id_in_map(qbo_mappings.get("classes", {}), cls):
            return f"Class not found: '{cls}'"
    currency = _normalize_currency_code(row["Currency Code"])
    if not _is_kzdw_case(client_name) and currency != "USD":
        return f"{client_name} journals post in USD only (Currency Code '{_norm_text(row['Currency Code'])}')"
    return None


def _journal_error(lines: pd.DataFrame, client_name: str) -> str | None:
    if _is_kzdw_case(client_name):
        if lines["Currency Code"].apply(_normalize_currency_code).nunique() > 1:
            return "Mixed currencies in one journal"
    total = math.fsum(_parse_amount(v) for v in lines["Amount"])
    if abs(total) >= 0.005:
        return f"Journal does not balance (diff {round(total, 2):.2f})"
    return None


def _output_rows(lines: pd.DataFrame, journal_no: str, client_name: str) -> list[dict]:
    blank_loc = _is_s5_or_umber_case(client_name)
    rows = []
    for _, line in lines.iterrows():
        currency = _normalize_currency_code(line["Currency Code"])
        location = "" if blank_loc else _fix_grp(_norm_text(line["Location"]))
        cls = "" if blank_loc else (_fix_grp(_norm_text(line["Class"])) or location)
        rows.append({
            "No": "",
            "Journal No": journal_no,
            "Date": _parse_date(line["Date"]).strftime("%Y-%m-%d"),
            "Memo": _norm_text(line["Memo"]),
            "Account": _norm_text(line["Account"]),
            "Amount": _parse_amount(line["Amount"]),
            "Name": _norm_text(line["Name"]),
            "Location": location,
            "Currency Code": currency,
            "Currency Exchange": 1 if currency == "USD" else "",
            "Class": cls,
            "Remarks": "Ready to sync",
        })
    return rows


def plan_month_end(
    source: SourceTab,
    *,
    client_name: str,
    month_start: datetime,
    month_end: datetime,
    start_jv: int,
    jv_in_tab: set[str],
    qbo_mappings: dict,
) -> MonthEndPlan:
    """Decide what Transform writes: Transform-tab lines, Status texts and the last number used."""
    prefix = _build_id_prefixes(client_name)[0]
    own_number = re.compile(rf"^{re.escape(prefix)}\d+$")
    df = source.df

    parsed_status = {row: parse_status(df.at[row, STATUS_HEADER]) for row in df.index}
    for kind, jv in parsed_status.values():
        if kind in ("transformed", "synced") and not own_number.match(jv):
            raise MonthEndInputError(
                f"the tab has {jv}, which is not a {prefix} journal. Each company needs its own month-end tab"
            )

    start = pd.Timestamp(month_start).normalize()
    end = pd.Timestamp(month_end)
    new_rows: list[int] = []
    rebuild: dict[str, list[int]] = {}
    outside = unknown = 0
    for row in df.index:
        kind, jv = parsed_status[row]
        if kind == "synced":
            continue
        date = _parse_date(df.at[row, "Date"])
        in_month = date is None or start <= date <= end
        if not in_month:
            if kind == "new":
                outside += 1
            continue
        if kind == "unknown":
            unknown += 1
        elif kind == "new":
            new_rows.append(row)
        elif jv not in jv_in_tab:
            rebuild.setdefault(jv, []).append(row)

    kzo_key = _is_kzo_case(client_name)
    groups: dict[tuple, list[int]] = {}
    for row in new_rows:
        date = _parse_date(df.at[row, "Date"])
        key = (date, _norm_text(df.at[row, "Memo"]).lower())
        if kzo_key:
            key += (_norm_text(df.at[row, "Account"]).lower(),)
        groups.setdefault(("new",) + key, []).append(row)
    for jv, rows in rebuild.items():
        groups[("rebuild", jv)] = rows

    status_updates: dict[int, str] = {}
    out_rows: list[dict] = []
    new_nos: list[str] = []
    rebuilt_nos: list[str] = []
    failed = 0
    current = start_jv
    for key, rows in sorted(groups.items(), key=lambda item: min(item[1])):
        lines = df.loc[rows]
        line_errors = {row: _line_error(lines.loc[row], client_name, qbo_mappings) for row in rows}
        journal_error = None if any(line_errors.values()) else _journal_error(lines, client_name)
        if any(line_errors.values()) or journal_error:
            failed += 1
            for row in rows:
                status_updates[row] = f"ERROR: {line_errors[row] or journal_error or _SIBLING_ERROR}"
            continue
        if key[0] == "rebuild":
            journal_no = key[1]
            rebuilt_nos.append(journal_no)
        else:
            current += 1
            journal_no = f"{prefix}{str(current).zfill(4)}"
            new_nos.append(journal_no)
            for row in rows:
                status_updates[row] = f"Transformed {journal_no}"
        out_rows.extend(_output_rows(lines, journal_no, client_name))

    # Only write what changes, so a re-run does not rewrite identical cells.
    status_updates = {
        row: text for row, text in status_updates.items()
        if _norm_text(df.at[row, STATUS_HEADER]) != text
    }
    error_lines = sum(1 for rows in groups.values() for row in rows
                      if status_updates.get(row, _norm_text(df.at[row, STATUS_HEADER])).startswith("ERROR"))
    journals = pd.DataFrame(out_rows, columns=journal_columns(client_name))
    return MonthEndPlan(
        journals=journals,
        status_updates=status_updates,
        last_journal_no=current,
        new_journal_nos=new_nos,
        rebuilt_journal_nos=rebuilt_nos,
        failed_journal_count=failed,
        error_line_count=error_lines,
        outside_month_count=outside,
        unknown_status_count=unknown,
        rows_to_verify=sorted(row for rows in groups.values() for row in rows),
    )


def changed_rows(before: SourceTab, after: SourceTab, rows: list[int]) -> list[int]:
    """Rows whose Date, Account or Amount differ between two reads (or that are gone)."""
    changed = []
    for row in rows:
        if row not in after.df.index or row not in before.df.index:
            changed.append(row)
            continue
        if any(_norm_text(before.df.at[row, c]) != _norm_text(after.df.at[row, c]) for c in _VERIFY_COLUMNS):
            changed.append(row)
    return changed


# ---- Sync ----

CLASH_REMARK = "ERROR: {jv} is already used in QBO by a different journal"


def find_number_clashes(
    to_sync: pd.DataFrame, existing_docs: set[str], qbo_journals: dict[str, list[tuple[str, str]]]
) -> set[str]:
    """Journal numbers already in QBO whose QBO journal is a different one.

    Sync skips a number that is already in QBO as "already synced". That is right when it is
    this same journal (an earlier sync that did not get to write its status), and silently
    loses the journal when the number was taken by another one. Same journal = same TxnDate
    and same memo (push_journal writes the first line's Memo as PrivateNote). Amounts are not
    compared: an edited rebuild of a journal that is already posted is still that journal and
    must be skipped, not posted again. A number whose QBO details could not be fetched is not
    judged, so it keeps today's skip.
    """
    clashes = set()
    for jv, group in to_sync.groupby("Journal No"):
        jv = str(jv)
        if jv not in existing_docs or jv not in qbo_journals:
            continue
        first = group.iloc[0]
        date = _parse_date_yyyy_mm_dd(first["Date"])
        memo = _norm_text(first.get("Memo", "")).lower()
        if not any(d == date and _norm_text(note).lower() == memo for d, note in qbo_journals[jv]):
            clashes.add(jv)
    return clashes


def synced_journal_nos(tab_df: pd.DataFrame) -> set[str]:
    """Journals the Transform tab already shows as posted (synced now or found in QBO)."""
    if tab_df.empty or "Remarks" not in tab_df.columns:
        return set()
    remarks = tab_df["Remarks"].astype(str).str.strip()
    done = remarks.str.startswith("Synced") | remarks.str.startswith("Skipper")
    return {str(jv) for jv in tab_df.loc[done, "Journal No"]}


def sync_status_updates(source: SourceTab, synced_jvs: set[str]) -> list[dict]:
    """Source Status cells to change after a sync: `Transformed <JV>` -> `Synced <JV>`.

    Sync never writes ERROR to the source: an ERROR line is new to Transform, which would give
    it a new number and post it again while the first copy may already be in QBO. Sync
    failures stay in the Transform tab's Remarks and the control row's QBO Journal cell.
    """
    updates = []
    for row in source.df.index:
        kind, jv = parse_status(source.df.at[row, STATUS_HEADER])
        if kind == "transformed" and jv in synced_jvs:
            updates.append({"row": row, "col": source.status_col, "val": f"Synced {jv}"})
    return updates


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def transform_note(plan: MonthEndPlan, tab_name: str, month_label: str) -> str:
    parts = []
    if plan.new_journal_nos:
        nos = plan.new_journal_nos
        span = nos[0] if len(nos) == 1 else f"{nos[0]} to {nos[-1]}"
        parts.append(f"{_plural(len(nos), 'journal')} ready: {span}.")
    if plan.rebuilt_journal_nos:
        parts.append(f"Rebuilt: {', '.join(plan.rebuilt_journal_nos)}.")
    if plan.failed_journal_count:
        verb = "has" if plan.failed_journal_count == 1 else "have"
        parts.append(f"{_plural(plan.failed_journal_count, 'journal')} {verb} errors: see Status in '{tab_name}'.")
    if plan.outside_month_count:
        parts.append(f"{_plural(plan.outside_month_count, 'line')} dated outside {month_label} left for later.")
    if plan.unknown_status_count:
        parts.append(f"{_plural(plan.unknown_status_count, 'line')} skipped: Status not recognised.")
    return " ".join(parts) or "Nothing new to transform."
