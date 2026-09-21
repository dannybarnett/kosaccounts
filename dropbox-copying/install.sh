#!/bin/bash
# Installs the Kosibah Dropbox to Dropbox copy as a launchd user agent.
# Safe to run again: it replaces the installed script and reloads the agent.
set -euo pipefail

LABEL="com.kosibah.dropbox_copy"
HERE="$(cd "$(dirname "$0")" && pwd)"
APP="$HOME/Library/Application Support/kosaccounts"
BIN="$APP/bin"
LOGS="$HOME/Library/Logs/kosaccounts"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
CONF="$APP/mac_copy.conf"

if [ "$(uname)" != "Darwin" ]; then
  echo "This installer is for macOS only." >&2
  exit 1
fi

# Find a Python 3.8 or newer interpreter.
PYTHON=""
for candidate in /opt/homebrew/bin/python3 /usr/local/bin/python3 /usr/bin/python3; do
  if [ -x "$candidate" ] && "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)' 2>/dev/null; then
    PYTHON="$candidate"
    break
  fi
done
if [ -z "$PYTHON" ]; then
  echo "No usable Python 3 found. Run 'xcode-select --install' or 'brew install python', then run this again." >&2
  exit 1
fi
echo "Using Python: $PYTHON"

mkdir -p "$BIN" "$LOGS" "$HOME/Library/LaunchAgents"
cp "$HERE/mac_copy.py" "$BIN/mac_copy.py"
chmod 755 "$BIN/mac_copy.py"

if [ ! -f "$CONF" ]; then
  cat > "$CONF" << 'CONFEOF'
[main]
# Leave dropbox_root empty to auto detect (~/Dropbox or ~/Library/CloudStorage/Dropbox*).
dropbox_root =
# How often the two source folders are checked, in seconds.
poll_seconds = 60
# A folder counts as settled after this many consecutive checks with no change.
settle_stable_polls = 2
# Log a warning if a folder has not settled after this long (it keeps waiting).
settle_max_wait_minutes = 60
CONFEOF
  echo "Wrote default config: $CONF"
fi

cat > "$PLIST" << PLISTEOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$PYTHON</string>
    <string>$BIN/mac_copy.py</string>
    <string>--config</string>
    <string>$CONF</string>
  </array>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>ThrottleInterval</key>
  <integer>30</integer>
  <key>ProcessType</key>
  <string>Background</string>
  <key>StandardOutPath</key>
  <string>$LOGS/launchd_stdout.log</string>
  <key>StandardErrorPath</key>
  <string>$LOGS/launchd_stderr.log</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key>
    <string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
  </dict>
</dict>
</plist>
PLISTEOF
plutil -lint "$PLIST" >/dev/null

UID_NUM="$(id -u)"
launchctl bootout "gui/$UID_NUM/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$UID_NUM" "$PLIST"
launchctl enable "gui/$UID_NUM/$LABEL"
launchctl kickstart -k "gui/$UID_NUM/$LABEL"

echo
echo "Installed and started: $LABEL"
echo "Check status : launchctl print gui/$UID_NUM/$LABEL | head -20"
echo "Watch the log: tail -f \"$LOGS/mac_copy.log\""
echo "Restart      : launchctl kickstart -k gui/$UID_NUM/$LABEL"
echo
echo "Note: the first run copies every eligible file currently in the two source folders."
