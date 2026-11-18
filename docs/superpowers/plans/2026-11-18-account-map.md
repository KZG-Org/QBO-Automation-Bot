# Account Map Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Resolve sheet account names to QBO accounts through one shared resolver backed by an
`Account Map` tab, so a QBO rename no longer breaks Transform, Sync or Reconcile, and no stage
guesses an account from a similar name.

**Architecture:** New module `src/logic/account_map.py` holds the resolver (5 steps of spec 5.2)
and the tab read/write. Transform, the month-end path, Sync and Reconcile all call it. Only
Transform writes the tab. Reconcile compares QBO account Ids instead of names.

**Tech Stack:** Python 3.11, pandas, gspread (via `GSheetsClient`), QBO REST (via `QBOClient`),
`unittest`. Run tests with `PYTHONIOENCODING=utf-8 .venv/python.exe -m unittest <modules>`
(no pytest locally).

**Spec:** `docs/superpowers/specs/2026-11-18-account-map-design.md`

## Global Constraints

- Tab name: `Account Map`. Header row 1, exactly: `Sheet Name`, `QBO Account`, `QBO ID`, `Type`, `Realm ID`, `Updated At`.
- `Type` values: `Auto`, `Manual`. Blank Type counts as `Manual`.
- `Updated At` format: `%Y-%m-%d %H:%M` (server local time).
- Map key: `normalize_account_text(text).lower()` of the sheet text BEFORE the KZP alias table.
- Accounts: no fuzzy, no closest-name posting anywhere. Closest names (up to 3, cutoff 0.60) appear only in ERROR text.
- The bot never writes `Sheet Name`, `QBO Account` or `Type` of a Manual row.
- A `QBO ID` counts only when the row's `Realm ID` equals the company being processed.
- Map writes use `value_input_option="RAW"`, all values as strings.
- Transform ERROR text keeps its current prefixes (`ERROR | Account not found: '<name>'`, `ERROR | Source Account not in QBO: '<name>'`, `Expense Account not in QBO`, `'From' Account not in QBO`, `'To' Account not in QBO`).
- Locations, Classes, Vendors, Payment Methods keep `find_id_in_map` / `find_id` with fuzzy, unchanged.
- KZO / KZP / KZDW / S5 / UMBER / TINDERPAY behavior is unchanged apart from account resolution.
- Every behavior change is documented in `README.md` in the same commit set.

## Review Focus

1. Sheet text with different case or extra spaces than the map row (`"  leading card ACCT - 3214 "`) must hit the row. Test in Task 1.
2. Team adds their own columns or reorders columns in the tab: read and write by header name, never by position. Test in Task 2.
3. A `QBO ID` cell the team typed that Sheets shows as `210.0` or ` 210 `: normalize to `210`. Test in Task 1.
4. A month-end JV row that uses a renamed account name must pass validation through the Auto row. Test in Task 3.
5. An account renamed between Transform and Sync: Sync must still resolve through the Auto row Transform saved. Test in Task 4.

---

### Task 1: Resolver core

**Files:**
- Create: `src/logic/account_map.py`
- Test: `tests/test_account_map.py`

**Interfaces:**
- Produces:
  - `ACCOUNT_MAP_TAB = "Account Map"`, `ACCOUNT_MAP_HEADERS: list[str]` (the 6 headers in order).
  - `map_key(text) -> str`
  - `normalize_qbo_id(value) -> str` (strip; drop a trailing `.0`; `""` for blank/NaN).
  - `@dataclass MapRow: sheet_name: str; qbo_account: str = ""; qbo_id: str = ""; type: str = "Manual"; realm_id: str = ""; updated_at: str = ""; row_num: int | None = None` with property `is_manual -> bool` (`type.strip().lower() != "auto"`).
  - `@dataclass(frozen=True) Resolution: account_id: str | None; qbo_name: str | None; how: str; detail: str = ""`. `how` is one of `manual`, `exact`, `suffix`, `saved`, `blank`, `ambiguous`, `not_found`, `manual_broken`. `ok` property = `account_id is not None`.
  - `class AccountResolver(accounts: dict[str, str], client_name: str = "", realm_id: str = "", rows: list[MapRow] | None = None, now: Callable[[], str] | None = None)` with:
    - `resolve(name) -> Resolution` (memoized per map key)
    - `account_id(name) -> str | None`
    - `remember(name, account_id) -> None` (records an Auto row; used by the seeding script)
    - `pending_rows() -> list[MapRow]` (rows to write: new or changed; Manual rows carry only the new `qbo_id`/`realm_id`/`updated_at`)
  - `resolve_account(qbo_mappings: dict, name, client_name: str = "") -> Resolution`: uses `qbo_mappings["account_resolver"]` if present, else builds `AccountResolver(qbo_mappings.get("accounts", {}), client_name)`, stores it under that key, and uses it.

