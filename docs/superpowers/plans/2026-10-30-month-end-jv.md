# Month-end JV Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Post hand-entered month-end journals through the bot (Transform, then SYNC NOW) for every company, with no duplicate or reused journal numbers.

**Architecture:** A pure planning module (`src/logic/month_end.py`) turns the source tab grid into journals, status texts and numbers. `run_ingestion.py` branches to `_process_month_end_row()` for rows whose Country is `Month-end JV`; `run_syncing.py` adds a clash check and a source status write-back for those rows; `run_reconciliation.py` skips the raw check for them. Normal rows gain one fix: `Last Journal No` is saved before the Transform write.

**Tech Stack:** Python 3, pandas, gspread, unittest (`.venv\python.exe -m unittest`), Google Apps Script via clasp.

**Spec:** `docs/superpowers/specs/2026-10-30-month-end-jv-design.md`

## Global Constraints

- Country marker: `Month-end JV` (trim, collapse spaces, ignore case).
- Source header row 1; required headers `Date, Memo, Account, Amount`; optional `Name, Location, Currency Code, Class`; bot-owned `Status`.
- Status texts: `Transformed <JV>`, `Synced <JV>`, `ERROR: <reason>`.
- Grouping: KZO family (`_is_kzo_case`) = Date + Account + Memo; all others = Date + Memo. Text keys: trim, collapse spaces, lower case.
- No rounding: each Amount at most 2 decimals (`abs(x*100 - round(x*100)) < 1e-6`); journal sum `abs(sum) < 0.005`.
- Currency: KZO/KZP/S5/UMBER blank or USD only; KZDW family one currency per journal; Currency Exchange `1` for USD, blank otherwise.
- Class blank → Location; S5/UMBER Location and Class blank.
- Journal No prefix `_build_id_prefixes(client_name)[0]`, zero-padded to 4 like the transformer.
- Template: `Sample - Journals_monthend` if the control sheet has it, else `Sample - Journals`.
- Output tab `{Country} {Mon yy} - Journals`, column order = the client's journal `cols_order` in `transformer.py:963-965`, `No` blank, Remarks `Ready to sync`.
- Normal-row behavior must not change except `Last Journal No` timing and the in-run counter refresh.
- README updated in the same commit as the behavior (user rule).

## Review Focus

1. A source tab with blank rows between entries: sheet row must come from the frame index label, not position. Test in Task 1.
2. Amount typed as text (`"1,234.50"`) or a formula with long decimals: text parses, long decimals → ERROR naming the row. Test in Task 1.
3. Status cleared by hand on a Transformed line while the JV is still in the tab: treated as new (documented risk); a Transformed line whose JV is gone is rebuilt under the same number. Test in Task 1.
4. Status write fails (no Editor access): nothing appended, `Transform` = `ERROR`, numbers already reserved. Test in Task 2.
5. Two READY rows of one company in one run: second row starts after the first row's numbers. Test in Task 3.

---

### Task 1: Planning module

**Files:**
- Modify: `config/settings.py` (add `MONTH_END_COUNTRY = "Month-end JV"`, `MONTH_END_TEMPLATE_TAB = "Sample - Journals_monthend"`, `JOURNAL_TEMPLATE_TAB = "Sample - Journals"`)
- Create: `src/logic/month_end.py`
- Test: `tests/test_month_end.py`

**Interfaces (Produces):**
- `class MonthEndInputError(Exception)` — message is the Transform Note text.
- `is_month_end_row(country: object) -> bool`
- `parse_status(value: object) -> tuple[str, str | None]` — kind in `{"new", "transformed", "synced", "unknown"}`; `new` for blank or text starting `ERROR`.
- `@dataclass SourceTab(df: pd.DataFrame, status_col: int, has_status_header: bool)` — `df.index` = sheet row numbers; columns canonical names.
- `read_source_tab(values: list[list]) -> SourceTab` — raises `MonthEndInputError("row 1 is missing the header 'Account' ...")`.
- `@dataclass MonthEndPlan(journals, status_updates: dict[int,str], last_journal_no: int, new_journal_nos: list[str], failed_journal_count: int, error_line_count: int, outside_month_count: int, unknown_status_count: int, rows_to_verify: list[int])`
- `plan_month_end(source: SourceTab, *, client_name: str, month_start, month_end, start_jv: int, jv_in_tab: set[str], qbo_mappings: dict) -> MonthEndPlan` — raises `MonthEndInputError` when a Status names another company's prefix.
- `changed_rows(before: SourceTab, after: SourceTab, rows: list[int]) -> list[int]` — compares Date, Account, Amount.
- `transform_note(plan: MonthEndPlan, tab_name: str, month_label: str) -> str`

