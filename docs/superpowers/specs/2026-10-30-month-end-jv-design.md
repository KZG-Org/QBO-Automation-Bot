# Month-end JV: post manual month-end journals through the bot

Status: approved by the user on 2026-10-30 ("if everything is good please deploy").

## 1. Why

Month-end journals (for example the group cost reclass `KZO-JV1371`, 30 Sep 2026) sit in a sheet
outside the normal COY flow. Today Mae imports them into QBO with SaaSant. The team wants to cancel
SaaSant and run these journals through the bot with the same buttons as every other row:
Transform, then SYNC NOW.

Volume: KZO has hundreds of lines a month. Each other company has fewer than 20.

## 2. Goals

1. One new control row per company per month: the **month-end row**. It uses the normal
   `Transform` (G), `QBO Sync` (H) and `QBO Reconcile` (I) dropdowns.
2. Transform reads a source tab of journal lines, groups them into journals, checks them, gives
   each journal a Journal No, and writes them to the row's own Transform file.
3. SYNC NOW posts them with the existing journal sync.
4. The bot writes a per-line status back to the source tab.
5. Works for KZO, KZP, KZDW, S5, UMBER and TINDERPAY.

## 3. Non-goals

- Expenses, transfers, bills, invoices. Journals only.
- Fixing a journal that is already in QBO. That stays a manual QBO edit, as for normal rows.
- The KZO bulk menu. The month-end row is run only from its own dropdowns (user decision).
- Rounding or auto-fixing amounts. Normal rows round to 2 decimals and fix up to 0.50; month-end
  amounts are posted exactly as typed (user decision, see 6.2).

## 4. Anti-goals (a failure even if it "works")

- A duplicate journal in QBO.
- Any change in how normal rows transform, sync or reconcile, for any company.
- The bot changing any source cell other than `Status`.
- A journal posted half done: if any month-end journal fails a check, SYNC NOW stays blocked.

## 5. What the user sees

### 5.1 Source tab

Lives in any spreadsheet. For KZO it is a tab in the TH COY file (for easier management), but it
holds entries for all countries. Example: `2026 TH COY` > tab `Month-end JV_test`.

Every company uses the same layout (user decision): header on row 1, columns A-I in this order.
The bot finds columns by header name, not letter.

| Col | Header | Value required | Meaning |
|---|---|---|---|
| A | `Date` | yes | Journal date. Sheets date or `dd/mm/yyyy`. |
| B | `Memo` | yes | Line description and journal memo. Part of the grouping key. |
| C | `Account` | yes | QBO account name. Same matching as normal rows (exact + suffix path, no fuzzy). |
| D | `Amount` | yes | Positive = debit, negative = credit. Same as the Transform `Journals` tab today. |
| E | `Name` | no | QBO vendor name. Ignored when not found (same as sync today). |
| F | `Location` | no | QBO location. Ignored for S5 and UMBER (same as normal rows). |
| G | `Currency Code` | no | Blank = USD. |
| H | `Class` | no | QBO class. Ignored for S5 and UMBER (same as normal rows). |
| I | `Status` | bot only | Written by the bot. The team never types here. |

Header rules:
- A missing `Date`, `Memo`, `Account` or `Amount` header stops Transform: `Transform` = `ERROR`,
  note names the missing header. Nothing is written.
- A missing `Name`, `Location`, `Currency Code` or `Class` header is treated as all blank (my call).
- **A missing `Status` header is added by the bot** (user decision): it writes `Status` in row 1 of
  the first column after the last non-empty header. Month-end rows only; the bot never adds
  columns to a normal COY tab.

The team does **not** fill `No` or `Journal No`.

Example from the test tab: rows 2-10 are one KZO reclass (AI Expenses, debits TH, PK, PH, PE, MX,
CO, BR, BD = 17,167.88; credit GROUP = -17,167.88). Rows 11-12 show the shape other companies use
(`AI Expenses` 500, `Account B` -500, memo `Month-End`).