Resolution order (spec 5.2): clean text (`resolve_account_alias`), Manual row (its `QBO Account` text by name rules first, then its saved Id), exact/suffix name match, Auto row Id, else not found. Name rules = exact full-name match (case-insensitive), then every trailing path of each QBO full name, as `find_id_in_map` does today. More than 1 hit = ambiguous; an Auto row whose Id is among the hits wins. Details:
- `exact`/`suffix` success records/updates the Auto row when the row is missing, its Id differs, its `QBO Account` differs from the current full name, or its `Realm ID` differs.
- `saved` (Auto) success updates the row's `QBO Account` to the current full name if it changed, and logs `[Account Map] '<name>' is not in QBO any more; using saved account <Id> now named '<QBO name>'`.
- A Manual row whose text resolves to an Id different from its `QBO ID` (or whose `QBO ID`/`Realm ID` is blank) records an update of `qbo_id`, `realm_id`, `updated_at`.
- Rows with another company's `Realm ID` are ignored (log once per resolver). A `QBO ID` with a blank `Realm ID` counts as no `QBO ID`.
- Duplicate Sheet Names: Manual beats Auto; same Type, the later row (higher `row_num`, else later in the list) wins, log a warning.
- `detail` text:
  - `not_found`: `Closest in QBO: 'a', 'b'. Fix the name or add it to the Account Map tab` (without the `Closest...` sentence when there is none).
  - `ambiguous`: `Matches more than one QBO account: 'a', 'b'. Add it to the Account Map tab`.
  - `manual_broken`: `Account Map row for '<name>' points to '<QBO Account>', which is not in QBO`.
  - `blank`: `Missing account name`.

