# Google Sheets -> Transform -> QBO Pipeline

This project runs a repeatable accounting pipeline:

1. Read **raw** data from Google Sheets (monthly tab).
2. Transform in Python (Pandas) into 3 outputs:
   - Journals
   - Expenses
   - Withdraw
3. Write outputs back to Sheets.
4. Push transformed records into QuickBooks Online (QBO).
5. Run reconciliation checks on a schedule (e.g., every 5 minutes).

## Folder Structure

```
config/
  settings.py
  secrets.env              # NOT committed
  service_account.json     # optional
  oauth_client_secret.json # optional
  token.json               # optional
logs/
src/
  connectors/
    gsheets_client.py
    qbo_client.py
  logic/
    transformer.py
    reconciler.py
  utils/
    logger.py
run_ingestion.py
run_reconciliation.py
```

## AI Codebase Graph

This repo has a Graphify knowledge graph in `graphify-out/`.

For AI-assisted architecture work, refactors, or coding optimization, ask the AI to read `graphify-out/GRAPH_REPORT.md` and use `graphify-out/graph.json` or `graphify query "<question>"` before changing code.

Client-specific logic differs across KZO, KZP, KZDW, S5, and UMBER (TINDERPAY runs on the KZDW branches — see [TINDERPAY](#tinderpay-kzdws-td-business-in-its-own-qbo-company)). Future optimization should compare those branches in:

- `run_ingestion.py`
- `src/logic/raw_adapter.py`
- `src/logic/transformer.py`
- `src/logic/syncing.py`
- `src/logic/reconciler.py`

## Apps Script buttons (`appscript/`)

The `READY` / `SYNC NOW` / `RECONCILE NOW` dropdowns on each client's `JOBS CONTROL` tab reach the
bot through an Apps Script project bound to that control sheet, which posts to `server.py`'s
`/webhook`. `appscript/<client>/` holds a `clasp` clone of each project (KZO, KZP, KZDW, S5,
UMBER, TINDERPAY). See [appscript/README.md](appscript/README.md) for what they do, the webhook
payload, and how to `clasp pull` / `clasp push`. The webhook token is hard-coded in all six and
must match `SECRET_TOKEN` in `server.py`.

## Control Sheet (for users)

In your **Control** tab, users only fill these columns:

- `Country`
- `Spreadsheet_URL` (or spreadsheetId)
- `Tab Name` (monthly raw tab name, e.g. `2025-10`)
- `Month` (optional, for tracking)
- `Active` = TRUE/FALSE

The pipeline automatically updates:

- `Last Run At`
- `Last Processed Row` (based on `No.` column in raw data)

### Pending rows

A raw row is held in `Pending Amount Nos` (not pushed to QBO) when it has a `QBO Method`
but `USD - QBO` is 0. Once the amount is filled in, the next run picks the row up
automatically, even though its `No.` is already below `Last Processed Row`.

Rows that had no `QBO Method` yet are not tracked here. KZO picks those up anyway once they
are ready (see [KZO: ready rows below the checkpoint](#kzo-ready-rows-below-the-checkpoint-are-still-picked-up)).
Other clients need their `No`s listed in `Pending Amount Nos` by hand.

### Transform stops when the raw `No` column is broken

Every raw tab numbers its rows with one array formula (`ARRAYFORMULA` / `SEQUENCE`) at the top
of the `No` column. Right after reading the raw tab (step 6a), ingestion looks for Sheets errors
(`#REF!`, `#N/A`, `#VALUE!`, …) in that column. If it finds any, that control row is **not
transformed**: `Transform` becomes `ERROR` and a new **`Transform Note`** column (created on first
use) says what to fix. The note is cleared on the next successful run.

The two error cases are told apart by reading that one column's formulas (only when an error is
present). The same step also stops when the bot cannot find the `No` column at all:

| Case | Message (example) |
|---|---|
| The formula is **blocked** by text typed below it, so no row gets a No | `⛔ TH COY raw file (tab 'Transaction 2026 (COY TH)'): the No formula in AA5832 is blocked by text in AA6001, AA6002. Delete that text, then set Transform to READY again.` |
| One **row** has an error No (its Date or QBO Import cell is broken) | `⛔ TH COY raw file (tab 'Transaction 2026 (COY TH)'): No shows #REF! in row 29496 (usually a broken Date or QBO Import cell). Fix or delete that row, then set Transform to READY again.` |
| A KZO tab's **header row** lost a column the layout needs, so the adapter would fall back to the legacy layout and read `No` from `Transfer to` | `⛔ TH COY raw file (tab 'Transaction 2026 (COY TH)'): row 1 is missing the header 'Checking ( For our use only )', so the bot cannot find the No column. Put it back (it must sit just left of the No column), then set Transform to READY again.` |
| Another client's raw tab has no `No` header | `⛔ KZDW raw file (tab '2026 transactions (new)'): row 5 has no 'No' header, so the bot cannot number the rows. Put it back, then set Transform to READY again.` |

KZO messages name the country (`TH COY raw file`); other clients name the client (`KZDW raw
file`). Up to 10 cells or rows are listed, then `(+N more)`; Google's error explanation is cut to
the code (`#REF!`). The bot does not edit raw files. The column checked is exactly the one the
transform reads `No` from: `raw_adapter.raw_no_column()` reports it from the same layout choice
(`_raw_layout()`) that `standardize_raw_df()` uses. A KZO tab that really is in the old
positional layout (numbers in column Y) is not stopped. Code: `src/logic/no_column_check.py`.

KZO's control sheet also has a `No Column Check` column (T, `ISERROR(IMPORTRANGE(…))`) that the
KZO bulk menu reads. That formula points at column `Y` for most months, not `AA` where KZO's
`No` is, so it can disagree with the check above.

### Duplicate protection: the Transform file outranks `Last Processed Row`

`Last Processed Row` is a checkpoint, not a record. Rows are appended to the Transform file
**before** the checkpoint is written back to the control sheet, so anything that interrupts a
run in that window — a Sheets 5xx on the control write, an expired token, a killed process, a
restarted webhook worker — leaves the work done but the checkpoint stale. Every one of those
rows then looks new on the next run and is transformed a **second** time under a fresh Ref No.
A hand-edited or undone checkpoint cell does exactly the same thing. Once synced, that means
duplicate transactions in QuickBooks.

`preserved_ids` does not save you here: `get_transform_tab_state()` only maps `No -> Ref No`
for rows flagged **ERROR**, so an already-synced row that gets re-selected is not recognised —
it is appended again with a new Ref No.

Two guards, both in `run_ingestion.py`:

1. **Checkpoint self-heal** (step 5b). Each Transform tab is read once anyway for the retry
   context, so that read also reports every `No` the tab already holds. `_heal_last_processed()`
   raises the checkpoint to `max(cell, max(No in Transform file))`. It only ever moves
   **forward**, and logs a warning naming both values when it corrects one. **KZO is
   different:** its checkpoint is exactly the Transform file's highest `No`, so it can also go
   down (see [below](#kzo-last-processed-row-follows-the-transform-file)).
   The Transform tabs are read with `read_tab_values()`: a read that fails (a Google quota
   error, for example) stops the row with `Transform` = `ERROR`. It is never read as an empty
   tab, because "empty" means "nothing transformed yet" and the month would be posted again.
   A tab that does not exist yet (a new month) is empty, which is correct.
2. **Append guard** (step 11b). After the ERROR-retry deletions, `_already_transformed_nos()`
   drops from the batch any `No` still present in the Transform file. Retry `No`s are exempt —
   their rows were just deleted, so rebuilding them is the intent. Skipped `No`s are logged and
   appended to the `Pending Nos Note` column.

Consequences worth knowing:

- **Rewinding `Last Processed Row` by hand no longer forces a re-transform.** It never worked
  the way it appeared to — it duplicated rather than rebuilt. To rebuild a row on purpose, put
  `ERROR` in its Transform row's `Remarks`: the retry path deletes that row and re-creates it
  under the **same** Ref No.
- For KZP / KZDW / S5 / UMBER the guards only move the checkpoint forward, so they cannot
  recover rows **deleted** from the Transform file while the checkpoint stayed high — those are silently behind the checkpoint and
  will never be rebuilt. If you delete Transform rows, lower `Last Processed Row` to the highest
  `No` you kept, then re-run. The raw-vs-transform reconciliation ("possibly skipped rows") is
  what catches this case. KZO is the exception: it re-creates a deleted row whose raw row is
  still ready (see [below](#kzo-ready-rows-below-the-checkpoint-are-still-picked-up)).

#### ERROR retries: the old rows must be gone before the rebuild

The retry path (step 11) deletes a row's old ERROR copy, then appends the rebuilt copy under
the same Ref No. Step 11b lets retry `No`s through for exactly that reason, so if the delete
fails, the run adds a second copy and sync posts both.

S5 Oct 26 (4 Nov 2026) hit this. Rows were deleted one API call at a time and every failure
was ignored. Retries of 392 and 360 rows ran past the Sheets write limit (about 60 per minute),
so most deletes failed with no error. Each retry added another copy: 373 duplicate expenses
and 1 duplicate transfer ($898,750.87) were synced to QBO. They were removed from QBO and the
Transform file on 5 Nov 2026, keeping the first copy of each. The backup and delete log are
kept outside git (`s5_oct_dup_cleanup/` on the operator's machine).

Now:

1. Step 11 (`_delete_retry_rows()`) reads all three Transform tabs with `read_tab_values()`,
   which raises on an API error. The old read turned any error into an empty tab, so the
   delete was skipped without a word.
2. It deletes the rows of all three tabs in **one** `batchUpdate`
   (`GSheetsClient.delete_rows_by_tab()`). Google applies all of it or none of it, so no tab
   is left half done, and a failure is raised. It is not retried: if a delete worked but its
   reply was lost, a retry would delete the rows that moved up.
3. It reads the tabs again. If any retry `No` is still there, the row stops before
   appending, with `Transform` = `ERROR` and the `No`s in the log.
4. If any part of step 11 fails, the retry `No`s are added to `Pending Amount Nos`.
   Some rows may already be gone (for example a delete whose reply was lost). KZP, KZDW, S5
   and UMBER never select a `No` below the checkpoint again unless it is ERROR or pending,
   so this makes the next run rebuild the missing ones. Step 11b skips any still there.
   Set Transform to READY again to retry.

The month-end JV path uses the same delete (`delete_rows()`, one tab). A failed delete there
stops the row, and the next run rebuilds from the source tab's Status.

#### Sync posts each Ref No once per run

Before posting, sync asks QBO which Ref Nos already exist, once per tab. That check cannot
see a Ref No posted a moment earlier in the same run, so if a Transform tab holds the same
Ref No twice as "Ready to sync", both rows used to be posted (S5 Oct 26: 373 duplicate
expenses). Now `run_syncing.py` remembers every Expense and Transfer Ref No it posts in the
run (journals were already posted once per Journal No):

| Second row with that Ref No | Remarks | Posted? |
|---|---|---|
| Same raw `No` (a copied row) | `Skipper (Duplicate row: … Delete this row.)` | No |
| Different raw `No` (a reused Ref No, for example after a stale `Last Expense No`) | `Not posted: Ref No … was already used by No …` | No, so give it a new Ref No and mark it for sync again |

Either case sets that tab's status to `SYNC FAIL` and `QBO Sync` to `PARTIAL ERROR`, so
someone looks. Only a successful post counts: if the first copy failed, the next may post.
The second text avoids the words ERROR and "Ready to sync", so the row is neither rebuilt by
Transform nor picked up again by sync on its own.

#### New rows are written below the last row, never over existing rows

Transform rows used to be added with Sheets' "append", which guesses where a table ends. If
it guesses too early (for example a blank row inside the tab), it writes the new rows **over**
the rows after that point. S5 Oct 26 hit this twice: T0030 landed in the middle of Transfers
(4 Nov), and JV0267 replaced two existing journal lines (5 Nov; QBO was right, the two lines
were restored in the sheet).

`GSheetsClient.append_or_create_df()` now reads the tab, takes the last row that holds any
value (blank rows inside count), adds grid rows if the tab is full, and writes the new rows
directly below it. Nothing above that row is ever written. All clients and the month-end JV
path use it.

#### Journal numbers are saved before the Transform write

Journal numbers (`KZO-JV1418`, …) live nowhere but the Transform file until sync puts them in
QBO. Each run starts from the highest `Last Journal No` in the control sheet or QBO. Two things
used to let a number be minted twice, and sync then skips the second journal as "already synced"
without an error, so it never reaches QBO:

- `Last Journal No` was written in the final control update (step 15), after the Transform write
  (step 13). A run that stopped in between left the counter low.
- The counter was read once per run, so two READY rows of one company in the same run (two KZO
  countries, or a country and its Month-end JV row) started from the same number.

Now step 12b (`_reserve_journal_numbers()`) writes `Last Journal No` right after numbering,
before anything else, and carries the counter to the next row of the run. An interrupted run
can leave a gap in the numbers, never a reused one. Expense and Transfer counters are unchanged.

#### KZO: `Last Processed Row` follows the Transform file

For **KZO only**, `Last Processed Row` is always the highest `No` in that month's three
Transform tabs (Journals, Expenses, Transfers). The bot works it out from the Transform file:

- **Before transforming** (step 5b): the tabs are read for the retry context anyway; their
  highest `No` becomes the checkpoint and is written to the cell straight away. It can go
  **down**: if Transform rows were deleted, their ready raw rows count as new again (and
  step 10d picks up deleted rows below the top). The log says when the cell was ahead of or
  behind the Transform file.
- **After the run** (step 15, or the idle path when nothing was transformed): the new
  highest `No`, as a plain number.

Until 5 Nov 2026 the cell held an `IMPORTRANGE` formula doing the same thing. It needed a
one-time "Allow access" for every new monthly Transform file, and was cached for a few
minutes. It is gone: each row's next run replaces the formula with the number, so no approval
is needed. The cell's old value is ignored for KZO; a `#REF!` there does no harm.

Other clients (KZP / S5 / UMBER / KZDW) keep the plain number, moved forward only.

#### KZO: ready rows below the checkpoint are still picked up

KZO's raw `No` starts with the row's date (see [shifted `No.`](#kzo-shifted-no-when-a-row-is-inserted)),
so `Last Processed Row` (the highest `No` in the Transform file) really means "the latest
date reached". If a later date is transformed before an earlier date is fully categorised,
every earlier row that gets its `QBO Import Method` afterwards sits below the checkpoint. The
`No > Last Processed Row` rule never selected those rows, and the Step 10 log counted them as
`Eligible Old & done`.

TH Sep 2026 exposed it. The 30 Sep 06:04 run transformed the Sep 21–27 rows that had a method
at the time (2,828 rows had none yet), including Sep 27 transfers, so the checkpoint jumped to
`9270036`. The rest of Sep 21–26 was filled in afterwards, and the 09:03 run only added the rest
of Sep 27. That left out 244 ready rows (240 from Sep 21–26, 4 from Sep 10). They were
recovered by listing them in `Pending Amount Nos`.

Step 10d (KZO only) now also selects **ready rows at or below the checkpoint whose `No` is not
in the Transform file**. They are counted as `Missed` in the Step 10 log. `Pending Amount Nos`
rows keep their own path (10b).

The risk is renumbering. Inserting or deleting a raw row renumbers the later rows on that
date, so a `No` missing from the Transform file can be a row that is already there under its
old `No`. Before picking up a date's missed rows, `_kzo_missed_nos()` checks that every
transformed `No` on that date still shows the same amount in raw. It uses the reconciler's rule
(largest absolute amount per `No`, 0.05 tolerance) and skips ERROR rows that are about to be
rebuilt. If any transformed `No` fails that check or is gone from raw, that date's missed rows
are **held**: nothing is transformed for the date, and `Pending Nos Note` says, for example:

```
held 9/15 rows 9150101;9150102: No 9150099 no longer matches its Transform row, so that date looks renumbered. Check the raw tab, then put the Nos to add in Pending Amount Nos
```

The date is the `No`'s `MDD` prefix (`No // 10000`). Descriptions are not compared, because
analysts edit them after transform (7 of 1,579 TH Sep rows), which would hold dates for no
reason. The amount check misses one kind of shift: a row inserted into a run of rows that all
have the same amount, with that same amount itself. That case still leaves QBO totals right.

Things to know:

- **Deleting a KZO Transform row makes it come back** on that month's next run, as long as its
  raw row is still ready. To drop a row for good, put `exclude` in its raw
  `Checking ( For our use only )` cell (step 9).
- Setting an old month's Transform back to READY picks up that month's missed rows too.
- In the Step 10 log, `Eligible Old & done` no longer counts missed or held rows, and
  `Held (renumbered date)` counts the held ones.

### KZO source tabs (all countries)

KZO country tabs (BR, TH, …) share one header set and are mapped **by header name**, not by
column position, so analysts can insert helper columns without breaking ingestion. The 2026
layout added `Transacted Amount Check` (Q) and `Variance Check` (R) mid-sheet; these no longer
shift `USD - QBO`, the QBO method/account fields, or the row number.

The row-number column sits at **AA**, immediately right of `Checking ( For our use only )`, and
its header cell is usually **blank**. The adapter resolves it from that position when no `No` /
`No.` header exists. Columns from `AB` onward are dropdown-helper values and are ignored.

Pre-2026 KZO tabs (same headers, without the two check columns) work unchanged. See
`src/logic/raw_adapter.py` (`_standardize_kzo`, `_resolve_kzo_no_col`) and the
"KZO source layout" section in `DOCUMENTATION.md`.

### S5: the header row is found, not assumed

S5 is the only client whose raw tab keeps a **wallet-balance block above the transaction
grid**, so its header row moves down (or up) whenever an analyst adds or removes a wallet
line. It sat on row 19 until Sep 2026, when an inserted line pushed it to row 20.

Both stages used to hardcode row 19, and reading the wrong row failed **silently**: the row
above the header is a `Follow Invoice Date / Follow Payment Date / Cr / Dr` legend, so every
column came out mislabelled, `_standardize_s5` found no `Date` and no `Category`, its
end-of-function filter dropped all 4,879 rows, and `run_ingestion.py` wrote `DONE (Empty)` as
though the tab had no data at all.

`read_s5_raw_df()` in `src/logic/raw_adapter.py` now locates the header by content. It scans
the first 40 rows for a row carrying one alias from each of:

| Group | Accepted headers |
|---|---|
| Country | `CO` |
| Date | `Date` |
| Category | `Category` |
| Bank | `Bank`, `Bank/Crypto` |
| Method | `QBO Import`, `QBO Way` |
| Amount | `Final Amount to be take (different currency)`, `Final Amount` |

`Date` and `Category` are required precisely because `_standardize_s5` filters on them — a row
this accepts cannot produce the silent empty frame. Matching ignores case, newlines and
repeated spaces, so `P&L (Actual)\nMonth`-style headers are fine.

Behavior:

- Header on row 19 → used silently, as before.
- Header anywhere else in the first 40 rows → used, with an INFO line naming the row it moved
  to (`S5 source header found on row 20 (was row 19)`).
- No header found → **`ValueError`**, so the run fails loudly instead of reporting `Empty`.
- Tab missing → empty frame, unchanged.

The grid is fetched once via `GSheetsClient.read_values()` and the frame built with
`GSheetsClient.df_from_values()`, so discovery costs no extra API call. `read_as_df()` is now a
thin wrapper over those two and behaves exactly as before.

Both `run_ingestion.py` and `run_reconciliation.py` go through `read_s5_raw_df()`. **KZO, KZP,
KZDW and UMBER are unchanged** and keep their fixed header rows (1, 5/4, 5, 4 respectively).

### KZO: blank header cells in the country tabs

Two columns in the KZO country layout routinely ship with an **empty header cell**, so neither
can be found by name:

| Column | Where it sits | Resolved by |
|---|---|---|
| `No` | right of `Checking ( For our use only )` (usually AA) | `_resolve_kzo_no_col()` |
| `CO` | column A, left of `COY` (blank on the **PH** tab) | `_resolve_kzo_co_col()` |

Each falls back to its neighbouring anchor when the header text is missing, and a named header
always wins — the PH tab, for instance, *does* name its `No` column.

This matters because `standardize_raw_df()` picks the KZO layout only when it recognises the
header shape. Requiring the literal `CO` header meant the whole PH tab failed that check and
fell through to the **legacy positional layout**, which maps by index and reads `No` from index
24 — the `Transfer to` column. Every PH `No` came out `0`, so raw-vs-transform reconciliation
reported *every* transformed row as `Unmatched: Missing in Raw -> est. No: not found`, and `CO`
(which drives Location/Class) was empty too.

The legacy fallback maps by position and therefore cannot fail loudly on its own, so a KZO tab
that lands there now logs a warning naming the columns it actually found. If you see
`KZO header shape not recognised`, check the raw tab's headers before trusting anything
downstream.

`run_reconciliation.py` also used to re-label the first 25 columns of the standardized frame by
position. It now reindexes by name against `RAW_STANDARD_COLUMNS`, the same way
`run_ingestion.py` does.

### KZO: shifted `No.` when a row is inserted

KZO's raw `No.` column is generated by a sheet ArrayFormula from **row position**, not a
stable ID: `TEXT(date, "MDD")` (e.g. `731` = Jul 31, `1225` = Dec 25) followed by a
zero-padded running count of same-date rows above it. Inserting or deleting a row anywhere
in a KZO country tab therefore renumbers every later row that shares that same date — the
transaction didn't change, only its `No.` did.

Two places surface this automatically (KZO only; other clients don't use this formula and
are unaffected):

- **Reconciliation (`Raw Reconcile` column, transform sheet).** When
  `Reconciler.reconcile_raw_vs_transform` flags a row `Unmatched: Amt Diff (...)` or
  `Unmatched: Missing in Raw`, it searches the current raw tab for an unclaimed row with the
  same Date + Amount as the transform row (which is ground truth, captured before any later
  shift) and appends a hint, e.g. `-> est. No: 7310132`. Raw is read with
  `UNFORMATTED_VALUE`, so raw Date cells are Excel serial numbers, not strings — the matcher
  handles that directly. If the Date can't be matched (blank/unparseable), it falls back to
  scanning raw Nos within +/-10 of the old No for an amount-only match — the same
  neighborhood an analyst would check by hand — and tags the result
  `-> est. No: 7310132 (nearby match, verify)`. If nothing matches at all it appends
  `-> est. No: not found (row may be deleted)`. If several rows match on Date+Amount, the
  one numerically closest to the old No wins (a shift only ever moves a row by a handful of
  positions) — only a genuine tie (two candidates equally close) falls back to
  `-> est. No: ambiguous (101, 102)`, which needs a manual look.
- **Ingestion (`Pending Nos Note` column, Control sheet).** A `No.` held in
  `Pending Amount Nos` (see below) has no snapshot to re-match by content — if it disappears
  from raw entirely between runs, `run_ingestion.py` logs a warning and writes a note like
  `7310132 (7/31 seq #132, missing — check 7310129-7310135)`, decoded from the formula's date
  prefix plus a +/-3 No range to manually check. The note clears itself once nothing is stale.

Note: an earlier version of this reconcile step also flagged raw rows sitting at/below
`Last Processed Row` with no matching transform content ("possibly skipped rows"), to catch
rows a shift pushed under the checkpoint before they were ever processed. It was removed —
in practice it produced far too many false positives against legitimate recurring
same-amount transactions (e.g. daily fixed-amount transfers) to be useful, and stayed only as
per-transaction reconcile via `est. No:` above.

### Journal reconcile: how sheet lines are paired with QBO lines

`Reconciler.reconcile_journals` finds the QBO journal (by `QBO ID`, else by `Journal No`) and
pairs every sheet line with one unused line of that journal, in four passes:

1. same account **and** same signed amount → `✅ Matched`
2. same amount, different account → `❌ Account Mismatch (Sheet: … vs QBO: …)`
3. same account, different amount → `❌ Amount Mismatch (Sheet: … vs QBO: …)`
4. nothing left → `❌ No matching line in QBO for '…' : …`

Each pass runs over **all** lines of the journal before the next pass starts. Before
2026-09-29 the passes ran row by row, so a sheet line whose QBO line had been removed (e.g. a
TD line taken out of a KZDW journal) could claim a later row's exact line through pass 3 and
leave that row wrongly `No matching line`. Seen on `KZDW-JV0666`: the stale `KZDW FIN OPS TRC 1
400,000` row took the `KZDW FIN OPS TRC 1 29,347.18` line away from the Hengslip fee row. Now
exact matches are handed out first, so a line QBO really has is always `✅ Matched`.

### Account matching: no fuzzy guessing for accounts

Account names from the sheet are resolved against QBO by **exact match**, then **suffix-path
match**, plus the hardcoded `replacements` table for known aliases. There is **no fuzzy
fallback for accounts** — if none match, the row fails with
`ERROR | Account not found: '<name>'` in `Remarks` and is never synced.

Suffix-path matching compares the sheet value against *every trailing path* of the QBO
fully-qualified name, so a sheet value may drop any number of leading parent levels:

| QBO account | sheet values that resolve |
|---|---|
| `Fixed Assets:Equipment` | `Equipment`, `Fixed Assets:Equipment` |
| `Marketing:RnD:AI Expenses` | `AI Expenses`, `RnD:AI Expenses`, `Marketing:RnD:AI Expenses` |

Both the "after the last colon" and "after the first colon" forms are accepted, because
analysts write either one. A bare middle fragment (`RnD`) is deliberately **not** a match.

If one name resolves to **two different accounts** under different parents (e.g.
`Marketing:Growth` and `Sales:Growth` for a sheet value of `Growth`), accounts **refuse** and
report `AMBIGUOUS` rather than pick one — same reasoning as the fuzzy ban below. KZO's current
chart of accounts has no such collisions; KZP's does, and resolves it with the alias table
described in the next section.

This is deliberate. A real chart of accounts contains siblings that differ only by a short
code — KZO has `Investment in HR Company`, `(MP)`, `(OSR)` and `(ORZ)` (71002/71006/71004/71009).
Against their fully-qualified names, `(ORZ)` scores **0.82** similarity to `(OSR)`, above the
old 0.80 fuzzy cutoff, so a missing or mistyped account was silently booked to whichever
sibling happened to be closest. A loud failure a human fixes in the sheet beats a wrong
posting nobody notices.

Fuzzy matching is still used for **vendors, classes, locations and payment methods**, where
names genuinely vary and a near-miss does not misstate the books. A failed account lookup logs
the closest QBO names as a hint, but never selects one.

#### KZP only: the duplicated `Growth` account

KZP's chart of accounts holds **two** accounts named `Growth` — one under `Marketing Income`,
one under `Marketing Expense` — so the bare leaf name no longer identifies an account and
suffix-path matching alone reports `AMBIGUOUS`. What tells them apart in the source sheet is
the sub-category, so the sheet convention keeps it: the expense one carries its parent as a
` - <parent>` suffix, and the bare name means income.

| Sheet / Transform value | Resolves to |
|---|---|
| `Growth` | `Marketing Income:Growth` |
| `Growth - Marketing Income` | `Marketing Income:Growth` |
| `Growth - Marketing Expense` | `Marketing Expense:Growth` |
| `Marketing Expense: Growth` (spaces around `:` allowed) | `Marketing Expense:Growth` |

Rules:

- **KZP only.** KZO / KZDW / S5 / UMBER still refuse `Growth` as `AMBIGUOUS` — the convention
  does not leak between workspaces.
- The **Account column keeps whatever the analyst wrote** (`Growth - Marketing Expense`), in the
  raw *and* the Transform file. The alias is applied at lookup time, in all three stages, so
  the sub-category hint stays visible to a human reading either sheet.
- The alias target is still a *name*, so it goes through normal exact + suffix-path matching
  afterwards. If QBO nests the pair deeper (`Income:Marketing Income:Growth`), it keeps working.
- For an aliased account, the **reconciler drops its fuzzy fallback** and requires an exact or
  suffix-path match. `Marketing Income:Growth` scores **0.81** against `Marketing Expense:Growth`,
  so the fuzzy check would have passed the exact wrong-parent posting the alias exists to catch.
  The one exception: when QBO reports a **bare** account name (no `:` path) the payload carries
  no parent to compare, so the reconciler falls back to a leaf comparison rather than raise a
  mismatch it cannot prove.
- Anything else — accounts not in the table, other clients — is untouched.

The table lives in `src/logic/account_aliases.py` (`KZP_ACCOUNT_ALIASES`), shared by all three
matchers. Add a new collision there rather than loosening a matcher.

Implemented as `allow_fuzzy` in `find_id_in_map` (`src/logic/transformer.py`) and a
`mapping_key != "accounts"` guard in `QBOSync.find_id` (`src/logic/syncing.py`), with a third
copy in `Reconciler._is_account_match` (`src/logic/reconciler.py`). The three matchers are
intentional duplicates — change them together. Only the alias table is shared
(`src/logic/account_aliases.py`).

Note the sync log only prints `[Sync Map]` for a **suffix/leaf** match. An exact match is
silent by design, so a section with no `[Sync Map]` lines means every account matched exactly —
typically transfers, whose bank accounts sit at the top level of the chart.

### Transfer duplicate check is scoped by month

Journals and expenses are checked for duplicates with `WHERE DocNumber IN (...)`, which QBO
indexes. Transfers carry their `Ref No` inside `PrivateNote` instead, and **QBO cannot filter
on that field** — `WHERE PrivateNote LIKE 'KZOTH0726T%'` is rejected with `400 Bad Request` —
so the notes must be fetched and scanned locally.

That fetch is therefore bounded by the control row's month (`_month_bounds` in
`run_syncing.py`). Without bounds, `QBOClient.query()` appends its own
`STARTPOSITION`/`MAXRESULTS` and keeps paging to the end of the table:

| scope | rows | time (KZO) |
|---|---|---|
| unscoped | 14,064 | **320s** |
| one month | 1,498 | **41s** |

The statement carries no `MAXRESULTS` of its own — one previously did, but pagination
overrode it, so it only looked like a limit. The check runs **once per sync section**, not per
row, and logs what it scanned (`🔍 Transfer duplicate check scanned N note(s) [start to end]`).
If the month can't be parsed it still runs unscoped, with a warning.

### Transfer: From and To must be different QBO accounts

QBO rejects a transfer whose two sides resolve to one account, and its answer is a plain
`400 Bad Request` on `/v3/company/<realm>/transfer` with no hint in the URL. Only transfers
can fail this way — journals and expenses have no equivalent constraint.

Blocking identical *text* is not enough: two different names can resolve to the same QBO
account through the explicit replacements (`CBD Z Card` -> `KZO CBD Z`) or through a leaf name
matching a sub-account. The check therefore compares the **resolved account Ids**, and runs at
both stages:

- **Transform** (`process_transfers` -> `validate_transfer_row`) writes the remark into the
  sheet, so the row is visible as broken before any sync runs:

  ```
  ERROR | 'From' and 'To' resolve to the same QBO account (Id 77): 'CBD Z Card' -> 'KZO CBD Z' | Row No: 12
  ```

- **Sync** (`QBOSync.push_transfer`) repeats it, because a row can be marked `Ready to sync`
  under one chart of accounts and be re-synced after the account is merged or renamed:

  ```
  ERROR: Transfer From and To resolve to the same QBO account (Id 77):
  'CBD Z Card' -> 'KZO CBD Z'. Money cannot move within one account; fix the From/To columns.
  ```

The plain same-text check stays ahead of both and keeps its own shorter message.

### QBO 400s report the reason and are not retried

`QBOClient._request_with_retries` used to hand every non-2xx status to
`resp.raise_for_status()`, whose `HTTPError` was caught by the transient-error handler. A
permanent rejection was therefore retried four times with backoff and surfaced as:

```
ERROR: QBO request failed after 4 attempts: 400 Client Error: Bad Request for url: .../transfer
```

— the URL, never the reason. QBO does send one, in a `Fault` envelope in the response body.
Now:

| status | behavior |
|---|---|
| 429, 500, 502, 503, 504 | retried with backoff (unchanged) |
| 401 | cached token cleared, retried **once**, then reported |
| other 4xx | **no retry**; raised immediately with QBO's own fault text |

`_fault_detail()` unwraps `Fault.Error[]` into `[code 6000] Business Validation Error | You
need to choose a different account for the transfer.`, which lands in the sheet's status
column, so a rejected row now says what is wrong with it.

### KZO: Reclass rows with `Category = Transfer`

A `QBO Method = Reclass` row normally becomes a **single** journal line against
`From Account` (falling back to `Type`). When its `Category` is `Transfer`, the row instead
becomes a **debit/credit pair**, because it describes money moving between two accounts.
Direction follows the sign of `USD - QBO` **as it appears in the raw sheet**, before the
existing Reclass sign flip:

| raw `USD - QBO` | negative line | positive line |
|---|---|---|
| `> 0` | `From Account` | `To Account` |
| `< 0` | `To Account` | `From Account` |

Both lines share one `Journal No`, and the pair nets to zero. `From Account` / `To Account`
are the raw columns of those names (standardized as `Account Fr` / `Account To`) — **not** the
`Transfer from` / `Transfer to` columns used by the Transfer method. The `Type` fallback is
deliberately not applied here: a blank From/To fails validation rather than silently posting
to a category account.

KZO only. UMBER has its own `Category = Transfer` handling in the standard-journal branch;
KZP, KZDW and S5 keep the plain single-line reclass behavior.

### KZDW COY filter (DPP only)

KZDW posts only rows whose `COY` is listed in `run_ingestion.py`:

```python
KZDW_ALLOWED_COY_VALUES: set[str] = {"DPP"}
```

Step 9b of ingestion (after the date and `Exclude` filters) drops every KZDW row whose `COY`
(case-insensitive, whitespace-trimmed) is not in that set — `TD`, `KZDW`, blank, anything else —
and logs the dropped counts per `COY`:

```
🏷️ [KZDW] Step 9b: COY Filter (DPP only) -> Kept: 833 | Dropped: 892 {'TD': 892}
```

Dropped rows are gone for that run: they are not new, not written to `Pending Amount Nos`, and
not retried from ERROR. A row whose `COY` is corrected to `DPP` later is picked up only if its
`No` is still above `Last Processed Row`. The filter applies to the `KZDW` workspace only, never
to TINDERPAY.

Why a filter and not a pending hold: KZDW's raw `No` is positional (month, day, then the row's
position within that date), so deleting TD rows from the raw tab renumbers the DPP rows after
them. Held TD Nos parked in `Pending Amount Nos` could then point at a different, already-posted
DPP row and post it again through the late-filled path.

`COY = KZDW` appears only in the Jan–Jun 2026 raw rows, and those control rows are `CLOSED`.
Reopening one of those months would transform nothing until `KZDW` is added to the set.

History: `COY = TD` held as pending 2026-07-22, released 2026-07-30, held again 2026-09-25 when
TINDERPAY went live, replaced by this DPP-only filter 2026-09-29. TD rows KZDW posted before
2026-09-25 are still in KZDW's QBO and in the KZDW Transform files (Location `TDTH`).

### TINDERPAY: KZDW's TD business in its own QBO company

TINDERPAY (Master Sheet `Client Name` = `TINDERPAY`, QBO company "KZDW Tinderpay") reads the
**KZDW raw layout** and follows every KZDW rule: header row 5, `_standardize_kzdw`, currency
per row, date+currency journal grouping, multicurrency transfers with FX. The switch is
`settings.is_kzdw_family(name)`, which every KZDW branch now calls (KZDW is still matched by
the `kzdw` substring, as before).

What differs from KZDW:

| | KZDW | TINDERPAY |
|---|---|---|
| Journal No | `KZDW-JV0001` | `TDP-JV0001` |
| Expense / Transfer ref | `KZDW0926E0001` / `KZDW0926T0001` | `TDP0926E0001` / `TDP0926T0001` |
| COY rows posted | `DPP` only ([filter](#kzdw-coy-filter-dpp-only)) | all (its raw is TD) |

The QBO journal-number lookup in `run_ingestion.py` takes its prefix from the transformer's
`_build_id_prefixes()`, so it always queries the prefix actually minted.

The QBO company must be set up like KZDW's before a sync can succeed: USD home currency,
multicurrency on, location tracking on, and the accounts and locations the raw rows name.
Accounts are matched exactly, so a missing account fails the row with
`ERROR | Account not found` rather than posting.

#### Adding a new QBO company

1. `python setup_qbo_company.py --save-master --client-name "<NAME>" --spreadsheet-id "<control sheet id>"`
   runs the Intuit OAuth flow and writes Realm ID + Refresh Token into the Master Sheet.
2. Add `<NAME>` to `ALLOWED_QBO_WORKSPACES` in `config/secrets.env` locally **and** on the
   server, then restart `qbo-bot.service`. Unlisted workspaces are skipped and their QBO calls
   refused.
3. Give the control sheet its own Transform File. A control row copied from another client
   carries that client's Transform File, `Last Processed Row` and `Last Journal/Expense/Transfer
   No`; clear them. Note that a blank Transform File makes the next run create a fresh one and
   reset `Last Processed Row` to 0, so if the new company takes over rows another company
   already posted, create the Transform File first and set the checkpoint yourself.
4. A new client family (a raw layout none of the existing branches reads) needs code in the
   files listed under [AI Codebase Graph](#ai-codebase-graph).

### Month-end JV rows

Month-end journals (group cost reclasses and the like, posted with SaaSant until Sep 2026) go
through the bot from **Oct 2026**: the team types the lines in a source tab, then uses the normal
Transform and SYNC NOW dropdowns. Code: `src/logic/month_end.py` (pure planning) and
`_process_month_end_row()` in `run_ingestion.py`. Design:
`docs/superpowers/specs/2026-10-30-month-end-jv-design.md`.

**Control row.** One per company per month, in that company's control sheet. A row is a month-end
row when its `Country` **or** its `Tab Name` contains `Month-end`, in any spelling (`Month-end`,
`Monthend`, `Month end`, `month_end`). `is_month_end_row()` in `src/logic/month_end.py` is the one
rule; the KZO script's `isMonthEndRow_` copies it. So a row with a blank Country (KZO row 83,
2026-10-31: it was read as a COY tab and stopped asking for a `No` column) or with its own
country (S5 row 10, `S5` / `Month-end JV`) still runs as month-end. Do not put "month-end" in the
tab name of a normal COY tab.

| Column | Value |
|---|---|
| `Country` | `Month-end JV` is clearest. Blank or a country also works when the tab name has "Month-end". |
| `Source File` / `Tab Name` | The spreadsheet and tab with the lines (KZO keeps its tab in the TH COY file). |
| `Month` / `Last Month Date` | As for normal rows. Only lines dated in that range are taken. |
| `Transform File` | Leave blank: the bot creates `<client> - Month-end JV QBO - Oct 26`. File and tab names always use `Month-end JV`, never the Country, so a month-end row can never write into a country's `TH Oct 26 - Journals` tab (Transform deletes ERROR journals from the tab it reads). |
| `No Column Check` (KZO) | Leave blank, or the script blocks Transform. |

A second `Month-end JV` row for the same month is refused (`Transform` = `ERROR`): two rows would
rebuild each other's journals. The KZO bulk menu skips these rows; run them from their own
dropdowns.

**Source tab.** Header on row 1, found by name: `Date, Memo, Account, Amount, Name, Location,
Currency Code, Class, Status`. `Date, Memo, Account, Amount` are required (a missing one stops
Transform and the note names it). `Status` belongs to the bot; if the column is missing, the bot
adds it after the last header. Each company needs its own tab: a Status with another company's
prefix (`KZP-JV…` in a KZO run) stops Transform.

- Amount: positive = debit, negative = credit. Formulas with long decimals are fine; nobody has to
  ROUND in the sheet. The bot first checks the **unrounded** lines: if they do not add up to zero
  (gap of half a cent or more), the journal is `ERROR: Journal does not balance (diff …)`. Then it
  rounds each line to cents, half away from zero like Sheets shows it (11,702.517… → 11,702.52).
  If rounding leaves a gap (0.01 or so), it moves it onto the journal's **largest negative line**
  and says so on that line: `Transformed KZO-JV1418 (rounding: -0.01 on this line to balance)`,
  plus `Rounding to cents moved …` in `Transform Note`. The note stays after sync
  (`Synced KZO-JV1418 (rounding: …)`). Normal rows keep their own rule (round, fix up to 0.50 on
  the last line).
- Grouping: lines with the same Date + Memo are one journal; for KZO (and KZO Sports) also the same
  Account. A KZO entry that spans two accounts therefore fails as unbalanced.
- Currency: KZO, KZP, S5, UMBER post USD only. KZDW / TINDERPAY: one currency per journal; a
  non-USD journal posts at QBO's rate for that date.
- Location and a typed Class must exist in QBO. A blank Class takes the Location. S5 / UMBER leave
  both blank, as on normal rows. Account matching is the normal exact / suffix-path rule.

**Transform.** Writes `Month-end JV Oct 26 - Journals` from the control sheet's
`Sample - Journals_monthend` template (its header notes come along; use Insert > Note, comments do
not copy), or `Sample - Journals` if that tab does not exist. Lines have a blank `No` and
`Remarks` = `Ready to sync`. Journal numbers continue the client's normal sequence. Order, so a
stopped run never duplicates:

1. Read the Transform tab and the source. A failed read (a 429, a timeout) stops here: it must not
   look like "no tab yet", or every journal not synced yet would be copied again. If the source
   has no `Status` column but the Transform tab already has journals, stop: every line would look
   new and the month would be posted again.
2. Drop every journal in the tab that has an `ERROR` remark (it is rebuilt from the source).
3. Group, check, number. Re-read the source; if any used row moved or changed, stop
   (`the tab changed during Transform`).
4. Save `Last Journal No`. From here the numbers are taken: any later failure still hands them
   on, so the next row of the same run starts after them.
5. Write `Status`: `Transformed KZO-JV1418`, or `ERROR: <reason>` on every line of a failed
   journal. If the bot cannot write (no **Editor** access on the source file), stop; nothing is
   copied.
6. Append the good journals. If this fails, run Transform again: the journals are rebuilt under
   the same numbers.

Lines with `Status` blank or `ERROR…` are new. `Transformed <JV>` whose journal is missing from the
tab is rebuilt under the **same** number; `Synced <JV>` is never read again. `Transform Note` sums
it up (journals ready, journals with errors, lines left for another month). `QBO Journal` =
`ERROR` while any line has an error, which blocks SYNC NOW until it is fixed.

**Sync.** Posts the tab like any Journals tab, with one extra check: a number that is already in
QBO is skipped as "already synced" only if that QBO journal has the same date and memo
(`PrivateNote`, the first line's Memo). Amounts are not compared, so an edited rebuild of a journal
that is already posted is skipped, never posted twice. Otherwise the lines get
`ERROR: <JV> is already used in QBO by a different journal` and `QBO Journal` = `SYNC FAIL`.
Sync never writes `ERROR` to the source (an ERROR line would get a new number and could post
twice); afterwards only posted journals change to `Synced <JV>` in the source.

To clear a clash, open that number in QBO:
- A different journal uses it: clear the `Status` of these lines (the one time you may), then run
  Transform. They get a new number.
- It is this journal (its memo or date was changed after posting): fix QBO by hand if needed, then
  type `Synced <JV>` in `Status` for these lines.

**Reconcile.** QBO check only; there is no raw `No` to compare.

**Rules for the team**

- Never type in or clear `Status`, except to clear a clash (above). A cleared `Transformed` line
  looks new and makes a second journal. Never delete the `Status` column. To redo a journal that is
  not synced yet, edit the source lines and put `ERROR` in its `Remarks` in the Transform tab, then
  run Transform. A synced journal is fixed in QBO by hand.
- Do not create a Month-end JV row for a month SaaSant already posted.
- The bot's Google account needs **Editor** access on every file that holds a month-end tab.

### Monthly deployment (new month tab)
Create a new row in Control sheet with:
- same `Spreadsheet_URL`
- new `Tab Name` (new month)
- set `Last Processed Row` = 0
- `Active` = TRUE

No code change needed.

## Setup

### 1) Create venv & install
```bash
python -m venv .venv
source .venv/bin/activate   # (Windows: .venv\Scripts\activate)
pip install -r requirements.txt
```

### 2) Configure secrets
Copy `config/secrets.env` template and fill values.

### 3) Google Sheets auth
Recommended: OAuth (work account)

- Put your OAuth client secret JSON at: `config/oauth_client_secret.json`
- First run will open a browser login (or you can generate token.json on your laptop and copy it to server).
- Token is saved at `config/token.json`

Alternative: service account
- Put service account json at `config/service_account.json`
- Share the spreadsheet with that service account email

### 4) Run ingestion
```bash
python run_ingestion.py
```

### 5) Run reconciliation
```bash
python run_reconciliation.py
```

## Scheduling (every 5 minutes)

Use **cron** or **systemd timer**:

Cron example:
```bash
*/5 * * * * /path/to/.venv/bin/python /path/to/run_reconciliation.py >> /path/to/logs/cron.log 2>&1
```

## Where to plug your existing notebook logic
- Copy your cleaning/splitting logic from `Accounting_Clean.ipynb` into:
  - `src/logic/transformer.py :: transform_raw()`
- Copy your QBO payload mapping/push logic (from `API_QBO_flow.ipynb`) into:
  - a new module (recommended) `src/logic/qbo_mapper.py`
  - call `qbo.create_journal_entry(...)` / `qbo.create_purchase(...)` / `qbo.create_deposit(...)` in `run_ingestion.py`
