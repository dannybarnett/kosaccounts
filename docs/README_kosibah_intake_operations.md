# Kosibah Couture LLC: Dropbox intake pipeline (operations guide)

This section describes how expense documents and bank statements travel from Dropbox to the processing folders on this server, and how to monitor the pipeline and debug it when something stops. The Mac side is built and documented as installed. The Ubuntu side is also built and documented as installed; it has been checked against the running system.

## The flow

1. **Mac (Stage 1).** A launchd service copies top level files from two shared Dropbox folders into the `kosaccounts` app folder in Dropbox.
   * `Kosibah expenses` goes to `Apps/kosaccounts/expenses`
   * `KOSIBAH LLC BANK STATEMENTS` goes to `Apps/kosaccounts/bank`
2. **Dropbox.** Syncs the app folder up to the cloud. This server never writes to Dropbox.
3. **Ubuntu, Dropbox listener (Stage 2).** Long polls the Dropbox application programming interface (API), waits for uploads to settle, downloads a complete batch into a staging folder, verifies it, then moves it into the import folders.
   * `expenses` goes to `KOS_ROOT/imports/expenses`
   * `bank` goes to `KOS_ROOT/imports/bank`
4. **Ubuntu, import watcher (Stage 3).** A Python watcher (the `inotify_simple` package, not the `inotifywait` command line tool -- there is no `inotify-tools` dependency here) notices new files in the import folders, waits for quiet, then runs the processing routine once per folder.
5. **Ubuntu, processor (Stage 4).** `scripts/run_pipeline.sh` (`python -m kosaccounts run`) reads each new file, extracts or parses it, matches suppliers, and appends rows to the output workbook. Not part of Danny's original design sketch above, but it's the stage that actually produces the workbook; see "Processing (server)" below.

`KOS_ROOT` is the server side root folder set in the configuration file (`/mnt/storage/docs/kosaccounts`). It holds only `imports/`, `.staging/` and `state/` -- the data the listener and watcher touch. Code, `config.toml`, `data/`, `output/` and `logs/` all live in this repo (`/home/dannybarnett/claude-coding/kosaccounts`), and the repo's `imports` is a symlink to `KOS_ROOT/imports`, so `imports/expenses/...` means the same files whichever root you use. Nothing in this pipeline is time critical. It is built to wait for completeness rather than speed.

## What is running where

**Mac (built):**
* Service: launchd user agent `com.kosibah.dropbox_copy`
* Script: `~/Library/Application Support/kosaccounts/bin/mac_copy.py`
* Config: `~/Library/Application Support/kosaccounts/mac_copy.conf`
* State: `~/Library/Application Support/kosaccounts/state.json`
* Staging: `~/Library/Application Support/kosaccounts/staging/`
* Logs: `~/Library/Logs/kosaccounts/mac_copy.log` (rotated), plus `launchd_stdout.log` and `launchd_stderr.log`

**Ubuntu (built):**
* `kosaccounts_dropbox_pull.service`: the Stage 2 long poll listener (`intake listen`). Restarts on failure. Runs as `User=dannybarnett`, `WorkingDirectory=/home/dannybarnett/claude-coding/kosaccounts`, `ExecStart=/home/dannybarnett/claude-coding/kosaccounts/.venv/bin/python -m kosaccounts intake listen`, with `/etc/kosaccounts.env` loaded.
* `kosaccounts_reconcile.service` and `kosaccounts_reconcile.timer`: a full check of both app folders against what has been imported (`intake reconcile`), run hourly (`OnCalendar=hourly`, `RandomizedDelaySec=300`) as a safety net and at startup. Same user, working directory and env file as the listener.
* `kosaccounts_import_watch.service`: the Stage 3 watcher (`intake watch`). Restarts on failure. Same user and working directory as the listener; it does not load `/etc/kosaccounts.env` -- it never talks to Dropbox, only to the local `imports/` folders and the processor.
* Secrets: `/etc/kosaccounts.env` (owner root, mode 600), loaded by systemd via `EnvironmentFile=`. Holds `DROPBOX_APP_KEY`, `DROPBOX_APP_SECRET`, `DROPBOX_REFRESH_TOKEN`. Never in the repository or in logs.
* State: `KOS_ROOT/state/intake.sqlite` (what has been imported, so files moved out of the import folders are not downloaded again) and `KOS_ROOT/state/intake.lock` (held by the listener/reconcile while they run).
* Staging: `KOS_ROOT/.staging/expenses` and `KOS_ROOT/.staging/bank`
* Logs: the systemd journal, read with `journalctl` (nothing to rotate by hand), for Stages 2 and 3. Stage 4 (the processor) logs to files in the repo -- see "Processing (server)" below.