- [ ] **Step 1: Write the failing tests** in `tests/test_account_map.py`, class `AccountResolverTests`, using accounts
  `{"Leading Card:Leading Card ACCT": "148", "Leading Card:Leading Card ACCT:Leading Card ACC - 3214": "28", "Cash On Hand:CBD Z 1": "210", "Professional Fees:Recruitment Fees": "155", "Retained Earning": "900", "Retained Earnings": "901", "Amount Owing by/(to) - KZO group": "910", "Amount Owing by/(to) KZO group": "911", "Marketing:Growth": "701", "Sales:Growth": "702"}`, realm `"R1"`, `now=lambda: "2026-11-18 10:00"`:
  - `test_exact_name_resolves_and_is_remembered`: `resolve("Leading Card:Leading Card ACCT").account_id == "148"`, `how == "exact"`; `pending_rows()` has 1 Auto row `("Leading Card:Leading Card ACCT", "Leading Card:Leading Card ACCT", "148", "Auto", "R1", "2026-11-18 10:00")`.
  - `test_suffix_name_resolves`: `"Leading Card ACC - 3214"` -> `"28"`, `how == "suffix"`.
  - `test_renamed_account_uses_the_auto_row`: rows `[MapRow("Leading Card ACCT - 3214", "old name", "28", "Auto", "R1")]` -> `"28"`, `how == "saved"`; pending row `qbo_account == "Leading Card:Leading Card ACCT:Leading Card ACC - 3214"`.
  - `test_exact_name_beats_the_auto_row`: rows `[MapRow("Leading Card ACCT", "x", "28", "Auto", "R1")]` -> `"148"`, `how == "suffix"`, pending Auto row Id `"148"`.
  - `test_manual_row_beats_the_exact_name`: rows `[MapRow("Leading Card ACCT", "Leading Card ACC - 3214", "", "Manual", "")]` -> `"28"`, `how == "manual"`; pending row is Manual with `qbo_id == "28"`, `realm_id == "R1"`, and `qbo_account == "Leading Card ACC - 3214"` unchanged.
  - `test_manual_text_beats_its_saved_id`: rows `[MapRow("Foo", "Cash On Hand:CBD Z 1", "28", "Manual", "R1")]` -> `"210"`.
  - `test_manual_saved_id_used_when_text_is_gone`: rows `[MapRow("Foo", "Gone Account", "210", "Manual", "R1")]` -> `"210"`, `how == "manual"`, no pending row.
  - `test_broken_manual_row_is_an_error`: rows `[MapRow("Foo", "Gone Account", "999", "Manual", "R1")]` -> `account_id is None`, `how == "manual_broken"`, `detail == "Account Map row for 'Foo' points to 'Gone Account', which is not in QBO"`.
  - `test_blank_type_counts_as_manual`: `MapRow("Foo", "Cash On Hand:CBD Z 1", type="")` -> `"210"`, `how == "manual"`.
  - `test_other_company_rows_are_ignored`: rows `[MapRow("CBD Z Card 1", "x", "210", "Auto", "R2")]` -> `how == "not_found"`.
  - `test_id_without_realm_is_ignored`: rows `[MapRow("CBD Z Card 1", "x", "210", "Auto", "")]` -> `how == "not_found"`.
  - `test_inactive_saved_id_is_not_found`: rows `[MapRow("Old Card", "x", "555", "Auto", "R1")]` -> `how == "not_found"`.
  - `test_ambiguous_without_auto_row_is_an_error`: `"Growth"` (client `"KZO"`) -> `how == "ambiguous"`, `detail` contains `"Marketing:Growth"` and `"Sales:Growth"`.
  - `test_ambiguous_with_auto_row_uses_it`: rows `[MapRow("Growth", "Sales:Growth", "702", "Auto", "R1")]` -> `"702"`.
  - `test_not_found_lists_closest_names`: `"Recruitment Fee"` -> `how == "not_found"`, `detail == "Closest in QBO: 'Professional Fees:Recruitment Fees'. Fix the name or add it to the Account Map tab"`.
  - `test_near_identical_siblings_resolve_only_to_themselves`: `"Retained Earning"` -> `"900"`, `"Retained Earnings"` -> `"901"`, `"Amount Owing by/(to) - KZO group"` -> `"910"`, `"Amount Owing by/(to) KZO group"` -> `"911"`.
  - `test_case_and_spaces_hit_the_map_row`: rows `[MapRow("Leading Card ACCT - 3214", "old", "28", "Auto", "R1")]`, `resolve("  leading card  ACCT - 3214 ")` -> `"28"`.
  - `test_typed_id_with_decimal_is_normalized`: `normalize_qbo_id("210.0") == "210"`, `normalize_qbo_id(" 210 ") == "210"`; rows `[MapRow("Foo", "Gone", "210.0", "Manual", "R1")]` -> `"210"`.
  - `test_duplicate_rows_manual_wins`: rows `[MapRow("Foo", "x", "210", "Auto", "R1", row_num=2), MapRow("Foo", "Professional Fees:Recruitment Fees", "", "Manual", "", row_num=3)]` -> `"155"`.
  - `test_kzp_alias_still_applies`: client `"KZP"`, accounts `{"Marketing Income:Growth": "5010", "Marketing Expense:Growth": "6010"}` -> `"Growth"` -> `"5010"`, `"Growth - Marketing Expense"` -> `"6010"`.
  - `test_blank_name`: `resolve("")` and `resolve(float("nan"))` -> `how == "blank"`.
  - `test_resolve_account_builds_and_caches_a_resolver`: `m = {"accounts": {"Cash On Hand:CBD Z 1": "210"}}`; `resolve_account(m, "CBD Z 1").account_id == "210"` and `"account_resolver" in m`.
  - `test_remember_records_an_auto_row`: `remember("S5A", "210")` -> `pending_rows()[0]` is Auto, Id `"210"`, `qbo_account == "Cash On Hand:CBD Z 1"`.

