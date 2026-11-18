# Account Map: keep matching accounts after a QBO rename

Date: 2026-11-18. Status: design, waiting for approval.

## 1. Why

The bot finds a QBO account by its name. When someone renames an account in QBO, every sheet
row that still uses the old name fails with `ERROR | Account not found`, and someone has to fix
code or sheets before the rows can sync.

On 2026-11-18 "Leading Card MKT - 1238" stopped syncing this way (hotfix `aaf7fd7`). An audit
the same day found 5 more names that will fail on the next Transform for the same reason.

The goal, in the user's words: "not too harsh" (the same account written another way should
still match) and "not too easy" (never post to a wrong account).

## 2. What the audit found (2026-11-18)

Data: 6 companies (KZO, KZP, KZDW, S5, UMBER, TINDERPAY), Transform tabs for Sep to Nov 2026
(112 tabs, 320 distinct account names), and every QBO account (835 active).

1. Different wording by analysts is not the problem. Every name that fails today was renamed
   in QBO after its rows synced. 13 synced transactions were opened in QBO: all 13 posted to the
   account the analyst meant (user confirmed).
2. Loose rules are unsafe. Each company has different accounts with almost the same name:
   UMBER `Retained Earning` / `Retained Earnings`, UMBER `Amount Owing by/(to) - KZO group` /
   `Amount Owing by/(to) KZO group`, KZO `KZO COY TRC 4` / `KZO COY TRC 4A`, S5 `Infra` /
   `Infras`. KZO has 740 pairs of different accounts that are 80% or more alike. Ignoring
   punctuation, ignoring plural "s" and matching on 4-digit card numbers each either merges 2
   real accounts or fixes none of the real failures.
3. Reconcile still accepts an 80% similar name, so it can report `Matched` for a row that went
   to the sibling account.
4. One sheet name, "Leading Card ACC", went to 2 different accounts 1 day apart (transfer 55127
   on 7 Oct, purchase 55337 on 8 Oct) because QBO names changed in between. The user confirmed
   both were correct. So when a QBO account has the exact name today, that account is the right
   one.

Names that will fail on the next Transform today:

| Company | Sheet name | QBO account now (Id) |
|---|---|---|
| KZO | Leading Card ACCT - 3214 | Leading Card:Leading Card ACCT:Leading Card ACC - 3214 (1150040028) |
| KZO | CBD Z Card 1 | Cash On Hand:CBD Z 1 (210) |
| KZDW | Amount Owing (by)/to TD | Amount Owing (by)/to TDP (1150040096) |
| S5 | Recruitment Fee | Professional Fees:Recruitment Fees (155) |
| S5 | S5A | Crypto Wallet Balances:S5 A1 (1150040109) |

## 3. Goals, non-goals, anti-goals

Goals
- A renamed QBO account keeps working with no code change and no manual step.
- The team can map a sheet name to a QBO account in a sheet tab, without a developer.
- Transform, Sync, the month-end path and Reconcile all resolve an account the same way.
- Reconcile checks the account by QBO Id, not by a similar name.
- An unknown name fails with a message that says what to do.

Non-goals
- Location, Class, Vendor and Payment Method matching stay as they are (they keep fuzzy).
- Reconcile for Transfers keeps checking only amount and date (no account check added).
- No Apps Script or control-sheet button changes.

Anti-goals (a failure even if it "works")
- Any guessing for accounts: no fuzzy, no "closest name", no partial-word match.
- The bot changes or deletes a row the team typed in the Account Map tab.
- A row posts to an account that is not (a) the exact QBO name today, (b) a team-typed map row,
  or (c) the account this exact sheet name posted to before.
- Behavior differences between KZO / KZP / KZDW / S5 / UMBER / TINDERPAY change, other than
  account matching.

## 4. Approaches considered

- **A. Remember the account (chosen).** The bot saves "sheet name -> QBO Id" the first time a
  name matches. If the name later disappears from QBO (renamed), the saved Id is used. The exact
  QBO name today still wins, which matches finding 4. A tab lets the team add or fix rows.
- **B. Manual map only.** Same tab, but no memory: every rename gives ERROR until someone adds
  a row. Rejected: every failure in the audit was a rename, so B keeps all the manual work.
- **C. Looser automatic rules** (plural, punctuation, abbreviations, card numbers, token
  overlap). Rejected: finding 2.
- Storing the map in a file on the server. Rejected: the team cannot see or fix it.

The user asked for a recommendation and the design follows A.

## 5. Design

### 5.1 The Account Map tab

One tab named `Account Map` in each client's Control Sheet (the spreadsheet in the Master
Sheet's `Spreadsheet ID` column, which already holds `JOBS CONTROL`). Header on row 1:

