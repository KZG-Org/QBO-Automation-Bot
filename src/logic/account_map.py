"""Account Map: resolve a sheet account name to a QBO account Id.

One resolver serves Transform, the month-end path, Sync and Reconcile, so a row marked
'Ready to sync' resolves to the same account at every stage. Design:
docs/superpowers/specs/2026-11-18-account-map-design.md (section 5.2).

Order, for one sheet name:
  1. clean the text (whitespace, ':' spacing, KZP alias table);
  2. a Manual row in the client's 'Account Map' tab: its QBO Account text first, its saved Id
     second;
  3. the exact QBO name today (exact full name, then suffix path) -- remembered as an Auto row;
  4. the Auto row's saved Id (the account was renamed in QBO);
  5. otherwise not found. Close names appear in the message only; they never decide a posting.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Callable

import pandas as pd

from src.logic.account_aliases import normalize_account_text, resolve_account_alias
from src.utils.logger import setup_logger

logger = setup_logger("account_map")

ACCOUNT_MAP_TAB = "Account Map"
ACCOUNT_MAP_HEADERS = ["Sheet Name", "QBO Account", "QBO ID", "Type", "Realm ID", "Updated At"]
AUTO = "Auto"
MANUAL = "Manual"


def _is_blank(value) -> bool:
    if value is None:
        return True
    try:
        if pd.isna(value):
            return True
    except (TypeError, ValueError):
        pass
    return str(value).strip() == ""


def map_key(text) -> str:
    """How a sheet name is looked up in the tab: spaces collapsed, case ignored."""
    if _is_blank(text):
        return ""
    return normalize_account_text(text).lower()


def normalize_qbo_id(value) -> str:
    """A QBO Id as text. Sheets may show a typed Id as '210.0'."""
    if _is_blank(value):
        return ""
    text = str(value).strip()
    if text.endswith(".0") and text[:-2].isdigit():
        text = text[:-2]
    return text


def _clean_display(text) -> str:
    return re.sub(r"\s+", " ", str(text)).strip()


@dataclass
class MapRow:
    sheet_name: str
    qbo_account: str = ""
    qbo_id: str = ""
    type: str = MANUAL
    realm_id: str = ""
    updated_at: str = ""
    row_num: int | None = None

    @property
    def is_manual(self) -> bool:
        return str(self.type or "").strip().lower() != AUTO.lower()


@dataclass(frozen=True)
class Resolution:
    account_id: str | None
    qbo_name: str | None
    how: str  # manual | exact | suffix | saved | blank | ambiguous | not_found | manual_broken
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.account_id is not None


def _quoted(names) -> str:
    return ", ".join(f"'{n}'" for n in names)


class AccountResolver:
    def __init__(
        self,
        accounts: dict[str, str],
        client_name: str = "",
        realm_id: str = "",
        rows: list[MapRow] | None = None,
        now: Callable[[], str] | None = None,
    ):
        self.client_name = str(client_name or "")
        self.realm_id = str(realm_id or "").strip()
        self._now = now or (lambda: datetime.now().strftime("%Y-%m-%d %H:%M"))

        self._fqns: list[str] = []
        self._name_by_id: dict[str, str] = {}
        self._exact: dict[str, list[str]] = {}
        self._suffix: dict[str, list[str]] = {}
        for fqn, acc_id in (accounts or {}).items():
            acc_id = normalize_qbo_id(acc_id)
            if not acc_id or _is_blank(fqn):
                continue
            fqn = str(fqn)
            self._fqns.append(fqn)
            self._name_by_id.setdefault(acc_id, fqn)
            full = normalize_account_text(fqn).lower()
            self._add(self._exact, full, acc_id)
            parts = full.split(":")
            for i in range(1, len(parts)):
                self._add(self._suffix, ":".join(parts[i:]), acc_id)

        self._manual: dict[str, MapRow] = {}
        self._auto: dict[str, MapRow] = {}
        for row in rows or []:
            key = map_key(row.sheet_name)
            if not key:
                continue
            target = self._manual if row.is_manual else self._auto
            if key in target:
                logger.warning(
                    f"[Account Map] '{row.sheet_name}' appears twice as {AUTO if not row.is_manual else MANUAL}; "
                    "using the lower row"
                )
            target[key] = row

        self._pending: dict[str, MapRow] = {}
        self._cache: dict[str, Resolution] = {}
        self._warned_realms: set[str] = set()

    @staticmethod
    def _add(index: dict[str, list[str]], key: str, acc_id: str) -> None:
        ids = index.setdefault(key, [])
        if acc_id not in ids:
            ids.append(acc_id)

    # ---------------------------------------------------------------- public API
    def resolve(self, name) -> Resolution:
        if _is_blank(name):
            return Resolution(None, None, "blank", "Missing account name")
        key = map_key(name)
        cached = self._cache.get(key)
        if cached is None:
            cached = self._resolve(name, key)
            self._cache[key] = cached
        return cached

    def account_id(self, name) -> str | None:
        return self.resolve(name).account_id

    def remember(self, name, account_id) -> None:
        """Save `name` -> `account_id` as an Auto row (used by the seeding script)."""
        acc_id = normalize_qbo_id(account_id)
        if acc_id not in self._name_by_id:
            raise ValueError(f"Account Id {account_id} is not an active QBO account")
        key = map_key(name)
        if not key or key in self._manual:
            return
        self._record_auto(name, key, acc_id, self._auto.get(key))
        self._cache.pop(key, None)

    def pending_rows(self) -> list[MapRow]:
        return list(self._pending.values())

    # ---------------------------------------------------------------- internals
    def _name_hits(self, text) -> tuple[list[str], str]:
        key = normalize_account_text(text).lower()
        exact = self._exact.get(key)
        if exact:
            return list(exact), "exact"
        return list(self._suffix.get(key, [])), "suffix"

    def _valid_saved_id(self, row: MapRow | None) -> str | None:
        if row is None:
            return None
        acc_id = normalize_qbo_id(row.qbo_id)
        realm = str(row.realm_id or "").strip()
        if not acc_id or not realm:
            return None
        if realm != self.realm_id:
            if realm not in self._warned_realms:
                self._warned_realms.add(realm)
                logger.warning(
                    f"[Account Map] ignoring rows saved for another QBO company (Realm ID {realm}); "
                    f"this run is {self.realm_id or 'unknown'}"
                )
            return None
        return acc_id if acc_id in self._name_by_id else None

    def _record_auto(self, name, key: str, acc_id: str, existing: MapRow | None) -> None:
        fqn = self._name_by_id[acc_id]
        if (
            existing is not None
            and not existing.is_manual
            and normalize_qbo_id(existing.qbo_id) == acc_id
            and existing.qbo_account == fqn
            and str(existing.realm_id or "").strip() == self.realm_id
        ):
            return
        row = MapRow(
            sheet_name=existing.sheet_name if existing is not None else _clean_display(name),
            qbo_account=fqn,
            qbo_id=acc_id,
            type=AUTO,
            realm_id=self.realm_id,
            updated_at=self._now(),
            row_num=existing.row_num if existing is not None else None,
        )
        self._pending[key] = row
        self._auto[key] = row

    def _resolve(self, name, key: str) -> Resolution:
        manual = self._manual.get(key)
        if manual is not None:
            return self._resolve_manual(key, manual)

        text = resolve_account_alias(name, self.client_name)
        ids, how = self._name_hits(text)
        auto = self._auto.get(key)
        auto_id = self._valid_saved_id(auto)

        if len(ids) == 1:
            self._record_auto(name, key, ids[0], auto)
            return Resolution(ids[0], self._name_by_id[ids[0]], how)

        if len(ids) > 1:
            if auto_id in ids:
                self._record_auto(name, key, auto_id, auto)
                return Resolution(auto_id, self._name_by_id[auto_id], "saved")
            names = sorted(self._name_by_id[i] for i in ids)
            return Resolution(
                None, None, "ambiguous",
                f"Matches more than one QBO account: {_quoted(names)}. Add it to the Account Map tab",
            )

        if auto_id:
            fqn = self._name_by_id[auto_id]
            logger.info(
                f"[Account Map] '{_clean_display(name)}' is not in QBO any more; "
                f"using saved account {auto_id} now named '{fqn}'"
            )
            self._record_auto(name, key, auto_id, auto)
            return Resolution(auto_id, fqn, "saved")

        near = difflib.get_close_matches(text, self._fqns, n=3, cutoff=0.60)
        closest = f"Closest in QBO: {_quoted(near)}. " if near else ""
        return Resolution(
            None, None, "not_found", f"{closest}Fix the name or add it to the Account Map tab"
        )

    def _resolve_manual(self, key: str, row: MapRow) -> Resolution:
        ids = self._name_hits(row.qbo_account)[0] if not _is_blank(row.qbo_account) else []
        if len(ids) == 1:
            acc_id = ids[0]
            if normalize_qbo_id(row.qbo_id) != acc_id or str(row.realm_id or "").strip() != self.realm_id:
                updated = replace(row, qbo_id=acc_id, realm_id=self.realm_id, updated_at=self._now())
                self._pending[key] = updated
                self._manual[key] = updated
            return Resolution(acc_id, self._name_by_id[acc_id], "manual")

        saved = self._valid_saved_id(row)
        if saved:
            return Resolution(saved, self._name_by_id[saved], "manual")

        problem = "which matches more than one QBO account" if len(ids) > 1 else "which is not in QBO"
        return Resolution(
            None, None, "manual_broken",
            f"Account Map row for '{row.sheet_name}' points to '{row.qbo_account}', {problem}",
        )


# ------------------------------------------------------------------ the tab itself
def _header_index(header_row) -> dict[str, int]:
    """{lowercased header: 0-based column} for the non-blank header cells."""
    return {str(h).strip().lower(): i for i, h in enumerate(header_row) if str(h).strip()}


def _cell(row, idx) -> str:
    if idx is None or idx >= len(row):
        return ""
    value = row[idx]
    return "" if value is None else str(value).strip()


def parse_account_map(values: list[list]) -> list[MapRow]:
    """Rows of the tab, columns found by header name (the team may reorder or add columns)."""
    if not values:
        return []
    cols = _header_index(values[0])
    if "sheet name" not in cols:
        logger.warning("[Account Map] the tab has no 'Sheet Name' header; ignoring it")
        return []
    get = {h: cols.get(h.lower()) for h in ACCOUNT_MAP_HEADERS}
    rows = []
    for row_num, row in enumerate(values[1:], start=2):
        name = _cell(row, get["Sheet Name"])
        if not name:
            continue
        rows.append(MapRow(
            sheet_name=name,
            qbo_account=_cell(row, get["QBO Account"]),
            qbo_id=_cell(row, get["QBO ID"]),
            type=_cell(row, get["Type"]),
            realm_id=_cell(row, get["Realm ID"]),
            updated_at=_cell(row, get["Updated At"]),
            row_num=row_num,
        ))
    return rows


def read_account_map(gs, spreadsheet_id: str) -> list[MapRow]:
    """The client's map. A missing tab is empty; any other read error is raised."""
    values = gs.read_tab_values(spreadsheet_id, ACCOUNT_MAP_TAB)
    if values is None:
        return []
    return parse_account_map(values)