- [ ] **Step 2: Run** `PYTHONIOENCODING=utf-8 .venv/python.exe -m unittest tests.test_account_map` — expect ImportError / failures.
- [ ] **Step 3: Implement** `src/logic/account_map.py` per the Interfaces block. Log through `setup_logger("account_map")`.
- [ ] **Step 4: Run** the same command — all pass.
- [ ] **Step 5: Commit** `feat: account resolver with Account Map rows`.

### Task 2: Account Map tab read and write

**Files:**
- Modify: `src/logic/account_map.py`, `src/connectors/gsheets_client.py` (new method)
- Test: `tests/test_account_map.py` (class `AccountMapTabTests`)

**Interfaces:**
- Consumes: `MapRow`, `ACCOUNT_MAP_TAB`, `ACCOUNT_MAP_HEADERS`, `map_key`, `AccountResolver.pending_rows()` from Task 1.
- Produces:
  - `GSheetsClient.write_values_raw(spreadsheet_url_or_id: str, tab_name: str, updates: list[tuple[str, list[list[str]]]], create_if_missing: bool = False) -> None` — one `ws.batch_update([{"range": a1, "values": vals}, ...], value_input_option="RAW")`; when the tab is missing and `create_if_missing`, `add_worksheet(title=tab_name, rows=1000, cols=len(ACCOUNT_MAP_HEADERS))` first. Decorated with `@retry_with_backoff()`. No call when `updates` is empty.
  - `parse_account_map(values: list[list]) -> list[MapRow]`: header row 1, columns found by header name (trimmed, case-insensitive); unknown columns ignored; rows with blank Sheet Name skipped; `row_num` = sheet row number.
  - `read_account_map(gs, spreadsheet_id: str) -> list[MapRow]`: `gs.read_tab_values(spreadsheet_id, ACCOUNT_MAP_TAB)`; `None` -> `[]`; exceptions propagate.
  - `write_account_map_changes(gs, spreadsheet_id: str, changes: list[MapRow]) -> int`: if `changes` is empty return 0 with no read. Else re-read the tab (`read_tab_values`), match each change by `map_key(sheet_name)` against the fresh rows. Existing row: for an Auto change write all 6 columns of that row; for a Manual change write only `QBO ID`, `Realm ID`, `Updated At`; if the fresh row is Manual and the change is Auto, skip it (team took the row over). New rows go below the last non-empty row. Missing tab: write header + rows from row 1 (`create_if_missing=True`). Header present but missing some of the 6 columns: add the missing headers to the right of the last header cell in the same batch. One `write_values_raw` call. Returns number of rows written.

- [ ] **Step 1: Write the failing tests** with a fake `gs` that records `read_tab_values` results and `write_values_raw` calls:
  - `test_parse_reads_columns_by_header_name`: values `[["Note", "QBO ID", "Sheet Name", "Type", "QBO Account", "Realm ID", "Updated At"], ["hi", "28", "Leading Card ACCT - 3214", "Auto", "x", "R1", ""]]` -> 1 row, `qbo_id == "28"`, `row_num == 2`.
  - `test_parse_skips_blank_sheet_names`.
  - `test_missing_tab_reads_as_empty`: `read_tab_values` returns `None` -> `[]`.
  - `test_read_error_propagates`: `read_tab_values` raises `RuntimeError` -> `read_account_map` raises.
  - `test_no_changes_means_no_read_and_no_write`.
  - `test_new_rows_go_below_the_last_row_in_one_call`: existing 3 data rows; 2 new Auto changes -> one `write_values_raw` call whose ranges start at row 5.
  - `test_rows_are_matched_by_name_after_a_sort`: change for `"B"` read when B was row 3; fresh read has B at row 2 -> write range on row 2.
  - `test_manual_change_writes_only_id_realm_and_date`: fresh row Manual; ranges cover only the `QBO ID`, `Realm ID`, `Updated At` cells of that row.
  - `test_auto_change_skips_a_row_the_team_made_manual`.
  - `test_missing_tab_is_created_with_headers`: `read_tab_values` -> `None`; call has `create_if_missing=True`, first range `A1` with `ACCOUNT_MAP_HEADERS`.
- [ ] **Step 2: Run** `PYTHONIOENCODING=utf-8 .venv/python.exe -m unittest tests.test_account_map` — new tests fail.
- [ ] **Step 3: Implement** the 3 functions and `GSheetsClient.write_values_raw`.
- [ ] **Step 4: Run** — all pass.
- [ ] **Step 5: Commit** `feat: read and write the Account Map tab`.