### 5.2 Control row (all 6 control sheets)

| Column | Value |
|---|---|
| A `Country` | `Month-end JV`. Changed 2026-10-31 (user): a row is month-end when Country **or** Tab Name contains "Month-end" in any spelling (Monthend, Month end). File and tab names always use `Month-end JV`, never the Country. |
| B `Source File` | Link to the spreadsheet holding the source tab. |
| C `Tab Name` | The source tab name. Any name. |
| D `Month` | First day of the month, e.g. `2026-10-01`. |
| E `Last Month Date` | Optional, e.g. `2026-10-31`. Same meaning as normal rows. |
| F `Transform File` | Leave blank. The bot creates `KZO - Month-end JV QBO - Oct 26` (existing step 3). Each company gets its own file (user decision). |
| T `No Column Check` (KZO) | Leave blank. If it shows TRUE, the KZO script blocks Transform. |

Several rows may share one month (one normal row per country plus the month-end row). Only one
month-end row per company per month: if a second one is READY, it gets `Transform` = `ERROR` and
the note `Another Month-end JV row exists for Oct 26 (row N)`. Two rows would each rebuild the
other's journals into their own Transform file.

### 5.3 Transform file

One tab: `Month-end JV Oct 26 - Journals` (the existing `{Country} {Mon yy} - Journals` pattern).
Template (user decision):
- Use the control sheet's `Sample - Journals_monthend` tab when it exists. The team puts header
  notes on it for month-end rows. The tab is copied whole (`copy_to`), so Sheets **notes** come
  with it. Threaded **comments** do not copy, so the team must use Insert > Note.
- If that tab does not exist, use `Sample - Journals` (no notes).
- Both must keep the same headers as `Sample - Journals` (checked in the KZO control sheet on
  2026-10-30: 15 headers, `No` through `Raw Reconcile`).

`append_or_create_df` appends rows **by position** after copying the template
(`gsheets_client.py:262-300`), so the output column order must match the template's first columns
exactly. Columns are the client's existing journal columns:

- KZDW / TINDERPAY: `No, Journal No, Date, Memo, Account, Amount, Name, Location, Currency Code, Currency Exchange, Class, Remarks`
- Others: `No, Journal No, Date, Memo, Account, Amount, Name, Location, Currency Code, Class, Remarks`

`No` is blank on every line (user decision). `Remarks` = `Ready to sync`. Failed journals are not
copied (they show in the source `Status` instead).

### 5.4 Status values

Source `Status`, per line:

| When | Value |
|---|---|
| Copied to the Transform tab | `Transformed KZO-JV1372` |
| Posted, or found already in QBO | `Synced KZO-JV1372` |
| Its journal failed a check | `ERROR: <reason>`, e.g. `ERROR: Account not found 'Account B'`, `ERROR: Journal does not balance (diff 12.50)` |

The JV number in the status is the key that links source lines to Transform lines (no `No`).

Control row, same values as normal rows:

| Cell | After Transform | After Sync |
|---|---|---|
| G `Transform` | `DONE` or `ERROR` | |
| H `QBO Sync` | | `DONE` or `PARTIAL ERROR` |
| M `QBO Journal` | `READY TO SYNC` or `ERROR` | `SYNCED` or `SYNC FAIL` |
| N `QBO Expense`, O `QBO Transfer` | blank | blank |
| P `Last Journal No` | updated | |
| `Transform Note` | short summary: journals copied, journals failed, rows left for another month | |
| L `Last Processed Row`, `Pending Amount Nos` | not written | |

`QBO Journal` = `ERROR` when any line in the source tab for this month has an `ERROR` status. The
existing Apps Script rule then blocks SYNC NOW until it is fixed.

## 6. How it works

### 6.1 Grouping (user decision)

| Company | Lines in one journal share |
|---|---|
| KZO and KZO Sports (client name contains `kzo`, same test as `_is_kzo_case`; my call) | Date + Account + Memo |
| KZP, KZDW, S5, UMBER, TINDERPAY | Date + Memo |

