# Acceptance checklist: server-side Dropbox intake

The 7 tests from `specs/README_server_intake.md` ("Acceptance tests"). Tests 1-6 need a live
Dropbox app folder with real uploads (via Stage 1 on the Mac, or files dropped directly into
`Apps/kosaccounts/expenses` or `Apps/kosaccounts/bank` in the Dropbox web UI) and the systemd
units installed and running (see `scripts/README-dropbox.md`, `scripts/install_systemd.sh`).
Test 7 is fully scripted and needs neither.

Run these once `/etc/kosaccounts.env` exists and
`kosaccounts_dropbox_pull.service`, `kosaccounts_import_watch.service`, and
`kosaccounts_reconcile.timer` are enabled and running.

## Timing to expect

Nothing here is time-critical; the pipeline trades speed for never processing a partial batch.
From the config defaults (`config.toml` `[intake]`):

- **Settle** (Stage 2, Dropbox listener): after the last change in a Dropbox folder, the
  listener needs `settle_poll_seconds` x `settle_stable_polls` = 60 x 2 = **~2 minutes** of no
  further change before it downloads the batch into `KOS_ROOT/imports/<folder>`.
- **Quiet** (Stage 3, import watcher): after the last file lands in
  `KOS_ROOT/imports/<folder>`, the watcher waits `inotify_quiet_seconds` = **2 minutes** of no
  further arrivals before it runs the processor.
- **Total**: roughly **4-5 minutes** from the last file landing in Dropbox to the processor
  starting. Give tests below at least that long before concluding something didn't happen.

## Useful commands

Watch both services while testing (run in separate terminals):

```bash
journalctl -u kosaccounts_dropbox_pull -f
journalctl -u kosaccounts_import_watch -f
```

List what has landed on disk:

```bash
ls -la /mnt/storage/docs/kosaccounts/imports/expenses
ls -la /mnt/storage/docs/kosaccounts/imports/bank
ls -la /mnt/storage/docs/kosaccounts/.staging   # should be empty except mid-batch
```

Query the intake state store (records every file the listener has downloaded, keyed so a moved
or deleted local file is never re-downloaded). `sqlite3` may not be installed on this box; both
options are given:

```bash
sqlite3 /mnt/storage/docs/kosaccounts/state/intake.sqlite \
  'select folder,name,local_name,imported_at from imported order by id desc limit 20'
```

```bash
.venv/bin/python -c "
import sqlite3
con = sqlite3.connect('/mnt/storage/docs/kosaccounts/state/intake.sqlite')
for row in con.execute(
    'select folder,name,local_name,imported_at from imported order by id desc limit 20'
):
    print(row)
"
```

---

## Test 1: a batch of ~20 files settles before anything appears

**Do:** add about 20 files to `Apps/kosaccounts/expenses` in Dropbox over a few minutes (e.g.
drag them into the Dropbox web UI a few at a time, spread over 2-3 minutes, to simulate a real
multi-minute upload).

**Look for:**
- `journalctl -u kosaccounts_dropbox_pull -f` shows no batch/import activity while uploads are
  still trickling in.
- Nothing appears in `KOS_ROOT/imports/expenses` until roughly 2 minutes after the *last* file
  was added.
- Then all ~20 files appear together in `KOS_ROOT/imports/expenses`, and the journal logs one
  batch line (folder, file count, total bytes, file names).
- `journalctl -u kosaccounts_import_watch -f` shows the processor invoked once, roughly 2
  minutes after that (quiet period), not once per file.
- Confirm in the state store: `select count(*) from imported where folder='expenses'` grew by
  ~20.

## Test 2: killed mid-download leaves no partial file in imports

**Do:** start a batch upload (a handful of files), and once the listener has started
downloading (watch `KOS_ROOT/.staging/<folder>/*.partial` appear), kill the listener:
`sudo systemctl stop kosaccounts_dropbox_pull.service` (or `sudo pkill -f "kosaccounts intake
listen"`), then restart it: `sudo systemctl start kosaccounts_dropbox_pull.service` (or just
wait if `Restart=always` already brought it back).

