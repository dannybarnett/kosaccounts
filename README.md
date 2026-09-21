# kosaccounts

Unattended bookkeeping pipeline for Kosibah LLC. Expense receipts and bank statements dropped
into Dropbox are picked up by a long-poll listener within minutes, extracted, matched against the
known supplier list, reconciled, and appended to a paste-ready workbook that mirrors the master's
Purchases sheet.

Design and decisions: `specs/` (requirements) and `~/.claude/plans/serene-jingling-rain.md`.
Dropbox setup: `scripts/README-dropbox.md`. Acceptance checklist: `docs/ACCEPTANCE.md`.

## How it runs

Three systemd units, event-driven end to end:

| Unit | What | State |
|---|---|---|
| `kosaccounts_dropbox_pull.service` | Long-polls the Dropbox API; once a folder settles, downloads the batch to a staging area, verifies it, then atomically renames it into `KOS_ROOT/imports/<folder>` | `KOS_ROOT/state/intake.sqlite` |
| `kosaccounts_import_watch.service` | inotify watches `KOS_ROOT/imports/{expenses,bank}`; once a folder has been quiet for 2 minutes, runs `scripts/run_pipeline.sh --stage expense` or `--stage bank` under the pipeline lock | (stateless; the ledger makes reruns idempotent) |
| `kosaccounts_reconcile.timer` | Hourly full reconcile, catching anything the listener's long-poll missed (e.g. it was down) | same lock/state as the listener |

Unit logs go to the journal: `journalctl -u kosaccounts_dropbox_pull -f`,
`journalctl -u kosaccounts_import_watch -f`. The processor's own logs still land in
`logs/pipeline-YYYY-MM-DD.log` and run summaries in `logs/run-YYYY-MM-DD.log`, same as before.

`KOS_ROOT=/mnt/storage/docs/kosaccounts` holds only data the listener/watcher touch:
`imports/`, `.staging/` and `state/`. Code, config, `data/`, `output/` and `logs/` stay in this
repo. The repo's `imports` symlink points at `KOS_ROOT/imports`, so paths like
`imports/expenses/...` below are the same files either way.

Expect roughly 2 minutes settle (Dropbox listener waiting for uploads to stop trickling in) plus
2 minutes quiet (watcher waiting before it processes) between the last file landing in Dropbox
and processing starting -- nothing here is time critical; it trades speed for never processing a
partial batch.

New files (by content hash, so renames are ignored and edited files are reprocessed) are found
under `imports/expenses` and `imports/bank`; `imports/invoices` is ignored (Yemi enters money
received directly in a spreadsheet now, not via Dropbox). Expense documents are read by a
headless Claude call (`claude -p`, model set in `config.toml`); bank statements are parsed by
deterministic parsers (HTML, CSV) with a model fallback that flags the file for attention.
Supplier names are matched by alias, exact, fuzzy, then model adjudication. Nothing ever blocks:
doubtful rows get `Status = Review` and an amber fill.

### Same file name, new content

The listener never overwrites a file. If a newly-uploaded file's name collides with one already
imported but the content differs, it is saved as `<stem>__<hash8><ext>` alongside the original.
The pipeline recognises this pattern as a re-upload of `<stem><ext>`: the earlier row's `Status`
becomes `Superseded`, and if it had already been matched to a bank transaction, that transaction
is released back to `No receipt` so the corrected row can claim it.

### Already-present files are adopted, not re-downloaded

If the listener finds a file already imported by the old rclone-based pipeline sitting at the
expected path in `KOS_ROOT/imports/<folder>` with byte-identical content, it records that file as
imported without downloading or touching it -- it is "adopted" into the state store rather than
fetched again. This is what let the files already on disk when the listener was first installed
carry over cleanly.

### Legacy

The original hourly rclone + cron import was retired on 2026-09-21 after the units above passed
the acceptance checklist in `docs/ACCEPTANCE.md`; its scripts and the rclone remote are gone.

## Monitoring and debugging

Quick answers to "is this working?", in the order you'd normally ask them. For the full runbook
(symptom-by-symptom troubleshooting, the Mac side, forcing a re-import, foreground debugging)
see `docs/README_kosibah_intake_operations.md`.

**Are the services up?**

```bash
systemctl status kosaccounts_dropbox_pull.service kosaccounts_import_watch.service --no-pager
systemctl list-timers 'kosaccounts*'
systemctl --failed
```

**Did the last intake batch land?** Look for a line like `folder expenses: downloaded N file(s)`:

```bash
journalctl -u kosaccounts_dropbox_pull -n 100 --no-pager
```

**Did the processor run and finish?** The watcher logs `processing folder=...` then
`finished folder=... exit_code=...`; the processor's own logs have the detail:

