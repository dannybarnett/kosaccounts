#!/bin/bash
# Stops and removes the launchd agent. Use --purge to also remove the
# installed script, config, state, staging files, and logs.
# Nothing in Dropbox is touched either way.
set -euo pipefail

LABEL="com.kosibah.dropbox_copy"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
UID_NUM="$(id -u)"

launchctl bootout "gui/$UID_NUM/$LABEL" 2>/dev/null || true
rm -f "$PLIST"
echo "Agent removed."

if [ "${1:-}" = "--purge" ]; then
  rm -rf "$HOME/Library/Application Support/kosaccounts" "$HOME/Library/Logs/kosaccounts"
  echo "Script, config, state, and logs removed."
else
  echo "Config, state, and logs kept. Run with --purge to remove them."
fi
