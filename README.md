# kosaccounts

Unattended bookkeeping pipeline for Kosibah LLC. Receipts and bank statements dropped into
Dropbox are copied to this server hourly, extracted, matched against the known supplier list,
reconciled, and appended to a paste-ready workbook that mirrors the master's Purchases sheet.

Design and decisions: `specs/` (requirements) and `~/.claude/plans/greedy-squishing-canyon.md`.
Dropbox and rclone setup: `scripts/README-dropbox.md`.

## How it runs

| When | What | Log |
|---|---|---|
| every hour at :00 | `scripts/rclone_import.sh` copies Dropbox `/Apps/kosaccounts/*` into `imports/` | `logs/rclone-YYYY-MM-DD.log`, failures in `logs/rclone-errors.log` |
| every hour at :15 | `scripts/run_pipeline.sh` runs `python -m kosaccounts run` | `logs/pipeline-YYYY-MM-DD.log`, run summaries in `logs/run-YYYY-MM-DD.log`, failures in `logs/pipeline-errors.log` |

Both scripts take a lock, so overlapping runs are skipped rather than doubled.

Per run: new files (by content hash, so renames are ignored and edited files are reprocessed)
are found under `imports/receipts` and `imports/bank`; `imports/invoices` is ignored for now.
Receipts are read by a headless Claude call (`claude -p`, model set in `config.toml`); bank
statements are parsed by deterministic parsers (HTML, CSV) with a model fallback that flags the
file for attention. Supplier names are matched by alias, exact, fuzzy, then model adjudication.
Nothing ever blocks: doubtful rows get `Status = Review` and an amber fill.

## Files you will look at

| Path | Purpose |
|---|---|
| `output/kosibah_import.xlsx` | The output workbook. Columns B..M are the master's 12 columns; N..Q are Source file, Processed on, Status, Bank ref. Copy rows into the master each quarter. Written atomically, safe to rsync any time. |
| `data/new_suppliers_pending.csv` | Suppliers seen for the first time, with a suggested category. Approve with `review --approve`. |
| `data/suppliers.csv` | Known suppliers and default categories (seeded from the master). |
| `data/supplier_aliases.csv` | Spelling/descriptor variants -> canonical supplier. Add rows by hand to merge duplicates. |
| `data/categories.csv` | Allowed categories -> Schedule C. The pipeline never invents categories. |
| `data/processing_log.csv` | Ledger of every file processed (hash, date, status, rows added). |
| `logs/last_run.json` | Machine-readable summary of the most recent run. |

## Commands

```bash
cd /home/dannybarnett/claude-coding/kosaccounts
.venv/bin/python -m kosaccounts scan                 # what would be processed next run
.venv/bin/python -m kosaccounts run --dry-run        # full run, model calls included, nothing written
.venv/bin/python -m kosaccounts run                  # what cron runs
.venv/bin/python -m kosaccounts review               # pending suppliers + Review rows
.venv/bin/python -m kosaccounts review --approve "New Supplier=Fabric"
.venv/bin/python -m kosaccounts duplicates           # likely duplicate suppliers in suppliers.csv
.venv/bin/python -m kosaccounts reconcile-ledger     # ledger entries whose source file is gone
.venv/bin/python -m kosaccounts seed                 # rebuild data/*.csv from the master workbook
.venv/bin/python -m pytest                           # test suite
```

## Running by hand and watching progress

```bash
cd /home/dannybarnett/claude-coding/kosaccounts
scripts/rclone_import.sh        # same as the :00 cron job
scripts/run_pipeline.sh         # same as the :15 cron job; output goes to the log, not the screen
```

Follow a run live from a second terminal:

```bash
tail -f logs/pipeline-$(date +%F).log
```

Expect roughly 10 seconds per receipt (each one is a model call); bank CSVs take under a second.
The workbook, ledger and run summary are written only at the end of the run. The scripts take a
lock, so a manual run started while cron's run is active exits at once with an "already running"
line in `logs/pipeline.lock.log`. Running `.venv/bin/python -m kosaccounts run` directly prints
to the screen instead, but takes no lock, so avoid starting it right around :15.

## Reviewing a run

1. `tail logs/run-$(date +%F).log` shows files found/new, rows added, review rows, new suppliers.
2. Open the workbook; amber rows need a look. Common reasons are in the Notes column:
   new supplier, low confidence, amounts that do not reconcile, non-USD currency, missing date.
3. Approve new suppliers so they stop being flagged: `review --approve "Name=Category"`.
4. To reprocess a file, delete its line from `data/processing_log.csv` (and its row from the
   workbook) and it will be picked up next run.

## Receipt filenames

`YYYY-MM-DD Supplier.pdf` gives the reader a date and supplier hint (tie-breakers only; the
receipt itself wins). Dropbox File Requests append the uploader's name, e.g.
`2026-06-12 Guide Fabrics Yemi Osunkoya.pdf`; names listed under `receipts.filename_strip` in
`config.toml` are removed from the hint. Add a new uploader's name there.

## Bank statements

Save the bank's own export, not the web page: CSV ("download transactions") is best, a PDF
statement is fine, an HTML "save page as" is usually an empty application shell. A format no
parser recognises is sent to the model and the file is flagged; add a parser under
`kosaccounts/bank/` and register it in `bank/registry.py`.

## Python environment

`.venv/` is a private Python environment inside the project (built with uv, no sudo needed).
`.venv/bin/python` is the interpreter with this project's packages; cron and the scripts use that
path, so nothing depends on activating it. `requirements.txt` is the source of truth for packages.

```bash
~/.local/bin/uv pip install --python .venv/bin/python -r requirements.txt            # sync after a pull
~/.local/bin/uv pip install --python .venv/bin/python -r requirements.txt --upgrade  # upgrade, then run pytest
rm -rf .venv && scripts/install.sh                                                   # rebuild from scratch
```

Never `sudo pip` or `apt` Python packages for this project; the venv would not see them.

## Setup on a fresh machine

`scripts/install.sh` (prints the sudo apt line, installs uv, builds `.venv`), then
`scripts/README-dropbox.md`, then `crontab scripts/crontab.txt`.