```bash
journalctl -u kosaccounts_import_watch -n 100 --no-pager
tail logs/run-$(date +%F).log
cat logs/last_run.json
```

**How many rows were processed? Any Review rows or new suppliers?**

```bash
.venv/bin/python -m kosaccounts review
```

**What is still waiting?**

```bash
.venv/bin/python -m kosaccounts scan
find /mnt/storage/docs/kosaccounts/.staging -type f   # should print nothing outside a batch in flight
```

**Did the commit and push happen?**

```bash
grep "git:" logs/pipeline-$(date +%F).log
git log -3 --oneline
```

**Timings.** End to end, from the last file landing in Dropbox to processing starting, is roughly
4-5 minutes (settle + quiet, see "How it runs" above); each receipt is one model call (~10
seconds), bank statements are near-instant. Measured live: the listener wakes within seconds of
the Mac's copy, a quiet folder settles in ~65 seconds, and three receipts processed in 22 seconds.

## Files you will look at

| Path | Purpose |
|---|---|
| `output/kosibah_import.xlsx` | The output workbook. **Purchases** tab: columns B..L (Date..Year) are the master's columns (the master derives Schedule C from Category, so it isn't pasted here); M..P are Source file, Processed on, Status, Bank ref; Q is **Copied to master**, human-owned -- Danny types anything into it (e.g. `Y` or a date) after pasting a row into the master, and the pipeline round-trips whatever is there unchanged on every later update, including when a row becomes Superseded; new rows arrive with it blank. `Status = Superseded` (grey, strike-through) marks a row replaced by a re-uploaded receipt or by a later receipt claiming a bank-only row -- kept for history, excluded from duplicate checks and from `review`'s Review-rows listing. **Bank** tab: every parsed bank transaction, one row per statement line, with its own reconciliation state (`Matched` / `No receipt` / `Ignored`) -- the source of truth for bank transactions across runs, so a late-arriving receipt can still claim an old unmatched debit. Duplicate (same date/company/total) and re-uploaded rows follow the same `Status = Review` / `Superseded` rules. Copy Purchases rows into the master each quarter (see "Quarterly copy to the master" below). Written atomically, safe to rsync any time. Every run that changes the workbook commits it (and `data/suppliers.csv` / `data/supplier_aliases.csv`) and pushes to GitHub, so a `git pull` on a laptop gets the latest. The server is the only writer of these files, so on a laptop read or copy from the workbook but do not commit changes to it -- otherwise the server's next push is rejected and needs a manual merge. Turn this off with `[processing] commit_outputs` / `push_outputs` in `config.toml`. |
| `data/new_suppliers_pending.csv` | Suppliers seen for the first time, with a suggested category. Approve with `review --approve`. |
| `data/suppliers.csv` | Known suppliers and default categories (seeded from the master). |
| `data/supplier_aliases.csv` | Spelling/descriptor variants -> canonical supplier. Add rows by hand to merge duplicates. |
| `data/categories.csv` | Allowed categories -> Schedule C. The pipeline never invents categories. |
| `data/processing_log.csv` | Ledger of every file processed (hash, date, status, rows added). |
| `logs/last_run.json` | Machine-readable summary of the most recent run. |

### Quarterly copy to the master

Open `output/kosibah_import.xlsx`, filter the Purchases tab to `Status != Superseded` and
`Copied to master` blank, paste those rows into the master, then mark each one in `Copied to
master` (anything non-blank works, e.g. `Y` or the date you copied it). The pipeline never
touches this column itself, so it's safe to leave it filled in across runs -- a row keeps its
mark even if it's later superseded by a corrected receipt.

## Commands

```bash
cd /home/dannybarnett/claude-coding/kosaccounts
.venv/bin/python -m kosaccounts scan                 # what would be processed next run
.venv/bin/python -m kosaccounts run --dry-run        # full run, model calls included, nothing written
.venv/bin/python -m kosaccounts run                  # what the watcher runs per folder
.venv/bin/python -m kosaccounts review               # pending suppliers + Review rows
.venv/bin/python -m kosaccounts review --approve "New Supplier=Fabric"
.venv/bin/python -m kosaccounts duplicates           # likely duplicate suppliers in suppliers.csv
.venv/bin/python -m kosaccounts reconcile-ledger     # ledger entries whose source file is gone
.venv/bin/python -m kosaccounts seed                 # rebuild data/*.csv from the master workbook
.venv/bin/python -m kosaccounts intake listen        # Dropbox long-poll listener (what the service runs)
.venv/bin/python -m kosaccounts intake reconcile     # one full reconciliation pass (what the timer runs)
.venv/bin/python -m kosaccounts intake watch         # import watcher (what the service runs)
.venv/bin/python -m kosaccounts intake sweep expenses  # run one folder's processor once, by hand
.venv/bin/python -m kosaccounts intake sweep bank
.venv/bin/python -m kosaccounts intake auth-setup    # one-time Dropbox OAuth helper
.venv/bin/python -m pytest                           # test suite
```