## How long things normally take

* Mac: checks every 60 seconds, then waits for two quiet checks, so a batch leaves the Mac roughly 2 to 4 minutes after the last file lands.
* Dropbox upload and sync: depends on file sizes and the connection.
* Server: notices the change almost at once, waits for the app folder to settle (at least 2 minutes: `settle_poll_seconds` x `settle_stable_polls` = 60 x 2 in `config.toml`), downloads, then the import watcher waits `inotify_quiet_seconds` (2 minutes) of quiet before processing.

A typical end to end time is 5 to 15 minutes. Anything over an hour deserves a look; the hourly reconcile timer should catch most stragglers by then.

Timings measured live during acceptance testing on 2026-09-21: the listener woke and started settling within seconds of the Mac's copy; a quiet folder settled in about 65 seconds (one initial listing plus a single 60-second re-check); the watcher's own quiet period was the configured 120 seconds; three receipts were processed by Stage 4 in 22 seconds total (each expense document is one model call, roughly 10 seconds); copying a batch through to rows landing in the workbook took about 3.5 minutes end to end. The hourly reconcile lists both app folders and downloads nothing when nothing is missing.

## Quick health check (Ubuntu)

Run these first when you want to know whether everything is alive.

```
systemctl status kosaccounts_dropbox_pull kosaccounts_import_watch --no-pager
systemctl list-timers kosaccounts_reconcile.timer --no-pager
systemctl --failed
```

Healthy output shows `active (running)` for the two services, a timer with a next run within the hour, and nothing listed under `--failed`.

For more detail on one service (state, last result, exit status, restart count, when it last started):

```
systemctl show kosaccounts_dropbox_pull -p ActiveState,SubState,Result,ExecMainStatus,NRestarts,ActiveEnterTimestamp
```

Check what has arrived recently:

```
ls -lt KOS_ROOT/imports/expenses | head
ls -lt KOS_ROOT/imports/bank | head
find KOS_ROOT/.staging -type f
```

The last command should print nothing when no batch is in flight. Files sitting in staging for a long time mean a batch failed part way and is waiting to retry; the journal will say why.

## Reading the logs (Ubuntu)

```
journalctl -u kosaccounts_dropbox_pull -n 100 --no-pager
journalctl -u kosaccounts_dropbox_pull --since "2 hours ago" --no-pager
journalctl -u kosaccounts_import_watch --since today --no-pager
journalctl -fu kosaccounts_dropbox_pull -u kosaccounts_import_watch
journalctl -u kosaccounts_dropbox_pull --since today --no-pager | grep -iE 'error|warn|fail'
journalctl -u kosaccounts_dropbox_pull -b -1 --no-pager
```

In order: the last 100 lines; the last two hours; today's import watcher activity; a live view of both services together (press Control and C to stop); only problem lines from today; and the previous boot, useful after an unexpected reboot.

Each batch should log the folder, file count, total bytes, and file names. If the journal has nothing from the last several hours, the service is either idle (normal when no files are arriving) or not running (check with `systemctl status`).

Check that logs survive reboots and how much space they use:

```
journalctl --list-boots | head
journalctl --disk-usage
```

If only one boot is listed, the journal is not persistent: create `/var/log/journal` and restart `systemd-journald`. To cap its size, set `SystemMaxUse=` in `/etc/systemd/journald.conf`.

## Processing (server, Stage 4)

