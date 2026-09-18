# Kosibah Accounts Pipeline: Dropbox Import + Incremental Processing

This spec covers two connected pieces of infrastructure on the Ubuntu server:

1. **The Dropbox import pipeline** (already built, recapped below for context)
2. **An enhancement to the Kosibah monthly accounts skill**, so it processes only newly-arrived files each run, and can be triggered headlessly via Claude Code on a schedule

---

## Part 1: Dropbox Import Pipeline (context, already implemented)

**Storage layout**
- Coding/working directory: `/home/dannybarnett/claude-config/dropbox-import/` (on `sdb2`, the OS disk)
- Actual data storage: `/mnt/storage/claude-config/dropbox-import/` (on `sda2`, the large disk)
- Subfolders (`raw`, `processed`, `logs`) live on `sda2` and are symlinked into the working directory

**Transport**
- `rclone`, configured with a Dropbox remote scoped to an **App folder** (not full Dropbox access), so the import process can only ever see its own dedicated folder
- Runs hourly via cron, using `rclone copy` (never `sync` or `move`): source files in Dropbox are never deleted or altered
- Uses `--checksum` (content-hash comparison, not just modtime) so no new or changed file is ever silently skipped
- Logs per-run stats and a separate error log for failed runs

**What lands in `raw/`:** PDFs (receipts, invoices, bank statements, client payment records) and Excel files, arriving continuously throughout the month.

---

## Part 2: Incremental Kosibah Accounts Processing

### Problem with the current skill

The existing `kosibah-monthly-accounts` skill (see `/mnt/skills/user/kosibah-monthly-accounts/SKILL.md`) processes entire folders (`Purchases/`, `Bank statements/`, `Receipts/`) from scratch each time it runs. With files now arriving continuously via the Dropbox import, this needs to change to: **process only what hasn't been seen before, append to existing output, and run without a human confirming each stage.**

### 2.1 Processing ledger

New file: `processing_log.csv` in the Kosibah folder.

| Column | Description |
|---|---|
| `Filename` | Original filename as it appeared in the source folder |
| `File Hash` | MD5 (or SHA1) hash of the file content |
| `Date Processed` | Timestamp when the file was ingested |
| `Stage` | Which stage processed it (1, 2, or 3) |
| `Status` | e.g. `Processed`, `Flagged`, `Error` |

**Logic on each run:**
1. Compute a hash for every file currently in `Purchases/`, `Bank statements/`, and `Receipts/`
2. Compare each hash against `processing_log.csv`
3. Any file whose hash is *not* already logged is treated as new and gets extracted
4. Files already logged are skipped entirely (no re-extraction, no re-flagging)

Hashing by content, not filename, means a renamed-but-identical file is correctly skipped, and a same-named-but-edited file (e.g. a corrected scan) is correctly reprocessed.

**Edge case to handle:** if a previously-logged file is deleted or replaced in the source folder, the ledger will still show it as processed. Decide whether to add a periodic reconciliation check (flag ledger entries whose source file no longer exists) or leave this as a manual cleanup task.

### 2.2 Incremental output (append, don't rebuild)

Each output spreadsheet (`expenses_output.xlsx`, `bank_statements.xlsx`, `receipts_output.xlsx`) must now be updated incrementally rather than regenerated from scratch:

1. If the output file already exists, load it
2. Extract data only from newly-identified files (per the ledger)
3. Append new rows to the existing sheet
4. Re-apply styling and totals across the *entire* sheet (old + new rows), so formatting and total rows stay consistent
5. Save

### 2.3 Stage dependencies under incremental processing

- **Stage 1** (Purchases) processes only new files since last run, and appends to `expenses_output.xlsx`
- **Stage 2** (Bank statements) processes only new bank statement files, but must cross-reference against the *full* (old + new) `expenses_output.xlsx`, not just newly-added rows, since a bank transaction might match a receipt from a previous run
- **Stage 3** (Client payments) processes only new client payment records