### Task 3: Transform and month-end use the resolver

**Files:**
- Modify: `src/logic/transformer.py` (remove `replacements` from `find_id_in_map`; account checks at the journal, expense and transfer validators), `src/logic/month_end.py:222`, `run_ingestion.py` (`process_client_control_sheet`)
- Test: `tests/test_account_map.py` (class `TransformIntegrationTests`), update `tests/test_sync_transfer_guard.py`, `tests/test_account_matching.py`

**Interfaces:**
- Consumes: `resolve_account`, `AccountResolver`, `read_account_map`, `write_account_map_changes`.
- Produces: `qbo_mappings["account_resolver"]` set by `process_client_control_sheet` for the whole client run.

Changes:
- Every account check in `process_journals` / `process_expenses` / `process_transfers` calls `resolve_account(qbo_mappings, name, client_name)`. On failure: `ERROR | Account not found: '<name>'. <detail> | Row No: <n>` (journals), `ERROR | Source Account not in QBO: '<name>'. <detail> | Row No: <n>` and the same pattern for `Expense Account`, `'From' Account`, `'To' Account`. The `Available: ...` text is dropped. Currency checks and the same-Id transfer check use the resolved Id as today; update the transfer comment (no more replacements).
- `find_id_in_map` keeps its signature and fuzzy for non-account maps; its `replacements` block is deleted.
- `month_end._line_error` uses `resolve_account(...)`; its message stays `Account not found: '<account>'`.
- `process_client_control_sheet`: right after the mappings are fetched, `read_account_map(gs, control_sheet_id)`; on exception log `❌ [<client>] Cannot read the Account Map tab: <e>` and return before any control row is touched. Set `qbo_mappings["account_resolver"] = AccountResolver(qbo_mappings["accounts"], client_name, realm_id, rows)`. Wrap the control-row loop in `try/finally`; in `finally`, `write_account_map_changes(gs, control_sheet_id, resolver.pending_rows())`, catching and logging any exception (a failed save never fails rows). Log `[Account Map] saved <n> row(s)` when n > 0.

- [ ] **Step 1: Write / update the failing tests**:
  - `TransformIntegrationTests.test_renamed_account_passes_with_an_auto_row`: `process_expenses` (or `transform_raw`) for client `"KZO"` with `qbo_mappings={"accounts": {"Leading Card:Leading Card ACCT:Leading Card ACC - 3214": "28", "Expense:AI": "5"}, "locations": {}, "account_resolver": AccountResolver({...same...}, "KZO", "R1", [MapRow("Leading Card ACCT - 3214", "x", "28", "Auto", "R1")])}` and a row whose `Account (Cr)` is `"Leading Card ACCT - 3214"` -> Remarks `"Ready to sync"`.
  - `test_unknown_account_error_names_the_closest_and_the_tab`: same without the map row -> Remarks starts with `"ERROR | Source Account not in QBO: 'Leading Card ACCT - 3214'. Closest in QBO: "` and contains `"add it to the Account Map tab"`.
  - `test_month_end_line_uses_the_auto_row`: `month_end._line_error` with the resolver above and account `"Leading Card ACCT - 3214"` -> `None` for the account part (the line is valid).
  - `tests/test_sync_transfer_guard.py`: replace the `"CBD Z Card"` -> `"KZO CBD Z"` cases with a leaf vs full name pair: accounts `{"Bank:KZO CBD Z": "77", "Bank:KZO Bank TH": "88"}`, From `"KZO CBD Z"`, To `"Bank:KZO CBD Z"` -> same-Id ERROR at Transform and ValueError at Sync. Keep the distinct-accounts case.
  - `tests/test_account_matching.py`: `test_hardcoded_replacements_still_apply_without_fuzzy` becomes `test_cbd_z_card_is_no_longer_rewritten`: `find_id_in_map({"Bank:KZO CBD Z": "900"}, "CBD Z Card", allow_fuzzy=False) is None`.
  - `test_ingestion_saves_the_map_once_per_client` (in `TransformIntegrationTests`, patching `read_account_map`, `write_account_map_changes`, `QBOSync` and a control sheet with no `READY` rows): `write_account_map_changes` called once with the resolver's pending rows.
  - `test_ingestion_stops_when_the_map_cannot_be_read`: `read_account_map` raises -> no `_batch_update_control` call, `write_account_map_changes` not called.
