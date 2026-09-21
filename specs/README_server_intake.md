# Kosibah Couture LLC: file intake pipeline (server side)

This section tells Claude Code how to build and operate the file intake on the Ubuntu server for the monthly accounts. It moves expense documents and bank statements from Dropbox onto the server and then triggers the existing processing. Claude Code writes all scripts and systemd unit files; this document is the specification.

Stage 1 runs on a Mac and is not built here. It is documented below only as a dependency, so the server side behavior makes sense as a whole. Do not implement anything for the Mac from this document.

## The whole flow

1. **Stage 1, Mac (dependency, built separately).** Copies top level files from two shared Dropbox folders into the `kosaccounts` app folder in Dropbox.
2. **Stage 2, Ubuntu server (Dropbox listener).** Notices changes in the app folder through the Dropbox application programming interface (API), waits for uploads to settle, then downloads a complete batch into the server import folders.
3. **Stage 3, Ubuntu server (import watcher).** Notices new files in the import folders using inotifywait, waits for quiet, then runs the processing routine.

Nothing here is time critical. Prefer completeness and correctness over speed. A single drop can contain many files (for example a full year of statements), and the pipeline must never process a partial batch.

## Names and locations

Use "expenses" everywhere. The folder was previously called "receipts". If any existing script, document, or path still says "receipts", update it consistently and list what changed in the summary of your work.

Dropbox source folders (Stage 1 input, Mac only; both sit at the root of Dropbox):
* `Kosibah expenses`
* `KOSIBAH LLC BANK STATEMENTS`

Dropbox app folder (Stage 1 output, Stage 2 input). The Dropbox app is named `kosaccounts` with App folder access, so on the Mac it appears as `Dropbox/Apps/kosaccounts`, and to the API it is the root (the empty path):
* `Apps/kosaccounts/expenses` (API path `/expenses`)
* `Apps/kosaccounts/bank` (API path `/bank`)

Server local folders (Stage 2 output, Stage 3 input), under a root set in configuration as `KOS_ROOT`:
* `KOS_ROOT/imports/expenses`
* `KOS_ROOT/imports/bank`

Mapping:
* `Kosibah expenses` goes to `Apps/kosaccounts/expenses`, which goes to `KOS_ROOT/imports/expenses`
* `KOSIBAH LLC BANK STATEMENTS` goes to `Apps/kosaccounts/bank`, which goes to `KOS_ROOT/imports/bank`

## Stage 1 contract (Mac, dependency only)

Separate Mac instructions will follow. Until then, treat this contract as authoritative. The server can rely on these behaviors:

* Copy only. Stage 1 never deletes, moves, or renames anything in the source folders.
* Top level files only. Subdirectories inside either source folder are ignored.
* Buffered. Stage 1 compares snapshots of the source folder and copies once the source has stopped changing, so a large drop is copied as one batch.
* Source files are set to available offline in Dropbox on the Mac, so whole files are present locally when the copy starts.
* Scheduling on the Mac is handled by launchd (the macOS service manager).

What this means for the server:

* Files arrive in the app folder gradually, over minutes, not atomically. A file's presence in the app folder does not mean the batch is complete.
* Never write to, move, or delete anything in Dropbox from the server. The server side access is read only.
* Do not assume file names are unique over time. The same name may reappear with different content.

## Stage 2: Dropbox listener (server)

### Behavior

1. **Watch for changes.** Use the Dropbox API long poll endpoint (`files/list_folder/longpoll`, timeout 480 seconds) with a cursor taken from `files_list_folder_get_latest_cursor` on the app folder root, recursive. Honor the `backoff` value the endpoint returns. If the cursor is reported as reset or invalid, rebuild it and run a full reconciliation. Use the official Dropbox Python software development kit (SDK).
2. **Startup order matters.** Take the cursor first, then run a full reconciliation, then enter the long poll loop. This closes the gap where a file could arrive between the two.
3. **Only two folders matter.** Ignore changes outside `/expenses` and `/bank`. Within those, consider only files directly inside the folder; ignore any subfolders and everything in them.
4. **Wait for the uploads to settle.** When a change is seen for a folder, take a snapshot: a sorted list of (file name, size, content hash) for the top level files. Repeat the snapshot every `SETTLE_POLL_SECONDS`. The folder is settled when `SETTLE_STABLE_POLLS` consecutive snapshots are identical. If it has not settled after `SETTLE_MAX_WAIT_MINUTES`, log a warning, stop for this cycle, and let the next change or the hourly reconciliation try again.
5. **Download a complete batch through staging.**
   * Work out which files in the settled snapshot are new (see the state store below).
   * Download each to `KOS_ROOT/.staging/<type>/<name>.partial`, where `<type>` is `expenses` or `bank`. The staging folder must be on the same filesystem as the imports folder and must not sit inside it.
   * Verify each download: size must match, and the Dropbox content hash must match if practical to compute.
   * Only when every file in the batch has downloaded and verified, rename them all into `KOS_ROOT/imports/<type>/` (a rename on one filesystem is atomic per file). Because of this, the import watcher in Stage 3 only ever sees finished files.
   * If any download fails, leave the batch in staging, log it, and retry on the next cycle. Never move a partial batch into imports.
   * Update the state store after the rename, not before.
6. **Name collisions.** If a file with the same name but a different content hash already exists in the imports folder (or has been recorded as imported before), keep both: save the new one as `<stem>__<first 8 characters of hash><extension>` and log a warning. Never overwrite.

### State store

Keep a SQLite database at `KOS_ROOT/state/intake.sqlite` recording, per imported file: Dropbox file identifier, Dropbox path, size, content hash, time imported, and final local name. The processing step may move finished files out of the imports folder into a `processed` subfolder; the state store is what stops a moved file from being downloaded again. Do not decide "already have it" by checking whether the file is sitting in the imports folder.

