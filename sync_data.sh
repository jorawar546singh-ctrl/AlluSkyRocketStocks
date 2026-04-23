#!/bin/bash
# ==============================================================
# Sync cloud scanner data to local dashboard folder
# ==============================================================
# Pulls the latest.json / history.json / CSVs that the cloud
# scanner has committed to GitHub, so your local dashboard shows
# the same data the cloud is producing.
#
# Usage:  bash sync_data.sh
# ==============================================================

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
cd "$SCRIPT_DIR"

if [ ! -d ".git" ]; then
  echo "ERROR: This folder isn't connected to the GitHub repo yet."
  echo "Run the git setup steps first."
  exit 1
fi

echo "Syncing from GitHub..."
git pull --rebase --autostash origin main 2>&1 | grep -v "^$"
echo ""
echo "Done. Refresh your dashboard to see the latest."