Stage 4 is not a systemd unit; it is the command the import watcher (or you, by hand) runs once a
folder is quiet: `scripts/run_pipeline.sh --stage expense|bank <folder>`, which is
`.venv/bin/python -m kosaccounts run` under `logs/pipeline.lock`. This is the stage that reads
each new file (a model call per receipt, roughly 10 seconds; bank CSVs are parsed deterministically
and take under a second), matches suppliers, and appends rows to
`output/kosibah_import.xlsx`. It lives in the repo, not `KOS_ROOT`, and logs to files, not the
journal.

**Logs, all in `logs/` in the repo:**

* `logs/pipeline-YYYY-MM-DD.log` -- the full log for the day, including one line per file, e.g.
  `expense: <file> -> <supplier> <total> <status>`.
* `logs/run-YYYY-MM-DD.log` -- one summary block per run: files found/new/flagged/errored per
  stage, rows added, review rows, superseded rows, new suppliers, unmatched bank/expense counts.
* `logs/last_run.json` -- the same summary as machine-readable JSON, overwritten each run.
* `logs/pipeline.lock` -- the flock file. The watcher passes `KOSACCOUNTS_LOCK_HELD=1` to the
  child when it invokes `run_pipeline.sh`, so the script does not try to re-take a lock it
  already holds; never set that variable by hand.

**Watching a run happen live:**

```
journalctl -fu kosaccounts_import_watch    # "processing folder=... cmd=..." then "finished folder=... exit_code=..."
tail -f logs/pipeline-$(date +%F).log      # the run itself, per-file lines as they happen
```

**After the run:**

```
tail logs/run-$(date +%F).log
cat logs/last_run.json
.venv/bin/python -m kosaccounts review     # pending suppliers, Review rows, bank rows with no receipt
.venv/bin/python -m kosaccounts scan       # what's left unprocessed
```

**Committing and pushing the workbook.** Every run that changes the workbook commits
`output/kosibah_import.xlsx`, `data/suppliers.csv` and `data/supplier_aliases.csv`
(`kosaccounts/gitsync.py`) and pushes to GitHub, so a laptop's `git pull` gets the latest. Look
for the `git:` line at the end of the day's pipeline log:

```
grep "git:" logs/pipeline-$(date +%F).log
git log -3 --oneline
```

Expect `git: committed N path(s)` and `git: pushed to origin`. `git: nothing` means the run did
not change any of those files. `git: push-failed` is logged as a warning, not an error -- the
commit stays local and the next successful push carries it (typical cause: a laptop pushed
first). Controlled by `[processing] commit_outputs` / `push_outputs` in `config.toml`.

**Idempotency and reruns.** There is no `processed` subfolder -- files stay in
`imports/<folder>` after they are handled. `data/processing_log.csv` is the ledger: one row per
file, keyed by its SHA-256 content hash, so an unchanged file is never reprocessed even though it
never moves. To force a rerun, delete the file's line from `data/processing_log.csv` (and its row
from the workbook, if you don't want the old one kept) -- see "Reviewing a run" in the top-level
README.

## If a service stops (Ubuntu runbook)

Work through these in order.

### 1. Is it really stopped?

```
systemctl is-active kosaccounts_dropbox_pull
systemctl is-failed kosaccounts_dropbox_pull
```

`failed` means it crashed and systemd gave up; `inactive` means someone stopped it or it never started; `activating` repeating means it keeps crashing and restarting.

### 2. Why did it stop?

```
systemctl status kosaccounts_dropbox_pull --no-pager -l
journalctl -u kosaccounts_dropbox_pull -n 200 --no-pager
```

Look at the last lines before the stop. A Python traceback names the failing call. If the status says `start-limit-hit`, systemd stopped retrying after too many quick failures; fix the cause, then run `sudo systemctl reset-failed kosaccounts_dropbox_pull`.

### 3. Match the symptom to a cause