- [ ] **Step 1: Write failing tests** in `tests/test_month_end.py`, using the user's test data (9 KZO lines, 31/10/2026, `AI Expenses`, TH 11702.52 … GROUP -17167.88 … BD 702.46) and rows 11-12 (`AI Expenses` 500 / `Account B` -500, memo `Month-End`). Assertions:
  - KZO: rows 2-10 → one journal `KZO-JV1418` from `start_jv=1417`; rows 11-12 → ERROR (two one-account journals, each unbalanced).
  - KZP: rows 11-12 → one journal `KZP-JV0006` from `start_jv=5`.
  - Output columns equal the client's `cols_order`; `No` blank; Remarks `Ready to sync`; Date `2026-10-31`; Amount unchanged.
  - Status updates `{2: "Transformed KZO-JV1418", …}`; failed lines get `ERROR: …`, sibling lines `ERROR: Another line in this journal has an error`.
  - Unknown account, unknown location, unknown class, missing amount, `11702.5197`, unbalanced (diff shown), KZO `THB`, KZDW mixed currencies → each its ERROR text; failed journals take no number.
  - `Transformed KZO-JV1400` with `KZO-JV1400` not in `jv_in_tab` → rebuilt under `KZO-JV1400`, no status write; in `jv_in_tab` → skipped; `Synced …` → skipped.
  - `Transformed KZP-JV0012` in a KZO run → `MonthEndInputError`.
  - Line dated 2026-09-30 → not planned, `outside_month_count == 1`.
  - Blank row between entries keeps the right sheet rows.
  - Class blank → Location; S5 → Location and Class blank; KZDW USD → Currency Exchange `1`.
  - `read_source_tab` without `Status` → `has_status_header False`, `status_col` = last non-empty header + 1; without `Account` → raises.
  - `is_month_end_row(" month-end  jv ")` True; `"TH"` False.

- [ ] **Step 2: Run** `.venv\python.exe -m unittest tests.test_month_end` — expect ImportError.
- [ ] **Step 3: Implement** `src/logic/month_end.py`. Reuse `parse_mixed_date` (dates read UNFORMATTED: serials or `dd/mm/yyyy` text — parse text with `dayfirst=True`), `find_id_in_map` (accounts `allow_fuzzy=False, client_name=...`; locations and classes with the same call style as `process_journals`), `_build_id_prefixes`, `_is_kzo_case`, `_is_kzdw_case`, `_is_s5_or_umber_case`, `_normalize_currency_code`.
- [ ] **Step 4: Run** the module tests — expect PASS.

### Task 2: Ingestion branch for month-end rows

**Files:**
- Modify: `src/connectors/gsheets_client.py` (add `worksheet_titles(spreadsheet_url_or_id) -> list[str]`)
- Modify: `run_ingestion.py` (add `_process_month_end_row`, branch after step 4)
- Test: `tests/test_month_end.py`

**Interfaces:**
- Consumes: Task 1.
- Produces: `_process_month_end_row(gs, control_sheet_id: str, ctrl_df: pd.DataFrame, row: pd.Series, row_num: int, client_name: str, qbo_mappings: dict, transform_url: str, start_jv: int) -> int` — returns the highest journal number now reserved (≥ `start_jv`).

Order inside (spec 6.5): one-row-per-month guard → read Transform tab, delete lines of any JV whose Remarks contain `ERROR`, collect `jv_in_tab` → read source (`read_values`, UNFORMATTED) → plan → re-read and `changed_rows` → write `Last Journal No` → add `Status` header if missing → batch-write statuses → append with template → control row (`Transform` DONE, `QBO Journal` ERROR / READY TO SYNC / unchanged when nothing new, `Transform Note`, `Last Run At`). Any `MonthEndInputError` or status-write failure → `Transform` = `ERROR` + note, return without appending.