KZO month-end entries use one account per journal (user). A KZO entry that uses 2 accounts splits
into 2 journals that cannot balance, so both get `ERROR` and SYNC NOW stays blocked until fixed.

This key groups **new** lines only (Status blank or `ERROR`). Lines rebuilt under an existing
number (6.3) are grouped by that number and never mixed with new lines.

Memo is in the KZO key on my call: without it, the test tab's row 11 (`AI Expenses`, same date)
would join rows 2-10 and the journal would not balance.

Text compare for the key: trim, collapse inner spaces, ignore case (my call).

### 6.2 Checks per journal

All lines of a journal pass or the whole journal fails. Failed journals get `ERROR: <reason>` on
each of their lines and are not copied. Other journals still go through.

1. Each line has Date, Account and a non-zero Amount. A fully blank row is ignored.
2. Account found in QBO (`find_id_in_map(..., allow_fuzzy=False, client_name=...)`, same as normal
   journals, including the KZP `Growth` alias).
3. Location found in QBO when filled (not for S5 / UMBER).
4. Class found in QBO when filled (not for S5 / UMBER). Needed because sync drops an unknown
   Class without an error (`find_id('classes', ..., warn_on_missing=False)`, `syncing.py:348`),
   and month-end Class is typed by hand, not copied from Location as on normal rows.
5. No rounding (user decision). The bot never changes an amount.
   - Each Amount has at most 2 decimals (QBO keeps cents only), else
     `ERROR: Amount 11702.5197 has more than 2 decimals (row 2). Use ROUND(...,2) in the sheet`.
     Compare with a float tolerance (`abs(x*100 - round(x*100)) < 1e-6`), so 11702.52 read as a
     float passes.
   - Sum of Amount = 0 to the cent (`abs(sum) < 0.005`), else
     `ERROR: Journal does not balance (diff 0.01)`.
6. Currency (my call, keeps normal-row behavior):
   - KZO, KZP, S5, UMBER: `Currency Code` must be blank or `USD`, else
     `ERROR: <company> journals post in USD only`. Normal rows for these always post USD.
   - KZDW / TINDERPAY: one currency per journal, else `ERROR: Mixed currencies in one journal`
     (sync takes the currency from the first line only). No account-currency check: normal KZDW
     journals skip it too (`transformer.py:941`, `_should_check_currency_transfer_only` is True
     for the KZDW family); QBO rejects a bad mix at sync. `Currency Exchange` is `1` for USD and
     blank otherwise; sync then takes QBO's rate for the journal date (`syncing.py:132`) and
     fails the journal if QBO has none. No rate column in the source tab.
7. Class (my call, matches normal rows, `transformer.py:960`): when `Class` is blank, use
   `Location`. S5 / UMBER: Location and Class are both blank, as on normal rows.

### 6.3 Which source lines Transform takes

Guard first (user: each company has its own source tab): if any `Status` in the tab names a JV
whose prefix is not this company's (`KZP-JV0012` in a KZO run), stop before anything is written:
`Transform` = `ERROR`, note `Tab '<name>' has KZP-JV journals; each company needs its own
month-end tab`.

- Date inside the row's month range (`get_month_date_range(Month, Last Month Date)`).
  Lines outside it are left untouched and counted in `Transform Note`.
- `Status` blank or starting with `ERROR` → new journal, new Journal No.
- `Status` = `Transformed <JV>` and `<JV>` is **not** in the Transform tab → rebuild under the same
  `<JV>` (see 6.5). Only lines dated inside the row's month are rebuilt.
- `Transformed <JV>` with `<JV>` in the tab, and `Synced <JV>` → skipped.

### 6.4 Journal No

- Prefix: `_build_id_prefixes(client_name)[0]` (`KZO-JV`, `KZP-JV`, `KZDW-JV`, `S5-JV`, `TDP-JV`, ...).
  Same counter as normal rows, so numbers stay in one sequence (user decision: same as today).
