#!/bin/bash
# Hourly processing run: python -m kosaccounts run. Extra args are passed through
# (e.g. scripts/run_pipeline.sh --dry-run). Runs under cron; safe to run by hand.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(dirname "$SCRIPT_DIR")"
LOGS="$REPO/logs"
mkdir -p "$LOGS"

# cron gives a minimal environment; the pipeline shells out to the claude CLI which needs both.
export HOME="${HOME:-/home/dannybarnett}"
export PATH="$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin"

# Hold the lock for the whole script: exec keeps fd 9 open until the process exits.
exec 9>"$LOGS/pipeline.lock"
if ! flock -n 9; then
  echo "$(date +'%Y-%m-%d %H:%M:%S') run_pipeline already running (lock held)" >> "$LOGS/pipeline.lock.log"
  exit 0
fi

cd "$REPO"
LOG_FILE="$LOGS/pipeline-$(date +%Y-%m-%d).log"
EXIT_CODE=0
.venv/bin/python -m kosaccounts run "$@" >> "$LOG_FILE" 2>&1 || EXIT_CODE=$?

if [ "$EXIT_CODE" -ne 0 ]; then
  echo "$(date +'%Y-%m-%d %H:%M:%S') pipeline failed with exit code $EXIT_CODE" >> "$LOGS/pipeline-errors.log"
  exit "$EXIT_CODE"
fi