def _row_values(row: MapRow) -> dict[str, str]:
    return {
        "Sheet Name": row.sheet_name,
        "QBO Account": row.qbo_account,
        "QBO ID": normalize_qbo_id(row.qbo_id),
        "Type": AUTO if not row.is_manual else (row.type or ""),
        "Realm ID": row.realm_id,
        "Updated At": row.updated_at,
    }


def write_account_map_changes(gs, spreadsheet_id: str, changes: list[MapRow]) -> int:
    """Write new and changed rows in one call. Returns the number of rows written.

    The tab is read again first and rows are matched by Sheet Name, so a sort or insert by the
    team since the run's first read cannot send an update to the wrong row. A Manual row only
    ever gets its QBO ID, Realm ID and Updated At cells written.
    """
    if not changes:
        return 0
    from gspread.utils import rowcol_to_a1

    values = gs.read_tab_values(spreadsheet_id, ACCOUNT_MAP_TAB)
    create = values is None
    updates: list[tuple[str, list[list[str]]]] = []

    if not values:
        cols = {h.lower(): i for i, h in enumerate(ACCOUNT_MAP_HEADERS)}
        updates.append(("A1", [list(ACCOUNT_MAP_HEADERS)]))
        last_row = 1
        existing: dict[str, tuple[int, bool]] = {}
    else:
        cols = _header_index(values[0])
        missing = [h for h in ACCOUNT_MAP_HEADERS if h.lower() not in cols]
        if missing:
            start = max(cols.values(), default=-1) + 1
            for offset, h in enumerate(missing):
                cols[h.lower()] = start + offset
            updates.append((rowcol_to_a1(1, start + 1), [missing]))
        last_row = max(
            (i for i, row in enumerate(values, start=1) if any(str(c).strip() for c in row)),
            default=1,
        )
        existing = {map_key(r.sheet_name): (r.row_num, r.is_manual) for r in parse_account_map(values)}

    def put(row_num: int, header: str, value: str) -> None:
        updates.append((rowcol_to_a1(row_num, cols[header.lower()] + 1), [[str(value or "")]]))

    written = 0
    for change in changes:
        key = map_key(change.sheet_name)
        cells = _row_values(change)
        if key in existing:
            row_num, sheet_is_manual = existing[key]
            if change.is_manual:
                for h in ("QBO ID", "Realm ID", "Updated At"):
                    put(row_num, h, cells[h])
            elif sheet_is_manual:
                continue  # the team took this row over since the first read
            else:
                for h in ACCOUNT_MAP_HEADERS:
                    put(row_num, h, cells[h])
        else:
            if change.is_manual:
                continue  # the team deleted the row since the first read
            last_row += 1
            existing[key] = (last_row, False)
            for h in ACCOUNT_MAP_HEADERS:
                put(last_row, h, cells[h])
        written += 1

    if written:
        gs.write_values_raw(spreadsheet_id, ACCOUNT_MAP_TAB, updates, create_if_missing=create)
    return written


def resolve_account(qbo_mappings: dict | None, name, client_name: str = "") -> Resolution:
    """Resolve through the run's resolver; build a map-less one when the run has none."""
    if qbo_mappings is None:
        return AccountResolver({}, client_name).resolve(name)
    resolver = qbo_mappings.get("account_resolver")
    if resolver is None:
        resolver = AccountResolver(qbo_mappings.get("accounts", {}) or {}, client_name)
        qbo_mappings["account_resolver"] = resolver
    return resolver.resolve(name)