* **Missing or unreadable secrets file.** Log says the environment variable is missing or a key error at startup. Check `/etc/kosaccounts.env` exists, is owned by root, mode 600, and has all three values (`DROPBOX_APP_KEY`, `DROPBOX_APP_SECRET`, `DROPBOX_REFRESH_TOKEN`). Note this only affects the listener and reconcile units -- the import watcher does not load this file.
* **Dropbox authorization failed** (log mentions an invalid or expired token, an authentication error, or HTTP status 401). The refresh token was revoked, or the app's permissions changed. Regenerate it with `.venv/bin/python -m kosaccounts intake auth-setup` (run from the repo) and paste it into the env file yourself, then restart. Tokens can be revoked in the Dropbox App Console.
* **Permission error from Dropbox** (log mentions a missing scope). The app needs `files.metadata.read` and `files.content.read`. Add them in the App Console, then regenerate the token, since scopes are fixed when a token is issued.
* **No network or name resolution failure.** Log mentions connection errors or a failure to resolve a name (Domain Name System, DNS). Check with `ping -c 3 api.dropboxapi.com` and `tailscale status`. The service should retry with backoff on its own once the network returns.
* **Storage not mounted or full.** Log mentions no such file or directory, or no space left on device. Check `df -h KOS_ROOT` and `findmnt KOS_ROOT`. The units require the mount, so they refuse to start without it.
* **Cursor reset.** Log mentions a reset. The listener rebuilds its cursor and runs a full reconcile by itself; no action needed unless it repeats.
* **Python package missing after a system change.** Log shows an import error for the Dropbox software development kit (SDK) or for `inotify_simple`. Reinstall the repo's venv (`~/.local/bin/uv pip install --python .venv/bin/python -r requirements.txt`) -- the units always run `.venv/bin/python`, never system Python, so this is the fix regardless of which unit is affected.
* **Lock held.** Log says another instance is running. `sudo lsof` may not be installed on this box; if it isn't, use `fuser KOS_ROOT/state/intake.lock` (listener/reconcile) or `fuser logs/pipeline.lock` (processor) to find the holder's PID. A lock is released when its process exits, so a stale lock file does not block anything by itself.
* **Processor failing (import watcher).** The watcher itself is fine but `scripts/run_pipeline.sh` errors. The watcher's journal line (`processing folder=... cmd=...` then `finished folder=... exit_code=...`) shows the command and its exit code; the actual error is in `logs/pipeline-YYYY-MM-DD.log`, not the journal. Files stay in `imports/<folder>` (there is no "processed" subfolder they move to) so a rerun picks them up -- see "Processing (server)" below.

### 4. Run it in the foreground to see the real error

If the journal is not enough, stop the service and run its `ExecStart` command by hand, as `dannybarnett`, from the repo directory, with the env file loaded. `/etc/kosaccounts.env` is root-only, so read it as root and then drop to `dannybarnett` with `su -p` (preserves the environment you just exported) before running the actual command, so any files it touches stay owned by `dannybarnett` like the real service:

```
sudo systemctl stop kosaccounts_dropbox_pull
sudo bash -c 'set -a; . /etc/kosaccounts.env; set +a; exec su -p dannybarnett -c \
  "cd /home/dannybarnett/claude-coding/kosaccounts && exec .venv/bin/python -m kosaccounts intake listen"'
```