- [ ] **Step 1: Failing tests** with `MagicMock` gs: happy path call order (control `Last Journal No` write before `batch_update_cells` on the source before `append_or_create_df`); status write raises → no append, Transform ERROR, note mentions Editor access; changed source → nothing written but control; missing Status header → `update_cell(source, tab, 1, col, "Status")`; second `Month-end JV` row with the same Month → ERROR naming the other row; template falls back to `Sample - Journals` when `worksheet_titles` lacks the month-end one.
- [ ] **Step 2: Run** — expect FAIL.
- [ ] **Step 3: Implement** and branch in `process_client_control_sheet` right after the tab names are built (`run_ingestion.py` step 4): `if is_month_end_row(country): global_last_jv = max(global_last_jv, _process_month_end_row(...)); continue`.
- [ ] **Step 4: Run** — expect PASS.

### Task 3: Normal rows reserve numbers first

**Files:** Modify `run_ingestion.py` (steps 12-15); Test `tests/test_month_end.py`

- [ ] **Step 1: Failing test** for `_reserve_journal_numbers(gs, control_sheet_id, ctrl_df, row_num, last_journal_no: int, global_last_jv: int) -> int`: writes `Last Journal No` and returns `max(...)`.
- [ ] **Step 2: Implement**; call it right after `transform_raw` (before step 13) and drop `COL_LAST_JV` from the step 15 dict. Assign the return value to `global_last_jv`.
- [ ] **Step 3: Run** full suite — expect PASS (166 existing + new).

### Task 4: Sync clash check and status write-back

**Files:** Modify `src/logic/syncing.py` (add `QBOSync.get_journal_totals(doc_nums: list) -> dict[str, list[tuple[str, float]]]`, query `SELECT DocNumber, TxnDate, TotalAmt FROM JournalEntry WHERE DocNumber IN (...)`, chunks of 50); modify `src/logic/month_end.py` (add `find_number_clashes(to_sync, existing_docs, qbo_journals) -> set[str]` and `sync_status_updates(source: SourceTab, synced_jvs: set[str], clash_jvs: set[str]) -> list[dict]`); modify `run_syncing.py` journal section; Test `tests/test_month_end.py`.

- Clash = JV in `existing_docs` where no QBO record has the same `TxnDate` and `abs(TotalAmt - sum of positive Amounts) < 0.005`.
- Clash lines: Remarks `ERROR: <JV> is already used in QBO by a different journal`, section fails; source Status `ERROR: <JV> is already used in QBO by a different journal. Run Transform to give it a new number.`
- `synced_jvs` = JVs whose tab Remarks start `Synced` or `Skipper` before this run, plus JVs posted or confirmed-skipped in this run. Source lines `Transformed <JV>` → `Synced <JV>`. Write-back failure logs a warning only.

- [ ] **Step 1: Failing tests** for both helpers (match, date differs, total differs, duplicate DocNumbers where one matches).
- [ ] **Step 2: Implement** and wire into `process_client_sync` only when `is_month_end_row(country)`.
- [ ] **Step 3: Run** full suite — expect PASS.

### Task 5: Reconcile, Apps Script, docs, commit

**Files:** `run_reconciliation.py` (skip raw fetch when `is_month_end_row(country)`), `appscript/kzo/Transform Button.js` (`getDialogData_` and `getTargetsForExecution` skip Country `Month-end JV`, case-insensitive), `appscript/README.md`, `README.md` (new section "Month-end JV rows"), spec + plan docs.

- [ ] **Step 1:** Edit; run full suite — expect PASS.
- [ ] **Step 2:** Commit spec + plan, then one feature commit (code + tests + README + appscript).

### Task 6: Deploy

- [ ] `git push origin main`; on `QBO_Accounting`: `git pull --ff-only origin main`, `sudo systemctl restart qbo-bot.service`, `systemctl is-active qbo-bot.service` → `active`.
- [ ] `appscript/kzo`: `clasp pull` first; re-apply the edit only if the pulled file differs; `clasp push`.