- Start: `max(global Last Journal No in control sheet, QBO max for prefix)`, as step 4 does today.
- Numbers go only to new journals that pass every check, in source order (first line's row).
  Failed journals get no number, so a fix-and-rerun leaves no gap.
- **Reserve before writing:** write the highest number used to the row's `Last Journal No`
  right after numbering, before any Status or Transform write (6.5 step 4b). A crash after that
  can leave a gap, never a reused number.
- **Fix in this work:** `global_last_jv` is read once per run (`run_ingestion.py:641`) and never
  raised after a row. Two READY rows of one company in the same run can mint the same numbers,
  and sync then skips the second as "already synced". After each row, raise `global_last_jv` to
  that row's `last_journal_no`. This also fixes the existing case of 2 KZO countries READY in
  one run.

### 6.5 Transform order (prevents duplicates)

0. (Added after the final review.) Reads that decide what is written are strict: a failed read
   of the Transform tab stops the row instead of counting as "no tab". A source with no `Status`
   header while the Transform tab already has journals stops the row. After step 4b nothing
   raises: any later failure stops the row and still returns the reserved number.
1. Read the Transform tab. Delete every line of any journal whose `Remarks` contains `ERROR`
   (sync failure or a user-flagged rebuild). This is the same "ERROR in Remarks → rebuild under
   the same number" rule normal rows use.
2. Read the source tab (header row 1, unformatted values).
3. Select lines (6.3), group (6.1), check (6.2), assign numbers (6.4).
4. Read the source tab again. If any selected row's Date, Account or Amount changed or moved,
   stop: write nothing, `Transform` = `ERROR`, note `Source tab changed during Transform, run again`.
4b. Write `Last Journal No` to the control row and raise `global_last_jv` (6.4).
5. Add the `Status` header if missing (5.1), then write all `Status` cells in one batch update.
6. Append the good journals to the Transform tab.
7. Update the control row.

If step 6 fails, step 3 of the next run finds `Transformed <JV>` lines whose JV is missing from the
tab and rebuilds them under the same number. If step 5 fails (for example no Editor access),
nothing was copied: `Transform` = `ERROR`, note names the file and says the bot needs Editor access.

### 6.6 Sync

`run_syncing.py` already reads `{Country} {Mon yy} - Journals`, so posting needs no change. Expense
and Transfer tabs do not exist and are skipped as today.

New for month-end rows only:

- **Check before trusting "already in QBO".** Today, when a Journal No already exists in QBO, sync
  marks every line `Skipper (Already synced ...)` and reports `SYNCED` without comparing anything
  (`run_syncing.py:299-303`). If two different journals ever share a number, the second one is
  never posted and nobody sees an error. For month-end rows, before skipping, compare the QBO
  journal's date and memo (`PrivateNote` = first line's Memo) with the sheet journal. Same → skip
  as today. Different → line `Remarks` = `ERROR: <JV> is already used in QBO by a different
  journal`, `QBO Journal` = `SYNC FAIL`; the source Status is not touched. Normal rows keep
  today's behavior. (Changed after the final review: the first build compared totals and wrote
  ERROR to the source, which could renumber and re-post an edited journal that was already in
  QBO, and relied on JournalEntry `TotalAmt`, which this repo never verified.)
- **Write back to source.** After the journal section, for each JV that ended as `Synced at ...`
  or a confirmed `Skipper (Already synced ...)`, set source lines with `Status` =
  `Transformed <JV>` to `Synced <JV>` in one batch. A failure here is logged and noted, not fatal; it does not change
`QBO Sync`. Sync never writes `ERROR` to the source (that would make Transform copy the journal
again).

To retry a journal that failed at sync: fix the cause (in source or QBO), then set Transform to
READY. Step 6.5.1 deletes it and rebuilds it under the same number. Then SYNC NOW.

### 6.7 Reconcile

