# Dropbox setup for the kosaccounts pipeline

Goal: files dropped into Dropbox by Danny or his husband are copied to `imports/` on the server
every hour, and a compromise of the server can never reach anything outside one Dropbox folder.

Design:

- The Dropbox app **kosaccounts** is a *Scoped access, App folder* app. Its token can only see
  `/Apps/kosaccounts/` in Danny's Dropbox, nothing else.
- Danny adds files directly to `/Apps/kosaccounts/{bank,receipts,invoices}`.
- Anyone else uploads through Dropbox **File Requests** (permanent links pointing at those
  subfolders). Uploaders need no access to the folder and never see its contents.
- `rclone copy` on the server is one-way: it never deletes or changes anything in Dropbox.

## 1. Dropbox app (https://www.dropbox.com/developers/apps)

1. Create app → **Scoped access** → **App folder** → name **kosaccounts**.
   Dropbox creates `/Apps/kosaccounts/` on first use.
2. **Permissions** tab, tick and Submit (do this *before* authorising; scopes are baked into the
   token):
   - `account_info.read`
   - `files.metadata.read`
   - `files.metadata.write`
   - `files.content.read`
   - `files.content.write`

   rclone requires the write scopes to authorise at all, even though this pipeline only reads.
   With an App-folder app they apply only inside `/Apps/kosaccounts/`.
3. **Settings** tab: OAuth 2 Redirect URIs → add `http://localhost:53682/`.
   Leave the app in Development status (fine for personal use, never needs review).
   The console's "Generate access token" button is not needed by rclone (it obtains its own
   long-lived refresh token in step 3). It is harmless to press it once to make Dropbox create
   `/Apps/kosaccounts/` so you can add the subfolders; the token expires after about four hours
   and should never be copied to the server.
4. Note the **App key** and **App secret**.

## 2. Folders and File Requests (Dropbox web)

1. In `/Apps/kosaccounts/` create `bank`, `receipts`, `invoices`.
2. Create a File Request for each folder (Dropbox → File requests → New request → choose the
   folder → "no deadline"). Share the receipts and bank links with anyone who uploads.

Notes on File Request uploads:

- Uploads land directly in the chosen folder and are picked up on the next hourly import.
- Dropbox may add the uploader's name in parentheses to a filename, e.g.
  `2026-09-12 - Mood (Yemi).pdf`. The pipeline ignores a trailing parenthesised group when it
  reads the date and supplier hint from the name, and it identifies files by content hash, so
  renames never cause duplicates.
- Keep the `YYYY-MM-DD - Supplier` naming convention where possible; it helps the receipt reader
  but is not required.

## 3. Authorise on a MacBook (the server has no browser)

```bash
brew install rclone
rclone authorize "dropbox" "<app_key>" "<app_secret>"
```

Approve the app in the browser that opens (it asks for access to the kosaccounts app folder
only). rclone prints a JSON token block (`{"access_token": ..., "refresh_token": ...,
"expiry": ...}`). Copy the whole block including the braces.

## 4. Configure rclone on the server

```bash
sudo apt install rclone      # once
rclone config
```

| Prompt | Answer |
|---|---|
| n) New remote | `n` |
| name | `kosaccounts` |
| Storage | `dropbox` |
| client_id | your App key |
| client_secret | your App secret |
| Edit advanced config? | `n` |
| Use auto config? ("Say Y if not sure / N if headless") | `n` (the server has no browser) |
| config_token | paste the JSON block from step 3 (rclone repeats the `rclone authorize` command to run on the MacBook just above this prompt) |
| Keep this remote? | `y` |

The token is stored in `~/.config/rclone/rclone.conf` (owner-only permissions, keep it that way).
Even if it leaked, it can only reach `/Apps/kosaccounts/`.

## 5. Verify

```bash
rclone lsd kosaccounts:
```

should list `bank`, `invoices`, `receipts` (the app folder is the remote root). Then run the
first import by hand and check:

```bash
scripts/rclone_import.sh
tail logs/rclone-$(date +%Y-%m-%d).log
ls -R imports/
```

## 6. Install the hourly schedule

```bash
crontab -l                                   # check nothing else is scheduled first
crontab /home/dannybarnett/claude-coding/kosaccounts/scripts/crontab.txt
```

rclone runs at :00, the pipeline at :15.

## Troubleshooting

- **invalid_grant / token expired**: re-run the `rclone authorize` step on the MacBook and paste
  the new token via `rclone config` → edit remote `kosaccounts`.
- **insufficient_scope / missing_scope**: the app's permissions were changed after authorising.
  Re-authorise.
- **`rclone lsd kosaccounts:` shows nothing**: the app folder is empty or was never created;
  add a file to `/Apps/kosaccounts/receipts` in Dropbox and retry.
- **rclone: command not found** under cron: install with apt so it lives in `/usr/bin`.
- Failed runs append a line to `logs/rclone-errors.log`; successful runs log to
  `logs/rclone-YYYY-MM-DD.log`.

## If Full Dropbox access were ever wanted instead

Not recommended (the token would see the whole account), but supported: create a *Full Dropbox*
app, share an ordinary folder with edit rights, and set `DROPBOX_SOURCE="<folder name>"` in
`scripts/rclone_import.sh`. Everything else is identical.