- [ ] **Step 2: Run** `PYTHONIOENCODING=utf-8 .venv/python.exe -m unittest tests.test_account_map tests.test_sync_transfer_guard tests.test_account_matching tests.test_month_end` — new tests fail.
- [ ] **Step 3: Implement** the changes listed above.
- [ ] **Step 4: Run** the same command, then the full suite (`ls tests/test_*.py` modules) — all pass.
- [ ] **Step 5: Commit** `feat: Transform and month-end resolve accounts through the Account Map`.

### Task 4: Sync uses the resolver

**Files:**
- Modify: `src/logic/syncing.py` (`QBOSync`), `run_syncing.py` (`process_client_sync`)
- Test: `tests/test_account_map.py` (class `SyncIntegrationTests`), `tests/test_account_matching.py`

**Interfaces:**
- Consumes: `AccountResolver`, `MapRow`, `read_account_map`.
- Produces: `QBOSync.use_account_map(rows: list[MapRow]) -> None`.

Changes:
- `QBOSync.find_id("accounts", name)` returns `self._account_resolver().resolve(name).account_id`; on failure logs a warning with the Resolution `detail`. `_account_resolver()` builds `AccountResolver(self.mappings["accounts"], client_name, realm_id, rows)` lazily and rebuilds it when `self.mappings["accounts"]` is a different object than the one it was built from (tests replace `sync.mappings` after construction). `client_name` / `realm_id` come from `self.client` via `getattr(..., "")`.
- The `replacements` block in `find_id` is deleted; non-account lookups are unchanged.
- `process_client_sync`: right after `sync_engine = QBOSync(client=qbo_client)`, `rows = read_account_map(gs, control_sheet_id)`; on exception log `❌ [<client>] Cannot read the Account Map tab: <e>` and return before any control row is set to PROCESSING; else `sync_engine.use_account_map(rows)`. Sync never writes the tab.

- [ ] **Step 1: Write the failing tests**:
  - `SyncIntegrationTests.test_sync_resolves_a_renamed_account_through_the_map`: `QBOSync` with patched `_get_qbo_mappings`, `mappings = {"accounts": {"Leading Card:Leading Card ACCT:Leading Card ACC - 3214": "28"}, "accounts_meta": {}}`, client `client_name="KZO"`, `realm_id="R1"`, `use_account_map([MapRow("Leading Card ACCT - 3214", "x", "28", "Auto", "R1")])` -> `find_id("accounts", "Leading Card ACCT - 3214") == "28"`.
  - `test_sync_does_not_write_the_map`: after resolving a new exact name, nothing calls a writer (assert the module-level `write_account_map_changes` is never called from `run_syncing.process_client_sync` with a patched control sheet).
  - `test_sync_stops_when_the_map_cannot_be_read`: patched `read_account_map` raises -> no `_batch_update_control` call with `PROCESSING`.
  - Existing `KzpGrowthAliasSyncTests` and `LeadingCardRenameTests.test_sync_resolves_the_mkt_name` keep passing unchanged.
- [ ] **Step 2: Run** `PYTHONIOENCODING=utf-8 .venv/python.exe -m unittest tests.test_account_map tests.test_account_matching tests.test_sync_transfer_guard` — new tests fail.
- [ ] **Step 3: Implement**.
- [ ] **Step 4: Run** the same command and the full suite — all pass.
- [ ] **Step 5: Commit** `feat: Sync resolves accounts through the Account Map`.

### Task 5: Reconcile compares account Ids

**Files:**
- Modify: `src/logic/reconciler.py`, `run_reconciliation.py` (`process_client_reconcile`), `src/logic/account_map.py` (add `fetch_qbo_accounts`)
- Test: `tests/test_reconciler.py`, `tests/test_account_matching.py`

