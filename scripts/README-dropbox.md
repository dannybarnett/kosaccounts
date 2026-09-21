# Dropbox setup for the kosaccounts pipeline

Goal: expense documents and bank statements dropped into two shared Dropbox folders are copied,
on a Mac, into the `kosaccounts` app folder; the server watches that app folder through the
Dropbox API, pulls new files down, and triggers processing. A compromise of the server can never
reach anything outside one Dropbox folder, and the server's token is read-only.

## What the app is

- The Dropbox app **kosaccounts** is a *Scoped access, App folder* app. Its token can only see
  `/Apps/kosaccounts/` in Danny's Dropbox, nothing else.
- It holds two subfolders, `expenses` and `bank`, matching `KOS_ROOT/imports/expenses` and
  `KOS_ROOT/imports/bank` on the server.
- The server's access is **read only**: `files.metadata.read` and `files.content.read`. It never
  writes to, moves, or deletes anything in Dropbox.

This replaces the old rclone + File Requests setup, which needed write scopes and ran from an
hourly cron job. See "Legacy" at the bottom.

## 1. Permissions tab changes (do this before authorising)

In the Dropbox App Console (https://www.dropbox.com/developers/apps) for the **kosaccounts**
app, open the **Permissions** tab:

- Tick `files.metadata.read`
- Tick `files.content.read`
- Untick any write scopes left over from rclone (`files.metadata.write`,
  `files.content.write`) — the new listener never needs them
- Click **Submit**

Scopes are baked into the token at the moment it is issued, so do this before running
`auth-setup` below. If the scopes change later, re-run `auth-setup` to get a token with the new
scopes.

## 2. Authorise

Run the one-time helper on the server (it never writes anything to disk and never logs the
values it handles):

```bash
.venv/bin/python -m kosaccounts intake auth-setup
```

It asks for the app key and secret (from the App Console's **Settings** tab; the secret is typed
without echoing), then prints an authorization URL. Paste that URL into any browser — the Mac is
fine, the server doesn't need one — approve the app, and paste the code it shows back into the
prompt. The tool then prints a refresh token **once**. Copy it immediately; it is not shown
again and not stored anywhere by this tool.

## 3. Install the secrets file

The app key, app secret, and refresh token live only in `/etc/kosaccounts.env` (owner root, mode
600), loaded by systemd via `EnvironmentFile=`. They must never appear in a script, in this
repository, or in logs — `.gitignore` covers `*.env` and `kosaccounts.env` in case a copy ever
lands in a working tree by accident, but the real protection is that the file only ever exists
under `/etc`.

```bash
sudo install -o root -g root -m 600 systemd/kosaccounts.env.example /etc/kosaccounts.env
sudo $EDITOR /etc/kosaccounts.env
```

Fill in the three variables the file already lists (`DROPBOX_APP_KEY`, `DROPBOX_APP_SECRET`,
`DROPBOX_REFRESH_TOKEN`) with the values `auth-setup` printed in step 2, one `NAME=value` line
each, no quotes, no spaces around the `=`. Save and exit; the file must stay mode 600, owner
root.

## 4. Install and start the services

```bash
scripts/install_systemd.sh            # prints the sudo commands (default; nothing is run)
scripts/install_systemd.sh --apply    # or runs them directly, prompting for sudo as needed
```

This creates the data directories under `KOS_ROOT`, installs the env file (if not already
present), installs the three systemd units, reloads systemd, and enables/starts
`kosaccounts_dropbox_pull.service`, `kosaccounts_import_watch.service`, and
`kosaccounts_reconcile.timer`. See `docs/ACCEPTANCE.md` for the full acceptance checklist.

## 5. Verify

```bash
sudo systemctl status kosaccounts_dropbox_pull.service
sudo systemctl status kosaccounts_import_watch.service
systemctl list-timers 'kosaccounts*'

journalctl -u kosaccounts_dropbox_pull -f
journalctl -u kosaccounts_import_watch -f
```

Expect, in order, on first start: a startup reconciliation pass (folder listing, "batch"
summary lines even if the batch is empty), then long-poll waits. When files land in Dropbox,
expect a settle delay (`settle_poll_seconds` x `settle_stable_polls`, default ~2 minutes) before
they appear in `KOS_ROOT/imports/<folder>`, then a further quiet delay
(`inotify_quiet_seconds`, default 2 minutes) before the watcher runs the processor — roughly
4 minutes from the last upload to processing starting. Nothing here is time critical; this
trades speed for never processing a partial batch.

## Troubleshooting

- **`AuthError` in the journal** (expired or revoked token): re-run
  `python -m kosaccounts intake auth-setup` and update `/etc/kosaccounts.env`, then
  `sudo systemctl restart kosaccounts_dropbox_pull.service`.
- **Cursor reset logged as a warning**: normal and self-healing — the listener rebuilds the
  cursor and runs a full reconciliation. No action needed unless it happens repeatedly.
- **`missing_scope` error**: the app's permissions were changed after the token was issued.
  Redo step 1 (Permissions tab + Submit), then step 2 (re-authorise).
- **Units sit inactive / `RequiresMountsFor` not satisfied**: `KOS_ROOT`
  (`/mnt/storage/docs/kosaccounts`) is not mounted yet. The units wait for the mount rather than
  failing; check `mount` and `systemctl status <unit>` for the reason.
- **Nothing appears after an upload**: check the settle/quiet timing above before assuming
  something is wrong: allow at least 4-5 minutes.

## Legacy: rclone + cron

The original hourly rclone-based import (a `rclone copy` under cron) was retired on 2026-09-21
once the services above passed acceptance. Its scripts and the `kosaccounts` rclone remote have
been deleted. Do not reinstall it; the listener replaces it entirely.
