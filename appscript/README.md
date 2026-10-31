# Apps Script: control-sheet buttons

Each client's **QBO Automate Control** Google Sheet has a bound Apps Script project that turns a
status change on the `JOBS CONTROL` tab into a webhook call to the bot (`server.py`). These folders
are `clasp` clones of those projects, pulled 2026-09-29.

| Folder | Apps Script project | Bound to (control sheet) | `client_name` sent |
|---|---|---|---|
| `kzo/` | [KZO] QBO Automation | [KZO] QBO Automate Control | `KZO` |
| `kzp/` | [KZP] QBO Automation | [KZP] QBO Automate Control | `KZP` |
| `kzdw/` | [DPP] QBO Automation | [DPP] QBO Automate Control | `KZDW` |
| `s5/` | [S5] QBO Automation | [S5] QBO Automate Control | `S5` |
| `umber/` | [UMBER] QBO Automation | [Umber] QBO Automate Control | `UMBER` |
| `tinderpay/` | [TDP] QBO Automation | [TDP] QBO Automate Control | `TINDERPAY` |

The script ID of each project is in its folder's `.clasp.json`.

## What the scripts do

`confirmAndSendWebhook(e)` (in `Transform Button.js`, every project) runs on an edit of the
`JOBS CONTROL` tab, row 2 or below, single cell:

| Column | New value | Webhook `event` | Server runs |
|---|---|---|---|
| G `Transform` | `READY` | `pipeline_trigger` | `run_ingestion.py` |
| H `QBO Sync` | `SYNC NOW` | `sync_trigger` | `run_syncing.py` |
| I `QBO Reconcile` | `RECONCILE NOW` | `reconcile_trigger` | `run_reconciliation.py` |

Before sending it:

- ignores repeat edits of the same cell for 10 seconds (`CacheService` lock);
- for `SYNC NOW` only, refuses to start while `QBO Journal` / `QBO Expense` / `QBO Transfer`
  (columns M–O) contain `ERROR` or `FAIL`, and resets the cell to `IDLE`;
- asks the user to confirm (country, month, last month date); `No` resets the cell to `IDLE`.

It then POSTs JSON to `WEBHOOK_URL` with header `X-My-Secret-Token`:
`event`, `spreadsheet_id`, `client_name`, `country`, `tab_name`, `month`, `last_month_date`,
`row_index`. `server.py` queues one job per spreadsheet and runs the script with
`--client <client_name>`. A non-200 reply or a connection error shows an alert and resets the
cell to `IDLE`.

**KZO only** adds a **QBO Automation** menu (`onOpen`) with *Transform / Sync / Reconcile*
dialogs. The user picks a month and several countries; the dialog then works through the matching
control rows one at a time: `triggerSingleRowAction` writes the keyword into the row's status
cell and sends the same webhook (no confirmation prompt), and the dialog polls `checkRowStatus`
until that row finishes before starting the next. For *Transform*, a row whose column T is `TRUE`
is refused and marked `ERROR` ("clean the No. column in the COY raw file"). A failed webhook also
marks the cell `ERROR`. Month-end JV rows (Country or Tab Name contains "Month-end", any
spelling; same rule as the bot) are left out of the menu (`isMonthEndRow_`); they are run from
their own row's dropdowns.

The trigger itself (an installable *On edit* trigger calling `confirmAndSendWebhook`) is set in the
Apps Script editor under **Triggers**. `clasp` does not pull or push triggers.

## Five copies of one script

`kzp`, `kzdw`, `s5`, `umber` and `tinderpay` are the same file except for `client_name` (line
136). KZO's `confirmAndSendWebhook` is the same logic with its own formatting. A change to the
shared behaviour has to be made in all six projects.

`WEBHOOK_URL` (the ngrok address) and `SECRET_TOKEN` are hard-coded in every copy. The token must
match `SECRET_TOKEN` in `server.py`; changing one without the others makes every button fail with
`401 Unauthorized`.

## Working with clasp

clasp 3 is installed globally and logged in as the bot's Google account (`clasp show-authorized-user`).

```bash
cd appscript/kzdw
clasp pull          # fetch the live project into this folder (overwrites local edits)
# edit "Transform Button.js"
clasp push          # replace the live project with this folder
```

Pull before editing: people also edit these projects in the browser, and `clasp push` replaces
the whole remote project with the local files.