### Secrets and authorization

* The app key, app secret, and refresh token live only in `/etc/kosaccounts.env` (owner root, mode 600), loaded by systemd with `EnvironmentFile=`. They must never appear in a script, in the repository, or in logs. Add the env file pattern to `.gitignore` in case a copy ever lands in the repo.
* Request only the `files.metadata.read` and `files.content.read` permissions.
* Provide a one time helper script (`dropbox_auth_setup`) that walks Danny through the authorization flow with offline access and prints the refresh token for him to paste into the env file himself. Claude Code must not handle or store the secret values.

### Resilience

* Only one pull process may run at a time (use a lock in the state folder).
* Network errors and Dropbox errors are logged and retried with backoff. Do not exit on a transient error; if the process does exit, systemd restarts it.
* A reconciliation pass (list both folders, compare with the state store, run the same settle and staging logic) must exist as a callable mode of the same code. It runs at startup, after a cursor reset, and hourly from a timer.

## Stage 3: import watcher (server)

### Behavior

1. Run inotifywait (from the inotify tools package, `inotify-tools`) in monitor mode, not recursive, on `KOS_ROOT/imports/expenses` and `KOS_ROOT/imports/bank`. Listen for `close_write` and `moved_to` events. Ignore hidden files and names ending in `.partial`. Because it is not recursive, activity inside a `processed` subfolder is ignored.
2. This watcher is deliberately lazy. After the first event for a folder, wait until `INOTIFY_QUIET_SECONDS` pass with no further events for that folder, then invoke the processor once for that folder. Do not start one run per file.
3. Only one processor run at a time (use `flock`). If events arrive while a run is in progress, run again afterward once things are quiet.
4. On startup, sweep both folders once and process anything present that has not been handled. Events that occur while the watcher is down are lost, so this sweep is required.
5. Run the watcher as its own systemd service that restarts on failure.

### The processor hook

* Configuration holds two commands, `PROCESS_EXPENSES_CMD` and `PROCESS_BANK_CMD`, each called with the imports folder path as its argument.
* Read the existing Kosibah monthly accounts processing routines and the rest of this README to find the correct entry points and wire them in. If the entry points are unclear, ask Danny before guessing.
* The existing accounts workflow has review pauses between its stages. The watcher must start processing but must never bypass or auto approve a review step. Confirm with Danny exactly which stage the automatic trigger should run.
* Processing must be idempotent: running it twice over the same files must not duplicate results. The processor may move finished files into a `processed` subfolder.

## Configuration

Put all settings in one config file (for example `kosaccounts.conf`, kept out of any secrets handling). Defaults:

* `KOS_ROOT`: no default; confirm with Danny before creating anything
* `SETTLE_POLL_SECONDS`: 60
* `SETTLE_STABLE_POLLS`: 2
* `SETTLE_MAX_WAIT_MINUTES`: 60
* `INOTIFY_QUIET_SECONDS`: 120
* Reconciliation interval: hourly (systemd timer)

## systemd units

Create these, all with `After=network-online.target`, `Wants=network-online.target`, and `RequiresMountsFor=` pointing at `KOS_ROOT`:

* `kosaccounts_dropbox_pull.service`: the Stage 2 long poll listener. `Restart=always`, `RestartSec=10`, `EnvironmentFile=/etc/kosaccounts.env`.
* `kosaccounts_reconcile.service` and `kosaccounts_reconcile.timer`: runs one reconciliation pass hourly, `Persistent=true`, same env file. Uses the lock so it never overlaps the listener.
* `kosaccounts_import_watch.service`: the Stage 3 watcher. `Restart=always`.

Run them as a normal service user, not root; systemd reads the env file as root before dropping privileges. Logs go to the journal (`journalctl -u kosaccounts_dropbox_pull` and so on). Log each batch with folder, file count, total bytes, and file names.

Check whether the earlier hourly cron job that pulls from Dropbox with rclone is still installed. Once the new pipeline is verified, propose disabling it. Do not delete it without asking.

## Failure and recovery

* Server offline or service stopped: the startup reconciliation and hourly timer bring everything up to date.
* Batch interrupted mid download: partial files stay in staging and never reach imports; the next cycle finishes the batch.
* Mac offline or Stage 1 late: the server simply sees no change and waits.
* Dropbox cursor reset: rebuild the cursor and reconcile.
* Processor fails: the files remain in imports (or are not marked processed) so a rerun picks them up; the failure is logged.

## Acceptance tests

Claude Code should run or script these before calling the work done:

1. Add about 20 files to `Apps/kosaccounts/expenses` over a few minutes. Nothing appears in `imports/expenses` until the folder settles; then all files appear together and the processor runs once.
2. Kill the listener mid download. No partial file appears in imports; after restart the batch completes.
3. Move a file out of imports (simulating processing). It is not downloaded again.
4. Add a subfolder with files to the app folder. It is ignored.
5. Upload a file with an existing name but different content. Both are kept and a warning is logged.
6. Stop the services for a while, add files, restart. Everything is picked up.
7. Confirm no secret values appear in the repository, scripts, unit files, or logs.

## Questions to settle with Danny before building

* What should `KOS_ROOT` be, and which user account should the services run as?
* Which existing routines are the entry points for expenses and for bank statements, and which stage should the automatic trigger run given the review pauses?
* May the old rclone cron job be retired once the new pipeline is verified?

## Mac side (to be added)

Mac instructions for the Dropbox to Dropbox copy (launchd job, snapshot and buffering logic, top level files only) will be added as a separate section. The server must not depend on anything beyond the Stage 1 contract above.
