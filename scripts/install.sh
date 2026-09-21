#!/bin/bash
set -euo pipefail

# Install kosaccounts dependencies and create environment (idempotent)

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(dirname "$SCRIPT_DIR")"

echo "=== Kosaccounts Installation Setup ==="

# Print sudo command for system packages
echo ""
echo "Step 1: Install system packages (requires sudo)"
echo "  sudo apt install -y poppler-utils"
echo ""

# Check and install uv if missing
if [ ! -f "$HOME/.local/bin/uv" ]; then
  echo "Step 2: Installing uv package manager..."
  curl -LsSf https://astral.sh/uv/install.sh | sh
else
  echo "Step 2: uv is already installed."
fi

# Create venv if missing
if [ ! -d "$REPO/.venv" ]; then
  echo "Step 3: Creating virtual environment..."
  "$HOME/.local/bin/uv" venv "$REPO/.venv" --python 3.12
else
  echo "Step 3: Virtual environment already exists."
fi

# Install Python dependencies
echo "Step 4: Installing Python packages..."
"$HOME/.local/bin/uv" pip install --python "$REPO/.venv/bin/python" -r "$REPO/requirements.txt"

# Create output directories if missing
mkdir -p "$REPO/logs" "$REPO/output" "$REPO/data"

echo ""
echo "=== Next Manual Steps ==="
echo ""
echo "1. Authorise Dropbox access and install the systemd units (see scripts/README-dropbox.md"
echo "   and scripts/install_systemd.sh):"
echo "   $REPO/.venv/bin/python -m kosaccounts intake auth-setup"
echo "   $REPO/scripts/install_systemd.sh          # prints the sudo commands to run"
echo "   $REPO/scripts/install_systemd.sh --apply  # or runs them directly"
echo ""
echo "2. Seed data from the master workbook:"
echo "   $REPO/.venv/bin/python -m kosaccounts seed --master specs/Kosibah\ LLC\ purchases\ and\ receipts.xlsx"
echo ""
echo "Installation complete."
