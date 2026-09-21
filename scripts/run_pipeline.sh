#!/bin/bash
# Processing run: python -m kosaccounts run. Extra args are passed through
# (e.g. scripts/run_pipeline.sh --dry-run). Invoked by the import watcher after a quiet period,
# by the timer/cron fallback, and safe to run by hand.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(dirname "$SCRIPT_DIR")"
# KOSACCOUNTS_LOGS_DIR lets tests point logs at a tmp directory without touching the repo's own
# logs/; unset in normal (systemd/manual) use, where it's always $REPO/logs.
LOGS="${KOSACCOUNTS_LOGS_DIR:-$REPO/logs}"
mkdir -p "$LOGS"

# cron gives a minimal environment; the pipeline shells out to the claude CLI which needs both.
export HOME="${HOME:-/home/dannybarnett}"
export PATH="$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin"

# KOSACCOUNTS_LOCK_HELD=1 means the caller (kosaccounts intake watch / Watcher.process_once)
# already holds this same lock file via Python's fcntl.flock before invoking this script. flock
# is scoped to an open file description, not to a path or a process: a child process opening
# and flocking the same path gets a NEW file description, which conflicts with the parent's
# already-held lock. Without this guard the script would always print "already running" and
# exit 0 without processing anything -- a deadlock between the watcher and this script. Manual
# runs (cron/timer/by hand) never set this variable, so they still take the lock as before.
if [ "${KOSACCOUNTS_LOCK_HELD:-}" != "1" ]; then
  # Hold the lock for the whole script: exec keeps fd 9 open until the process exits.
  exec 9>"$LOGS/pipeline.lock"
  if ! flock -n 9; then
    echo "$(date +'%Y-%m-%d %H:%M:%S') run_pipeline already running (lock held)" >> "$LOGS/pipeline.lock.log"
    exit 0
  fi
fi

cd "$REPO"
LOG_FILE="$LOGS/pipeline-$(date +%Y-%m-%d).log"
EXIT_CODE=0
.venv/bin/python -m kosaccounts run "$@" >> "$LOG_FILE" 2>&1 || EXIT_CODE=$?

if [ "$EXIT_CODE" -ne 0 ]; then
  echo "$(date +'%Y-%m-%d %H:%M:%S') pipeline failed with exit code $EXIT_CODE" >> "$LOGS/pipeline-errors.log"
  exit "$EXIT_CODE"
fi