For month-end rows, skip the raw read and the raw-vs-transform check (there is no raw `No`). Run the
existing QBO journal reconcile on the tab; it groups by `Journal No` and does not need `No`.

### 6.8 Apps Script (KZO only)

In `appscript/kzo/Transform Button.js`, `getDialogData_` and `getTargetsForExecution` skip rows
whose Country is `Month-end JV`. The other 5 projects have no menu and need no change. Pull with
`clasp pull` before editing; `clasp push` replaces the live project, so confirm with the user
before pushing.

### 6.9 Start date and first test (my call)

- Month-end rows start with **Oct 2026**. Sep 2026 and earlier were posted by SaaSant. Lines are
  only taken for the row's own month, so old lines in the tab are never picked up as long as no
  month-end row exists for an old month. README states: do not create a month-end row for a
  month SaaSant already posted, and stop SaaSant imports from Oct 2026.
- There is no test QBO company and no dry-run mode. First test: Transform only on the
  `Month-end JV_test` row, check the Transform tab and the source Status, then SYNC NOW only
  when the lines are real Oct entries. A test journal synced by mistake is deleted in QBO by hand.

## 7. Constraints

- Preserve every behavior difference between KZO, KZP, KZDW, S5, UMBER and TINDERPAY on normal rows.
- Read `graphify-out/GRAPH_REPORT.md` before editing any `.py` (repo rule, enforced by hook).
- Document the behavior in `README.md` in the same commit (user rule). Also update
  `appscript/README.md` for the menu change.
- The bot's Google account needs **Editor** on each spreadsheet that holds a month-end source tab.
  Today it only reads COY files. The user must grant this.
- Deploy per the usual procedure (push, then pull and restart `qbo-bot.service` on the EC2 host).
- `ALLOWED_QBO_WORKSPACES` differs per machine: the server has UMBER, local has KZO Sports. This
  work does not change either list.
- Commit order (user decision): the pending KZO step 10d work goes in first, as its own commit.

## 8. Left to the builder

- Module layout. Suggestion: a new `src/logic/month_end.py` (read, select, group, check, number,
  build output) and a branch in each runner right after the control row is read:
  `if is_month_end_row(row): ...; continue`.
- Date parsing (serial numbers and `dd/mm/yyyy`); reuse `parse_mixed_date` where it fits.
- Source row → sheet row: `df_from_values` drops blank rows but keeps index labels, so sheet row
  = header row + 1 + index label. Use the label, never the position.
- Exact `Transform Note` wording; keep it short and say what to do.
- Tests in `tests/` (unittest, same style as existing). Must cover: grouping per company, each
  check, numbering across two rows in one run, rebuild of a missing JV, rebuild after ERROR,
  changed-source stop, sync status write-back, and that normal rows are unchanged.

## 9. Known risks

- Someone clears a `Transformed` / `Synced` status by hand: the line looks new and a second
  journal is made. Mitigation: README says never clear `Status`; to redo, put `ERROR` in the
  Transform tab `Remarks` instead.
- Someone edits a source line after it is `Transformed` and does not flag it: the edit is ignored.
  Same mitigation.
- Someone flags a journal that is already in QBO for rebuild: it is rebuilt under the same number,
  sync finds it in QBO and skips it, so QBO keeps the old version. Synced journals are fixed in
  QBO by hand, as for normal rows.

### Normal rows: reserve journal numbers first (in scope, user decision)

Normal rows append to the Transform file (step 13) **before** writing `Last Journal No` (step 15).
A crash between the two leaves the control counter low, and QBO does not have the numbers until
sync. The next run can then reuse those numbers, and sync silently skips the second journal
(see 6.6). Fix: right after `transform_raw` returns (step 12), write `Last Journal No` to the
control row and raise `global_last_jv`, before step 13. Only that one column moves earlier;
everything else in step 15 stays where it is. Expense and Transfer counters are out of scope
(their Ref Nos are scoped by country and month).