| Column | Filled by | Meaning |
|---|---|---|
| `Sheet Name` | bot or team | Account text exactly as it appears in the Transform sheet. Matched after collapsing spaces and ignoring upper/lower case. |
| `QBO Account` | bot or team | QBO full account name. The team types this for a Manual row; the bot writes the current QBO name for Auto rows. |
| `QBO ID` | bot (team may type) | QBO account Id. The bot fills it for Manual rows by resolving `QBO Account` (exact or suffix-path rule only). |
| `Type` | bot or team | `Auto` = the bot learned it. `Manual` = the team set it. A blank Type counts as `Manual`. |
| `Realm ID` | bot | The QBO company the `QBO ID` belongs to. Written together with `QBO ID`. |
| `Updated At` | bot | When the bot last wrote the row (`YYYY-MM-DD HH:MM`, server time). |

Rules for the tab:
- The bot never edits the `Sheet Name`, `QBO Account` or `Type` of a Manual row. It only writes
  `QBO ID`, `Realm ID` and `Updated At` on a Manual row.
- One row per Sheet Name. If the tab holds the same Sheet Name twice, a Manual row beats an Auto
  row; between 2 rows of the same Type, the lower row wins and the bot logs a warning.
- Rows with a blank `Sheet Name` are ignored.
- A `QBO ID` counts only when the row's `Realm ID` equals the company being processed. A row with
  another company's Realm ID is ignored (logged once per run). A row with a `QBO ID` but a blank
  `Realm ID` is treated as having no `QBO ID`. This stops a Control Sheet copied from another
  company from posting to that company's Ids. (Claude's call, after review.)

### 5.2 How an account name is resolved (one function, used everywhere)

Input: the sheet account text, the client name, the QBO account list (active accounts:
FullyQualifiedName -> Id, as `QBOSync._get_qbo_mappings` loads it today) and the Account Map.

1. **Clean** the text: collapse whitespace, drop spaces around `:`, apply the KZP alias table
   (`account_aliases.py`, unchanged).
2. **Manual row** for this name. Same rule as everywhere: the name first, the saved Id second.
   - Resolve its `QBO Account` text by step 3's rules. Exactly 1 account -> use it. If that Id
     differs from the row's `QBO ID` (the team changed the target, or the cell was blank),
     Transform saves the new Id.
   - Else, if the row's `QBO ID` is an active account in this company -> use it (the target
     account was renamed in QBO). The bot does not edit the team's `QBO Account` text.
   - Else ERROR
     `Account Map row for '<name>' points to '<QBO Account>', which is not in QBO`.
   (Claude's call, after review: if the saved Id won over the text, a team edit of
   `QBO Account` would be silently ignored.)
3. **Exact QBO name today**: exact full-name match, then suffix-path match (today's rules,
   case-insensitive). Exactly 1 account -> use it. The Transform stage saves or updates the
   Auto row (`Sheet Name` -> this Id, `QBO Account` = current full name).
   - 2 or more accounts (ambiguous): if an Auto row for this name points to one of them, use
     that one. Otherwise ERROR `AMBIGUOUS` as today.
4. **Auto row** for this name -> use its Id if it is an active QBO account. This is the rename
   case. The Transform stage updates the row's `QBO Account` to the current QBO name, so the
   team can see the rename in the tab. Log:
   `[Account Map] '<name>' is not in QBO any more; using saved account <Id> now named '<QBO name>'`.
5. **Nothing found**: ERROR
   `Account not found: '<name>'. Closest in QBO: '<a>', '<b>'. Fix the name or add it to the Account Map tab`.
   "Closest" is for the message only (up to 3 names, 60% similar or more). It is never used to post.

The hardcoded `replacements` table (`"CBD Z Card": "KZO CBD Z"`) is removed from all 3 places.
"KZO CBD Z" no longer exists in QBO, so the rule only breaks rows today. Its case becomes a
seeded Auto row (5.6).

Location / Class / Vendor / Payment Method lookups keep using `find_id_in_map` with fuzzy, as
today.

### 5.3 Who reads and writes the tab