**Interfaces:**
- Consumes: `AccountResolver`, `read_account_map`.
- Produces:
  - `fetch_qbo_accounts(qbo_client) -> dict[str, str]`: `qbo_client.query("SELECT Id, Name, FullyQualifiedName FROM Account")` -> `{FullyQualifiedName or Name: Id}` (active only, same keys as `QBOSync.mappings["accounts"]`).
  - `Reconciler(qbo_client, account_resolver: AccountResolver | None = None)`. When `None`, the first account check builds `AccountResolver(fetch_qbo_accounts(client), client_name, realm_id, [])`.

Changes:
- Journal line pool entries carry `account_id = AccountRef.value`. Passes 1 and 3 accept when the sheet account resolves and its Id equals `account_id`. Pass 2 (amount only): when the sheet account does not resolve, the status is `❌ Account not found: '<sheet name>'` (with the header prefix as today); otherwise the existing `❌ Account Mismatch (Sheet: '...' vs QBO: '...')`. Pass 4 unchanged.
- Expenses: resolve `Account (Cr)`; not resolved -> `Payment Account not found: '<sheet name>'`; Id differs from `AccountRef.value` -> existing `Payment Account Mismatch: '<sheet>' != '<qbo display>'`.
- Delete `_is_account_match` and its KZP bare-name fallback; drop the `difflib` import if unused.
- `process_client_reconcile`: after auth, `rows = read_account_map(gs, control_sheet_id)` and `accounts = fetch_qbo_accounts(qbo_client)`; on exception log and return before any control row changes; `Reconciler(qbo_client, account_resolver=AccountResolver(accounts, client_name, realm_id, rows))`. Reconcile never writes the tab.

- [ ] **Step 1: Write / update the failing tests**:
  - `tests/test_reconciler.py` `JournalLineMatchingTests`: `_line(...)` adds `AccountRef.value` from a name->Id dict; `_run` passes `account_resolver=AccountResolver(<that dict>, "KZDW", "R1", [])`. All 5 existing tests keep their expected statuses.
  - New `test_sibling_account_is_a_mismatch`: accounts `{"Cash:KZO COY TRC 4 - Reserve Fund": "1", "Cash:KZO COY TRC 4A - Reserve Fund": "2", "Exp:Fee": "3"}`; sheet `("KZO COY TRC 4 - Reserve Fund", -100.0), ("Fee", 100.0)`; QBO lines `Credit 100 on Id 2`, `Debit 100 on Id 3` -> statuses[0] starts with `"❌ Account Mismatch"`.
  - New `test_renamed_account_matches_by_id`: map row `MapRow("Old Card", "x", "28", "Auto", "R1")`, accounts `{"Cards:New Card": "28", "Exp:Fee": "3"}`; sheet `("Old Card", -50.0), ("Fee", 50.0)`; QBO lines on Ids 28 and 3 -> both `"✅ Matched"`.
  - New `test_unknown_sheet_account_is_reported`: sheet account `"Nope"` -> status `"❌ Account not found: 'Nope'"`.
  - New expense tests (`ExpensePaymentAccountTests`): same Id -> `"✅ Matched"`; different Id -> contains `"Payment Account Mismatch"`; unresolved -> contains `"Payment Account not found: 'Nope'"`.
  - `tests/test_account_matching.py`: `KzpGrowthAliasReconcileTests` and `LeadingCardRenameTests.test_reconcile_matches_the_mkt_name` rewritten to Id comparisons through a resolver (sheet `"Growth"` vs line Id of `Marketing Income:Growth` -> match; vs `Marketing Expense:Growth` -> mismatch).
  - `test_reconcile_stops_when_the_map_cannot_be_read` (patched `read_account_map` raises -> `Reconciler` never built, no control update).
- [ ] **Step 2: Run** `PYTHONIOENCODING=utf-8 .venv/python.exe -m unittest tests.test_reconciler tests.test_account_matching tests.test_account_map` — new tests fail.
- [ ] **Step 3: Implement**.
- [ ] **Step 4: Run** the same command and the full suite — all pass.
- [ ] **Step 5: Commit** `feat: Reconcile compares QBO account Ids`.

### Task 6: One-off seeding script

**Files:**
- Create: `seed_account_map.py` (repo root, next to `setup_qbo_company.py`)
- Test: `tests/test_account_map.py` (class `SeedLearnTests`)