### 2.4 Removing the manual pause/confirm flow

The original skill pauses after each stage for user review. For headless/scheduled operation, this needs to change to:
- Each stage runs automatically if new files are found
- If a stage finds zero new files, it's skipped (no output regenerated, no pause)
- A run summary is written to a log file instead of presented for interactive confirmation: count of new files processed per stage, new suppliers flagged, duplicates flagged, unaccounted bank transactions

**Open decision for Claude Code:** should flagged items (new suppliers, duplicates, unclear amounts) still block the pipeline entirely, or should they be logged for later human review while the pipeline continues processing everything else? Given headless operation, the latter is likely preferable, but this should be an explicit, configurable choice rather than an assumption baked into the code.

### 2.5 New-supplier handling

Currently, unmatched suppliers are flagged orange in the output but never written back to `company_categories.csv`, so the same new supplier can be re-flagged indefinitely. Under headless operation, there's no user present to confirm additions mid-run. Recommended approach:
- Maintain a separate `new_suppliers_pending.csv` that accumulates flagged new suppliers across runs (deduplicated)
- Danny reviews and merges confirmed entries into `company_categories.csv` periodically (manually, or via a lightweight confirm step run interactively when convenient)
- Once merged, those suppliers stop being flagged in future runs automatically, since they'll now match

---

## Part 3: Headless Execution on Ubuntu

Claude Code supports non-interactive execution via the `--print` (`-p`) flag, which runs a single prompt to completion and exits, with no terminal UI and no interactive approval prompts.

**Key flags for unattended/cron use:**

| Flag | Purpose |
|---|---|
| `--print`, `-p` | Run non-interactively; print final result and exit |
| `--allowedTools` | Pre-authorize specific tools so the run never stalls waiting for approval (essential for cron, since nothing is present to approve a prompt) |
| `--permission-mode acceptEdits` | Auto-accept file edits without prompting |
| `--output-format json` | Structured output for logging/parsing, rather than free-form text |
| `--append-system-prompt` | Add run-specific instructions on top of the default system prompt |

**Example cron invocation:**
```bash
0 * * * * cd /home/dannybarnett/claude-config/dropbox-import && \
  claude -p "Run the kosibah-monthly-accounts skill in incremental mode against the Kosibah folder. Process only files not already in processing_log.csv." \
  --allowedTools "Bash,Read,Edit" \
  --permission-mode acceptEdits \
  --output-format json \
  >> /home/dannybarnett/claude-config/dropbox-import/logs/kosibah-run-$(date +\%Y-\%m-\%d).log 2>&1
```

**Scoping tool access:** since this run touches the filesystem and potentially executes Python scripts (for PDF extraction, Excel writing), `--allowedTools` should be scoped as tightly as the workflow allows, rather than blanket-authorizing all Bash commands. Worth defining the specific tool/command patterns Claude Code will need (e.g. `Bash(python3 *)`, `Read`, `Edit`) once the incremental-processing scripts are built, rather than granting unrestricted Bash access in a scheduled, unattended job.

**Sequencing relative to the rclone import:** since new files need to land in `raw/` (or directly in the Kosibah subfolders, depending on final folder mapping) before Claude Code's run can see them, the two cron jobs should be sequenced with a buffer, e.g. rclone at the top of the hour, Claude Code's accounts run 10–15 minutes later, so a slow Dropbox transfer doesn't get processed mid-copy.

---

## Summary of what Claude Code needs to build

1. `processing_log.csv` schema and the hash-check logic to identify new files each run
2. Incremental (append, not rebuild) logic for all three output spreadsheets
3. Modified stage flow: skip stages with no new files, no interactive pause, write a run summary log instead
4. `new_suppliers_pending.csv` handling, decoupled from the interactive confirm step
5. A wrapper script or updated skill invocation suitable for `claude -p`, with an appropriately scoped `--allowedTools` list
6. The cron entry itself, sequenced after the hourly rclone import