For the import watcher, skip the `EnvironmentFile` step (it doesn't load one) and just run
`cd /home/dannybarnett/claude-coding/kosaccounts && .venv/bin/python -m kosaccounts intake watch`
as `dannybarnett` directly (no `sudo` needed to read a file that doesn't exist for it). `systemctl
cat <unit>` always shows the exact `ExecStart`, `User` and `WorkingDirectory` in case a unit file
changes. Press Control and C to stop it, then start the service again.

### 5. Check Dropbox access without the script

This confirms the credentials work and the app folder is reachable. It prints the names of items at the app folder root and never prints the secrets. Uses the repo's own venv so the `dropbox` package is available.

```
cd /home/dannybarnett/claude-coding/kosaccounts
sudo bash -c 'set -a; . /etc/kosaccounts.env; set +a; .venv/bin/python -c "
import os, dropbox
d = dropbox.Dropbox(oauth2_refresh_token=os.environ[\"DROPBOX_REFRESH_TOKEN\"], app_key=os.environ[\"DROPBOX_APP_KEY\"], app_secret=os.environ[\"DROPBOX_APP_SECRET\"])
print([e.name for e in d.files_list_folder(\"\").entries])
"'
```

You should see `expenses` and `bank`.

### 6. Restart and catch up

```
sudo systemctl restart kosaccounts_dropbox_pull
sudo systemctl start kosaccounts_reconcile.service
sudo systemctl restart kosaccounts_import_watch
```

The reconcile run compares both app folders with the state database and imports anything missing. Restarting the import watcher makes it sweep the import folders once, which covers files that arrived while it was down (the watcher marks every configured folder dirty on startup, since events that happen while it is not running are otherwise lost).

Then follow the journal to confirm a clean cycle:

```
journalctl -fu kosaccounts_dropbox_pull
```

If a file is already sitting in `imports/<folder>` and you don't want to wait for the watcher's quiet period or restart the service, run the processor for that one folder directly, under the same lock the watcher uses:

```
cd /home/dannybarnett/claude-coding/kosaccounts
.venv/bin/python -m kosaccounts intake sweep expenses
.venv/bin/python -m kosaccounts intake sweep bank
```

This is the safe manual trigger -- it runs `scripts/run_pipeline.sh`'s underlying command once for that folder and exits, rather than touching the long-running watcher.

### 7. Testing the import watcher safely

Do not drop a test file in the real import folders unless you mean to process it -- there is no separate mechanism to dry-run the watcher's file-detection step on its own, because it's a small Python loop (`inotify_simple`), not a separate command like `inotifywait`. The safe way to exercise it deliberately is `intake sweep` above, which runs the same processor the watcher would, once, under the same lock, without needing to wait for the watcher's quiet period or touch the real service.

## Forcing a re-import of a file

The state database decides what counts as already imported. To download a file again, stop the listener, remove that file's row from `KOS_ROOT/state/intake.sqlite`, and start the listener and then a reconcile run. `sqlite3` is not installed on this box, so query and edit it through the repo's Python instead. Start by looking at what is imported for a given file:

```
.venv/bin/python -c "
import sqlite3
con = sqlite3.connect('/mnt/storage/docs/kosaccounts/state/intake.sqlite')
for row in con.execute('select * from imported where folder=? and name=?', ('expenses', '2026-03-29 T Mobile Yemi Osunkoya.pdf')):
    print(row)
"
```

Then, with the listener stopped, delete that row:

```
.venv/bin/python -c "
import sqlite3
con = sqlite3.connect('/mnt/storage/docs/kosaccounts/state/intake.sqlite')
con.execute('delete from imported where folder=? and name=?', ('expenses', '2026-03-29 T Mobile Yemi Osunkoya.pdf'))
con.commit()
"
```

Take a copy of the database before editing it. Dropbox itself is never changed by any of this. This only makes the listener pull the file again -- if it was already processed into the workbook, also delete its line from `data/processing_log.csv` (see "Finding where a file got stuck" and the "Reviewing a run" section in the top-level README) so the processor treats it as new too.

## Mac side: monitoring and debugging

### Is it running?

```
launchctl print gui/$(id -u)/com.kosibah.dropbox_copy | head -30
tail -50 ~/Library/Logs/kosaccounts/mac_copy.log
```

The first command shows the job state, its process identifier, and the last exit status. The second shows recent activity. A healthy log shows lines like these:

* `started; poll every 60s, settle after 2 stable poll(s)`
* `expenses: change detected, N file(s) to send; waiting for the folder to settle`
* `expenses: delivered N file(s), B bytes: names`

Watch it live with `tail -f ~/Library/Logs/kosaccounts/mac_copy.log`. Restart it with `launchctl kickstart -k gui/$(id -u)/com.kosibah.dropbox_copy`.

### Symptoms

* **Nothing is happening and the job is not listed.** Reinstall with `./install.sh` from the install folder.
* **Permission errors in the log or in `launchd_stderr.log`.** macOS is blocking the background job from reading the Dropbox folders. Open System Settings, Privacy and Security, Full Disk Access, add the Python binary named at install time, and restart the job.
* **`Dropbox folder not found; set dropbox_root`.** Set `dropbox_root` in `mac_copy.conf` and restart.
* **`source folder not found`.** The source folder name changed or Dropbox has not synced it to the Mac. The names must match exactly.
* **`app folder missing`.** `Apps/kosaccounts` does not exist in Dropbox. The job will not create it; check the Dropbox app is set up and synced.
* **`some files are online only placeholders; batch not ready`.** Some source files are not downloaded to the Mac. Set the source folders to available offline in Dropbox. The job never forces a download.
* **`folder has not settled after 60 minutes; still waiting`.** Files in the source are still changing. Find what is writing to the folder.
* **Log shows `cycle failed; will retry` with a traceback.** The job keeps running and tries again next cycle. Read the traceback; a repeated failure names the file or step.
* **Files not arriving at all but the job looks fine.** The Mac was asleep or logged out (the job only runs while you are logged in and the Mac is awake), or Dropbox is paused or signed out on the Mac. Check the Dropbox menu bar icon.
* **`another instance is already running; exiting`.** Something else, possibly a manual run, holds the lock. Quit it, or restart the job.

### Manual and recovery actions

* Run one immediate pass without waiting for the folder to settle (only when the source is not still filling): `python3 ~/Library/Application\ Support/kosaccounts/bin/mac_copy.py --once`
* Files that already exist at the destination with identical content are skipped, so a rerun never creates duplicates. To make the server pull a file again, use the server side re-import steps above; deleting the Mac state does not do it.
* Remove the agent with `./uninstall.sh` (keeps config, state, and logs) or `./uninstall.sh --purge` (removes them too). Dropbox is never touched.

## Finding where a file got stuck

When a file dropped into Dropbox has not been processed, check each hop in order and stop at the first place it is missing:

1. **Source folder on the Mac.** Is it a top level file, not in a subfolder, and not a hidden, `~$`, `.tmp`, `.part`, or `.crdownload` name? Those are ignored on purpose.
2. **Mac log.** Was a change detected and delivered? If it says it is waiting, the folder is still changing. If it shows a warning, use the symptom list above.
3. **App folder in Dropbox** (`Apps/kosaccounts/expenses` or `bank`, visible in the Dropbox web interface). Is the file there? If not, the problem is on the Mac or in Dropbox sync.
4. **Server journal.** Did the listener see a change and download the batch? Check for staging leftovers with `find KOS_ROOT/.staging -type f`.
5. **Import folder.** Is the file in `KOS_ROOT/imports/<type>`? There is no `processed` subfolder -- a file stays right there once handled, so its presence alone doesn't tell you whether it was processed; check step 7.
6. **Import watcher journal.** Did it fire and start the processor? Look for `processing folder=<type> cmd=...` followed by `finished folder=<type> exit_code=...` in `journalctl -u kosaccounts_import_watch`. If the file is in the import folder but nothing ran, `.venv/bin/python -m kosaccounts intake sweep expenses` (or `bank`) runs the processor for that folder right now, or restart the watcher to force its startup sweep.
7. **Processor ledger and run log.** Is the file's line in `data/processing_log.csv` (match by filename or its SHA-256)? If not, it was never picked up by a run -- rerun Stage 4 (previous step). If it is there, check its `Status` column and the matching entry in `logs/run-YYYY-MM-DD.log` for that date: `Flagged`/`Review` means it was processed but needs a look in the workbook (amber row); an error is in that day's `logs/pipeline-YYYY-MM-DD.log`, one line per file.

## Rules the pipeline keeps

* Nothing is ever deleted, moved, or modified in the source folders, and the server only reads from Dropbox.
* Only top level files are handled; subfolders are ignored at every stage.
* The server never processes a partial batch. Files reach the import folders only after the whole batch has downloaded and been verified.
* The same file name with different content is kept as two files (the newer one gets a suffix from its content hash) and a warning is logged.
* Secrets stay in `/etc/kosaccounts.env` only.

## Maintenance notes

* The first Mac run copies every eligible file already in the two source folders. Expect a large first batch on the server.
* After any change to unit names, folder paths, or log locations, update this section.
* Related documents: the server side build specification and the Mac side install guide (`README_mac_install.md`).