Managing the services:

```bash
sudo systemctl status kosaccounts_dropbox_pull.service kosaccounts_import_watch.service
systemctl list-timers 'kosaccounts*'
sudo systemctl restart kosaccounts_dropbox_pull.service
journalctl -u kosaccounts_dropbox_pull -f
journalctl -u kosaccounts_import_watch -f
```

See `scripts/README-dropbox.md` for first-time setup (app permissions, `auth-setup`, the env
file, installing the units) and `docs/ACCEPTANCE.md` for the acceptance checklist to run once
they're installed.

## Running by hand and watching progress

```bash
cd /home/dannybarnett/claude-coding/kosaccounts
.venv/bin/python -m kosaccounts intake sweep expenses   # run the expense stage once, by hand
scripts/run_pipeline.sh --stage expense                 # same thing, via the lock the watcher uses
```

Follow a run live from a second terminal:

```bash
tail -f logs/pipeline-$(date +%F).log
```

Expect roughly 10 seconds per receipt (each one is a model call); bank CSVs take under a second.
The workbook, ledger and run summary are written only at the end of the run. `run_pipeline.sh`
and the watcher share `logs/pipeline.lock`, so a manual run started while the watcher is mid-run
exits at once with an "already running" line in `logs/pipeline.lock.log`. Running
`.venv/bin/python -m kosaccounts run` directly prints to the screen instead, but takes no lock.
When the watcher itself invokes `run_pipeline.sh` it sets `KOSACCOUNTS_LOCK_HELD=1` so the script
skips taking the lock a second time (it already holds it) -- never set that variable by hand.

## Reviewing a run

1. `tail logs/run-$(date +%F).log` shows files found/new, rows added, review rows, new suppliers.
2. Open the workbook; amber rows need a look. Common reasons are in the Notes column:
   new supplier, low confidence, amounts that do not reconcile, non-USD currency, missing date.
3. Approve new suppliers so they stop being flagged: `review --approve "Name=Category"`.
4. To reprocess a file, delete its line from `data/processing_log.csv` (and its row from the
   workbook) and it will be picked up next run.

## Expense filenames

`YYYY-MM-DD Supplier.pdf` gives the reader a date and supplier hint (tie-breakers only; the
receipt itself wins). Dropbox File Requests append the uploader's name, e.g.
`2026-06-12 Guide Fabrics Yemi Osunkoya.pdf`; names listed under `expenses.filename_strip` in
`config.toml` are removed from the hint. Add a new uploader's name there.

Files are matched by content hash, not name, so re-uploading the exact same file again (e.g. an
accidental duplicate Dropbox sync) is silently skipped -- nothing changes. Re-uploading a
*corrected* receipt under the **same filename** (different content -> a new hash) supersedes the
row from the earlier upload: the old row's `Status` becomes `Superseded` with a note recording the
date, and if it had already been matched to a bank transaction, that transaction is released back
to `No receipt` on the Bank tab so the corrected row can claim it on this or a later run.

## Bank statements

Save the bank's own export, not the web page: CSV ("download transactions") is best, a PDF
statement is fine, an HTML "save page as" is usually an empty application shell. A format no
parser recognises is sent to the model and the file is flagged; add a parser under
`kosaccounts/bank/` and register it in `bank/registry.py`.

## Python environment

`.venv/` is a private Python environment inside the project (built with uv, no sudo needed).
`.venv/bin/python` is the interpreter with this project's packages; the systemd units and scripts
use that path, so nothing depends on activating it. `requirements.txt` is the source of truth for
packages.

```bash
~/.local/bin/uv pip install --python .venv/bin/python -r requirements.txt            # sync after a pull
~/.local/bin/uv pip install --python .venv/bin/python -r requirements.txt --upgrade  # upgrade, then run pytest
rm -rf .venv && scripts/install.sh                                                   # rebuild from scratch
```

Never `sudo pip` or `apt` Python packages for this project; the venv would not see them.

## Setup on a fresh machine

`scripts/install.sh` (prints the sudo apt line, installs uv, builds `.venv`), then
`scripts/README-dropbox.md` (Dropbox app permissions, `intake auth-setup`, the `/etc/kosaccounts.env`
file), then `scripts/install_systemd.sh` (installs and starts the three units).
