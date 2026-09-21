# Kosibah Dropbox to Dropbox copy: Mac install

This is the Stage 1 job. It copies top level files from two Dropbox folders into the `kosaccounts` app folder, where the Ubuntu server picks them up.

* `Kosibah expenses` goes to `Apps/kosaccounts/expenses`
* `KOSIBAH LLC BANK STATEMENTS` goes to `Apps/kosaccounts/bank`

## Install

1. Put the four files from this folder together in one folder on the Mac.
2. Open Terminal in that folder and run `./install.sh`. If macOS refuses to run it, run `chmod +x install.sh uninstall.sh` first.
3. Check that it is running: `launchctl print gui/$(id -u)/com.kosibah.dropbox_copy | head -20`
4. Watch it work: `tail -f ~/Library/Logs/kosaccounts/mac_copy.log`

The first run copies every eligible file already sitting in the two source folders. Files already present at the destination with identical contents are skipped.

## How it behaves

* Checks both source folders every 60 seconds. When something is new or changed, it waits until two consecutive checks show no further change, then copies the whole batch.
* Copies go to a staging folder outside Dropbox, are verified by SHA256 (Secure Hash Algorithm, 256 bit) hash, and are then moved into the app folder together.
* Top level files only. Subfolders are ignored. Hidden files, `.DS_Store`, `~$` lock files, and `.tmp`, `.part`, and `.crdownload` files are ignored.
* Nothing is ever deleted, moved, or modified in the source folders, and nothing is deleted in the app folder.
* An edited file with the same name is sent again; an unchanged one is not.
* Files that are online only placeholders are not forced to download; the batch waits and retries.
* It runs as a launchd user agent: starts at login, restarts if it dies (30 second minimum gap). It only runs while you are logged in and the Mac is awake; it catches up on its next check after waking.

## Settings

Edit `~/Library/Application Support/kosaccounts/mac_copy.conf`, then restart with `launchctl kickstart -k gui/$(id -u)/com.kosibah.dropbox_copy`. Leave `dropbox_root` empty to auto detect the Dropbox folder.

## Run a copy now

`python3 ~/Library/Application\ Support/kosaccounts/bin/mac_copy.py --once` runs a single pass immediately without waiting for the folders to settle. Only do this when you know the source folders are not still filling.

## If nothing is copied

* Look in `~/Library/Logs/kosaccounts/mac_copy.log` and `launchd_stderr.log`.
* A permission error reading the Dropbox folders means macOS is blocking the background job. Open System Settings, Privacy and Security, Full Disk Access, add the Python binary named on the first line of the install output, then restart the job.
* "app folder missing" means `Apps/kosaccounts` does not exist in Dropbox yet. The job will not create it.
* "online only placeholders" means some files in the source are not downloaded to the Mac. Set the folder to available offline in Dropbox.

## Uninstall

`./uninstall.sh` removes the agent and keeps config, state, and logs. `./uninstall.sh --purge` removes those too. Dropbox is never touched.