- **Transform** (`run_ingestion.py`, all clients, including the month-end path): reads the tab
  once per client run, before transforming. Collects new and changed Auto rows (and Ids saved
  on Manual rows) in memory. At the end of that client's run, if anything changed, it reads the
  tab again and matches rows by `Sheet Name` (not by the row numbers from the first read, since
  the team may have sorted, inserted or deleted rows in between). Then it writes once: new rows
  appended below the last row, changed rows updated in place. No read and no write when nothing
  changed. (Claude's call, after review.)
- **Sync** (`run_syncing.py`) and **Reconcile** (`run_reconciliation.py`): read the tab once
  per client run. They never write it.
- Tab missing: Transform creates it with the 6 headers on its first write. Sync and Reconcile
  treat a missing tab as empty.
- Tab read fails for any reason other than "missing" (quota, network): stop that client's run for
  the stage and log the error, the same way a failed Transform-tab read stops today. Do not run
  without the map, because a Manual row may be what keeps a row off the wrong account.

Only Transform writes, so 2 stages never write the tab at the same time. Transform runs per
client under the existing single-run lock.

### 5.4 Sync

`QBOSync.find_id("accounts", ...)` uses the 5.2 function. Same results as Transform for the same
map, so a row marked `Ready to sync` resolves to the same account at Sync. The transfer
"From and To resolve to the same account" check keeps comparing resolved Ids.

If a rename happened between Transform and Sync, step 4 still finds the account: Transform
already saved the Auto row.

### 5.5 Reconcile

Journals (each line) and Expenses (payment account, `Account (Cr)`): resolve the sheet name with
the 5.2 function, then compare its Id with the QBO line's `AccountRef.value`.
- Same Id -> account matches.
- Different Id -> mismatch, message shows both names
  (`Account: '<sheet name>' != '<QBO name>'`, same wording style as today).
- Sheet name does not resolve -> mismatch `Account not found: '<sheet name>'`.

`Reconciler._is_account_match` (name and 80% fuzzy compare) and its KZP bare-name fallback are
removed: Ids make both unnecessary. The 4-pass journal line pairing keeps its order and logic;
only its account test changes from "names match" to "Ids are equal".

Reconcile needs the QBO account list (one Account query per client run, as Transform and Sync
already do) and the map.

Known limits, accepted (Claude's call, after review). Both show as a `⚠️` mismatch; neither
posts anything:
- A sheet name the bot never saw, whose account was renamed in QBO before the bot saw it, shows
  `Account not found`. The seeding in 5.6 covers every month the Control Sheets list, so this
  should only affect names the seeding could not learn.
- If QBO later gives a name to a different account (finding 4), older rows with that name show a
  mismatch. This is rare (1 case in the audit).
The alternative, saving the resolved Id on every Transform row, needs a new column in all
Transform tabs and the `Sample - *` templates. Not worth it for these 2 rare cases.

### 5.6 Fill the tab at deploy (one-off)

A one-off script, run once on the server after deploy, over every month each Control Sheet
lists (not only Sep to Nov):
1. Every account name in those Transform tabs that resolves today by step 3 -> Auto row.
2. A name that does not resolve today but has synced rows (a `QBO ID`) -> learn its Id from
   what QBO actually posted, only where that is unambiguous:
   - Expenses `Account (Cr)`: the Purchase's `AccountRef`.
   - Expenses `Expense Account (Dr)`: the Purchase line's account, only if the Purchase has 1
     expense line.
   - Transfers `Transfer Funds From` / `To`: `FromAccountRef` / `ToAccountRef`.
   - Journals `Account`: only if exactly 1 QBO line has the same amount and Debit/Credit side.
   If all of a name's checked rows agree on 1 Id -> Auto row. If they disagree -> not seeded,
   listed in the output.
3. The 5 renamed names in section 2 must come out with the Ids shown there (a check on step 2).
4. "Leading Card ACC" posted to 2 accounts, so step 2 must leave it out. It stays an ERROR until
   the team adds a Manual row.

To limit QBO calls, step 2 reads at most 3 synced rows per name. The script prints what it will
write and writes only with a `--write` flag.

### 5.7 Messages and README

- README gets an "Account Map tab" section: what each column means, the 5 steps, how to add a
  Manual row, and what each ERROR means. The "Account matching" section and the "three
  intentional duplicates" note are updated: there is now one shared resolver.
- `ERROR | Account not found: '<name>'...` keeps its current prefix, so the SYNC NOW guard in
  Apps Script (which looks for `ERROR`) still works.

## 6. Constraints

- Google Sheets read quota (60 reads per minute per user): 1 extra read per client per Sync or
  Reconcile run. Transform: 1 read, plus 1 more read and 1 write only when the map changed.
- QBO account list: only active accounts (today's query). An inactive or deleted account counts as
  "not in QBO".
- Each client keeps its own map. A name in KZO's map never affects KZDW.
- The KZP `Growth` alias table stays in code and runs before the map (step 1).

## 7. Tests the builder must include

- Resolver: each of the 5 steps; Manual beats exact name; exact name beats Auto (the
  "Leading Card ACC" case); ambiguous plus Auto row; Auto Id that is inactive -> ERROR; blank
  Type counts as Manual; duplicate rows.
- Each audit name in section 2 resolves to the Id in the table once seeded.
- The UMBER pairs (`Retained Earning` / `Retained Earnings` and the 2 "KZO group" accounts)
  each resolve only to their own account.
- Transform writes only new or changed rows, in one call, and never edits a Manual row's
  `Sheet Name`, `QBO Account` or `Type`.
- Transform matches rows by `Sheet Name` on the second read: rows sorted or inserted between
  the 2 reads still get the right update.
- Manual row: text that resolves beats a different saved Id; a saved Id is used only when the
  text no longer resolves.
- A row whose `Realm ID` is another company's is ignored; a `QBO ID` with a blank `Realm ID`
  is ignored.
- Sync and Reconcile never write the tab.
- Reconcile: same Id -> match; sibling account -> mismatch (the `TRC 4` / `TRC 4A` case that
  fuzzy passes today).
- A failed map read stops the client's run for that stage; a missing tab is treated as empty.

## 8. Left to the builder

- Module layout (for example a new `src/logic/account_map.py` holding the resolver and the tab
  read/write) and how the resolver reaches the transformer functions (for example inside the
  `qbo_mappings` dict, to avoid changing many signatures).
- Exact log wording, apart from the messages fixed above.
- How the seeding script gets the Sep to Nov names (re-read the tabs, or reuse the audit file).
