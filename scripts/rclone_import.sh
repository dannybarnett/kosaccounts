#!/bin/bash
# Hourly Dropbox -> imports/ copy. One-way, never deletes or modifies the source.
# Runs under cron (see scripts/crontab.txt). Safe to run by hand.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(dirname "$SCRIPT_DIR")"
LOGS="$REPO/logs"
mkdir -p "$LOGS"

# Hold the lock for the whole script: exec keeps fd 9 open until the process exits.
exec 9>"$LOGS/rclone.lock"
if ! flock -n 9; then
  echo "$(date +'%Y-%m-%d %H:%M:%S') rclone_import already running (lock held)" >> "$LOGS/rclone.lock.log"
  exit 0
fi

# Path inside the remote that holds bank/, receipts/, invoices/. The kosaccounts app is an
# "App folder" app, so the remote root IS /Apps/kosaccounts and this stays empty. (Only a
# "Full Dropbox" app would need a folder path here.) Override with the DROPBOX_SOURCE env var.
DROPBOX_SOURCE="${DROPBOX_SOURCE-}"

LOG_FILE="$LOGS/rclone-$(date +%Y-%m-%d).log"
EXIT_CODE=0
rclone copy "kosaccounts:$DROPBOX_SOURCE" "$REPO/imports/" \
  --checksum \
  --exclude ".converted/**" \
  --exclude "*.tmp" \
  --log-file "$LOG_FILE" \
  --log-level INFO \
  --stats-one-line \
  --stats 0 || EXIT_CODE=$?

if [ "$EXIT_CODE" -ne 0 ]; then
  echo "$(date +'%Y-%m-%d %H:%M:%S') rclone copy failed with exit code $EXIT_CODE" >> "$LOGS/rclone-errors.log"
  exit "$EXIT_CODE"
fi
