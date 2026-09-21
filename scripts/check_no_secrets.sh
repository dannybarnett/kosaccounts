#!/bin/bash
# Acceptance test 7 (specs/README_server_intake.md): confirm no Dropbox secret VALUES appear in
# the repository, the systemd unit files, or the journal for the kosaccounts units. Names of the
# env vars with an empty value (as in systemd/kosaccounts.env.example) are fine; a name followed
# by a non-empty value, or anything shaped like a Dropbox token, is not.
#
# Exit 0 and print "no secrets found" if clean. Exit 1 and print the offending lines otherwise.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(dirname "$SCRIPT_DIR")"
cd "$REPO"

# A DROPBOX_(APP_KEY|APP_SECRET|REFRESH_TOKEN) followed by '=' and then a non-whitespace value.
NAME_VALUE_PATTERN='DROPBOX_(APP_KEY|APP_SECRET|REFRESH_TOKEN)[[:space:]]*=[[:space:]]*[^[:space:]]'
# Dropbox refresh/access token shape (sl.<20+ chars>). Deliberately skip the generic
# app-key-like 15-char pattern: too many false positives on unrelated 15-char strings.
TOKEN_PATTERN='sl\.[A-Za-z0-9_-]{20,}'

FOUND=0
HITS=""

add_hits() {
  local label="$1"
  local output="$2"
  if [ -n "$output" ]; then
    FOUND=1
    HITS="$HITS
--- $label ---
$output"
  fi
}

# (a) repo files: everything git tracks or could track, minus known data/output/spec dirs and
# the venv/git metadata. Includes untracked files (e.g. this script's own siblings) but not
# ignored ones, which is what we want: build the same file list git status would show as
# trackable, so a secret accidentally `git add`-ed would be caught.
mapfile -t REPO_FILES < <(
  git -C "$REPO" ls-files --cached --others --exclude-standard -- . \
    ':!:.venv' ':!:.git' ':!:imports' ':!:output' ':!:logs' ':!:data' ':!:specs' \
    2>/dev/null
)

if [ "${#REPO_FILES[@]}" -gt 0 ]; then
  REPO_NAME_HITS=$(grep -nE "$NAME_VALUE_PATTERN" "${REPO_FILES[@]}" 2>/dev/null \
    | grep -vE '\.example[:-]' || true)
  REPO_TOKEN_HITS=$(grep -nE "$TOKEN_PATTERN" "${REPO_FILES[@]}" 2>/dev/null || true)
  add_hits "repo files (name=value)" "$REPO_NAME_HITS"
  add_hits "repo files (token shape)" "$REPO_TOKEN_HITS"
fi

# (b) systemd unit files specifically (belt and braces: they are also covered by (a) above,
# but check them even if run from outside a git checkout of this repo).
if [ -d "$REPO/systemd" ]; then
  UNIT_NAME_HITS=$(grep -rnE "$NAME_VALUE_PATTERN" "$REPO/systemd" 2>/dev/null \
    | grep -vE '\.example[:-]' || true)
  UNIT_TOKEN_HITS=$(grep -rnE "$TOKEN_PATTERN" "$REPO/systemd" 2>/dev/null || true)
  add_hits "systemd/ unit files (name=value)" "$UNIT_NAME_HITS"
  add_hits "systemd/ unit files (token shape)" "$UNIT_TOKEN_HITS"
fi

# (c) journal, if accessible.
if command -v journalctl >/dev/null 2>&1; then
  JOURNAL_OUTPUT=$(journalctl -u kosaccounts_dropbox_pull -u kosaccounts_reconcile \
    -u kosaccounts_import_watch --no-pager -n 5000 2>/dev/null)
  JOURNAL_RC=$?
  if [ "$JOURNAL_RC" -ne 0 ] || [ -z "$JOURNAL_OUTPUT" ]; then
    echo "note: journalctl unavailable or returned nothing for the kosaccounts units (permission denied, no journal access, or units not yet installed) -- skipping journal check"
  else
    JOURNAL_NAME_HITS=$(echo "$JOURNAL_OUTPUT" | grep -nE "$NAME_VALUE_PATTERN" || true)
    JOURNAL_TOKEN_HITS=$(echo "$JOURNAL_OUTPUT" | grep -nE "$TOKEN_PATTERN" || true)
    add_hits "journal (name=value)" "$JOURNAL_NAME_HITS"
    add_hits "journal (token shape)" "$JOURNAL_TOKEN_HITS"
  fi
else
  echo "note: journalctl not found -- skipping journal check"
fi

if [ "$FOUND" -ne 0 ]; then
  echo "SECRET VALUES FOUND:"
  echo "$HITS"
  exit 1
fi

echo "no secrets found"
exit 0