**Interfaces:**
- Consumes: `AccountResolver.remember`, `AccountResolver.pending_rows`, `read_account_map`, `write_account_map_changes`, `fetch_qbo_accounts`.
- Produces:
  - `learn_account_id(kind: str, column: str, txn: dict, sheet_amount: float | None) -> str | None` (pure). `kind` in `Journals`, `Expenses`, `Transfers`. Rules (spec 5.6): `Account (Cr)` -> `txn["AccountRef"]["value"]`; `Expense Account (Dr)` -> the only `AccountBasedExpenseLineDetail` line's account, else `None`; `Transfer Funds From` / `To` -> `FromAccountRef` / `ToAccountRef`; Journals `Account` -> the only line with `Amount == abs(sheet_amount)` (to 0.01) and `PostingType` `Debit` when `sheet_amount > 0` else `Credit`, else `None`.
  - CLI: `venv/bin/python seed_account_map.py [--client NAME] [--write]`. For each active, allowed client: auth, `fetch_qbo_accounts`, `read_account_map`, `AccountResolver(...)`; walk every control row with a Transform File and Month (all months; month-end rows use `MONTH_END_COUNTRY`), tabs `<country> <Mon YY> - Journals|Expenses|Transfers`, columns as in the audit script; sleep 1.2 s before each Sheets read. Resolve every name (exact/suffix records Auto rows). For names that do not resolve and have synced rows, read up to 3 synced rows (`QBO ID`) through `QBOClient._get` (`journalentry`, `purchase`, `transfer`) and apply `learn_account_id`; all learned Ids equal and active -> `remember(name, id)`; else print `NOT SEEDED: '<name>' -> <ids>`. Print every planned row; write with `write_account_map_changes` only when `--write` is given.
- [ ] **Step 1: Write the failing tests** for `learn_account_id`: one test per rule, plus `test_two_expense_lines_learn_nothing` and `test_two_journal_lines_with_the_same_amount_learn_nothing`.
- [ ] **Step 2: Run** `PYTHONIOENCODING=utf-8 .venv/python.exe -m unittest tests.test_account_map` — fail.
- [ ] **Step 3: Implement** `seed_account_map.py`.
- [ ] **Step 4: Run** — pass.
- [ ] **Step 5: Commit** `feat: one-off script to fill the Account Map tab`.

### Task 7: Docs, full check, review, deploy, seed

**Files:**
- Modify: `README.md` (new section `### Account Map tab`; update `### Account matching: no fuzzy guessing for accounts`, the replacements paragraph added on 2026-11-18, the "three matchers are intentional duplicates" paragraph, and the `CBD Z Card` example in `### Transfer: From and To must be different QBO accounts`), `DOCUMENTATION.md:340` (replacements hint -> Account Map).

- [ ] **Step 1:** Write the README section: what the tab is for, the 6 columns, the 5 resolution steps, how to add a Manual row (type `Sheet Name` and `QBO Account`, leave the rest blank), the ERROR texts and what to do for each, the Realm ID rule, the Reconcile limits from spec 5.5, and that the tab is filled once by `seed_account_map.py`.
- [ ] **Step 2:** Run the full suite: `mods=$(ls tests/test_*.py | sed 's#/#.#; s#\.py$##' | tr '\n' ' '); PYTHONIOENCODING=utf-8 .venv/python.exe -m unittest $mods` — expect `OK`.
- [ ] **Step 3:** Fresh whole-branch review (subagent, `git diff main...account-map`); fix findings; re-run the suite.
- [ ] **Step 4:** Commit docs; fast-forward `main` to `account-map`; `git push origin main`.
- [ ] **Step 5:** Deploy per the QBO deploy procedure (check no job is running, `git pull --ff-only`; no restart needed unless `server.py` imports changed).
- [ ] **Step 6:** On the server: `venv/bin/python seed_account_map.py` (dry run). Check: the 5 names in spec section 2 map to `1150040028`, `210`, `1150040096`, `155`, `1150040109`; `Leading Card ACC` is listed as NOT SEEDED. Then run with `--write`.
- [ ] **Step 7:** Verify: read each company's `Account Map` tab row count; run the audit resolution check (all 320 audit names + the 5 renamed names resolve except `Leading Card ACC`).
