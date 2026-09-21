#!/bin/bash
# Prints (default) or runs (--apply) the commands that install and start the three kosaccounts
# systemd units: kosaccounts_dropbox_pull.service, kosaccounts_import_watch.service, and
# kosaccounts_reconcile.service/.timer. There is no passwordless sudo on this box, so by default
# this script only PRINTS the exact sudo commands for Danny to run by hand; pass --apply to run
# them (they will prompt for a sudo password as needed). Safe to re-run: every step is idempotent.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(dirname "$SCRIPT_DIR")"
KOS_ROOT="/mnt/storage/docs/kosaccounts"

MODE="print"
if [ "${1-}" = "--apply" ]; then
  MODE="apply"
elif [ "${1-}" = "--print-only" ] || [ -z "${1-}" ]; then
  MODE="print"
elif [ -n "${1-}" ]; then
  echo "Usage: $0 [--print-only|--apply]" >&2
  exit 1
fi

run_or_print() {
  # $1: a human-readable label; remaining args: the command
  local label="$1"
  shift
  echo ""
  echo "# $label"
  printf '%q ' "$@"
  echo ""
  if [ "$MODE" = "apply" ]; then
    "$@"
  fi
}

echo "=== kosaccounts systemd install ($MODE mode) ==="

run_or_print "Create data directories under KOS_ROOT" \
  sudo mkdir -p \
    "$KOS_ROOT/.staging" \
    "$KOS_ROOT/state" \
    "$KOS_ROOT/imports/expenses" \
    "$KOS_ROOT/imports/bank"

run_or_print "Own those directories as dannybarnett (not all of /mnt/storage)" \
  sudo chown -R dannybarnett:dannybarnett \
    "$KOS_ROOT/.staging" \
    "$KOS_ROOT/state" \
    "$KOS_ROOT/imports/expenses" \
    "$KOS_ROOT/imports/bank"

echo ""
echo "# Install the secrets file, only if /etc/kosaccounts.env does not already exist"
echo "if [ ! -e /etc/kosaccounts.env ]; then"
echo "  sudo install -o root -g root -m 600 $REPO/systemd/kosaccounts.env.example /etc/kosaccounts.env"
echo "fi"
if [ "$MODE" = "apply" ]; then
  if [ ! -e /etc/kosaccounts.env ]; then
    sudo install -o root -g root -m 600 "$REPO/systemd/kosaccounts.env.example" /etc/kosaccounts.env
  else
    echo "  (skipped: /etc/kosaccounts.env already exists)"
  fi
fi

run_or_print "Edit the secrets file: paste in the values printed by 'intake auth-setup'" \
  sudo "${EDITOR:-nano}" /etc/kosaccounts.env

run_or_print "Install the unit files" \
  sudo install -o root -g root -m 644 \
    "$REPO"/systemd/*.service "$REPO"/systemd/*.timer \
    /etc/systemd/system/

run_or_print "Reload systemd" \
  sudo systemctl daemon-reload

run_or_print "Enable and start the long-running services and the reconcile timer" \
  sudo systemctl enable --now \
    kosaccounts_dropbox_pull.service \
    kosaccounts_import_watch.service \
    kosaccounts_reconcile.timer

echo ""
echo "# Optional: allow dannybarnett to start/stop/restart/status these units without a"
echo "# password (see systemd/kosaccounts-sudoers.example for the exact rules):"
echo "sudo visudo -f /etc/sudoers.d/kosaccounts"
echo "# (this step is never run automatically, even with --apply -- paste the file's contents"
echo "# into the editor visudo opens)"

echo ""
echo "=== Verification ==="
echo ""
echo "sudo systemctl status kosaccounts_dropbox_pull.service"
echo "sudo systemctl status kosaccounts_import_watch.service"
echo "sudo systemctl status kosaccounts_reconcile.service"
echo "systemctl list-timers 'kosaccounts*'"
echo ""
echo "journalctl -u kosaccounts_dropbox_pull -f"
echo "journalctl -u kosaccounts_import_watch -f"
echo ""

if [ "$MODE" = "print" ]; then
  echo "This was a dry run (--print-only, the default). Re-run with --apply to execute these"
  echo "commands (you will be prompted for your sudo password as needed)."
fi