**Look for:**
- No `.partial` file, and no finished file, appears under `KOS_ROOT/imports/<folder>` while the
  listener is down.
- After restart, the journal shows the batch completing (staging finishes, files renamed into
  `imports/`).
- `KOS_ROOT/.staging/<folder>` is empty again afterwards.

## Test 3: a file moved out of imports is not re-downloaded

**Do:** after a file has been imported (confirm via the state store or by seeing it in
`KOS_ROOT/imports/expenses`), move it out — e.g.
`mv KOS_ROOT/imports/expenses/<name> /tmp/` (simulating the processor's own "move to processed"
behaviour) — then trigger a reconciliation: wait for the hourly timer, or run
`sudo systemctl start kosaccounts_reconcile.service` by hand.

**Look for:**
- The file does **not** reappear in `KOS_ROOT/imports/expenses`.
- The journal for the reconcile run shows it was skipped (already in the state store by
  `folder, name, content_hash`), not re-downloaded.

## Test 4: a subfolder in the Dropbox app folder is ignored

**Do:** in Dropbox, create a subfolder inside `Apps/kosaccounts/expenses` (e.g. `old/`) and add
a file inside it.

**Look for:**
- Nothing about that subfolder or its file appears in the `kosaccounts_dropbox_pull` journal
  (no settle activity, no batch).
- Nothing appears in `KOS_ROOT/imports/expenses` from it, ever.

## Test 5: same name, different content -> both kept, warning logged

**Do:** upload a file to `Apps/kosaccounts/expenses` with the same name as one already imported,
but different content (e.g. re-save the same-named file with an extra page).

**Look for:**
- After settling, both files land in `KOS_ROOT/imports/expenses`: the original name, and a
  second file named `<stem>__<8-char-hash><ext>`.
- A warning is logged in `journalctl -u kosaccounts_dropbox_pull` naming the collision.
- Neither the original file nor its state-store row is overwritten.

## Test 6: services stopped for a while, files added, then restarted

**Do:** `sudo systemctl stop kosaccounts_dropbox_pull.service kosaccounts_import_watch.service`.
While stopped, add a few files to `Apps/kosaccounts/expenses` and/or `Apps/kosaccounts/bank` in
Dropbox. Wait a few minutes, then
`sudo systemctl start kosaccounts_dropbox_pull.service kosaccounts_import_watch.service`.

**Look for:**
- On startup, `kosaccounts_dropbox_pull` runs a full reconciliation pass before entering its
  long-poll loop (journal shows a reconcile/batch line even though nothing was listening while
  it was down).
- The files added while stopped appear in `KOS_ROOT/imports/<folder>` after the normal settle
  delay from startup.
- On startup, `kosaccounts_import_watch` sweeps both import folders once (journal shows a run
  even if nothing changed while it was down) and processes anything unhandled.

## Test 7: no secret values anywhere (scripted)

**Do:**

```bash
scripts/check_no_secrets.sh
```

**Look for:** exit code 0 and the line `no secrets found`. The script greps the repository
(tracked and untracked files, excluding `.venv`, `.git`, `imports`, `output`, `logs`, `data`,
`specs`), every file under `systemd/`, and — if `journalctl` is accessible — the last 5000 lines
from `kosaccounts_dropbox_pull`, `kosaccounts_reconcile`, and `kosaccounts_import_watch`, for
`DROPBOX_APP_KEY`, `DROPBOX_APP_SECRET`, or `DROPBOX_REFRESH_TOKEN` followed by `=` and a real value, and
for Dropbox token-shaped strings (`sl.` followed by 20+ characters). It exits 1 and prints the
offending lines if it finds anything; it skips the journal check gracefully (with a note, not a
failure) if `journalctl` is missing or access is denied. This test can be run any time, with or
without the units installed.

---

After all 7 pass, propose to Danny deleting `scripts/rclone_import.sh`, `scripts/crontab.txt`,
and the rclone Dropbox remote configuration (`scripts/README-dropbox.md` "Legacy" section).
