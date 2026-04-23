#!/bin/bash
# ==============================================================
# TTM Scanner  --  Auto-run Installer
# ==============================================================
# Schedules the scanner to run every 15 minutes during US market hours
# (9:30am - 4:00pm ET, Mon-Fri) using macOS's built-in launchd.
#
# Usage:
#   bash auto_run_setup.sh install     # install + activate
#   bash auto_run_setup.sh uninstall   # stop + remove
#   bash auto_run_setup.sh status      # check if running
#   bash auto_run_setup.sh logs        # tail the scanner log
# ==============================================================

set -e

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
PYTHON_BIN="$(which python3)"
AGENT_LABEL="com.jorawar.ttmscanner"
AGENT_PATH="$HOME/Library/LaunchAgents/${AGENT_LABEL}.plist"
LOG_OUT="${SCRIPT_DIR}/scanner.log"
LOG_ERR="${SCRIPT_DIR}/scanner.err.log"

# -- 15-min slots between 9:30am and 4:00pm Eastern
#    Your Mac runs in local time, so we compute slots relative to ET.
#    launchd StartCalendarInterval takes Hour/Minute arrays in local time.
#    For simplicity we run every 15 min between 6am and 2pm Mountain Time
#    (Calgary), which covers US market hours 8am-4pm ET. Adjust if you move.

case "${1:-install}" in

  install)
    if [ -z "$PYTHON_BIN" ]; then
      echo "ERROR: python3 not found on PATH. Install Python first."
      exit 1
    fi
    echo "Installing TTM auto-scanner..."
    echo "  Script folder: $SCRIPT_DIR"
    echo "  Python:        $PYTHON_BIN"

    # Build StartCalendarInterval entries: every 15 min, 6am-2pm local,
    # Monday-Friday (weekdays 1-5 in launchd).
    SLOTS=""
    for HOUR in 6 7 8 9 10 11 12 13; do
      for MIN in 0 15 30 45; do
        SLOTS+="
    <dict>
      <key>Hour</key><integer>${HOUR}</integer>
      <key>Minute</key><integer>${MIN}</integer>
      <key>Weekday</key><integer>1</integer>
    </dict>
    <dict>
      <key>Hour</key><integer>${HOUR}</integer>
      <key>Minute</key><integer>${MIN}</integer>
      <key>Weekday</key><integer>2</integer>
    </dict>
    <dict>
      <key>Hour</key><integer>${HOUR}</integer>
      <key>Minute</key><integer>${MIN}</integer>
      <key>Weekday</key><integer>3</integer>
    </dict>
    <dict>
      <key>Hour</key><integer>${HOUR}</integer>
      <key>Minute</key><integer>${MIN}</integer>
      <key>Weekday</key><integer>4</integer>
    </dict>
    <dict>
      <key>Hour</key><integer>${HOUR}</integer>
      <key>Minute</key><integer>${MIN}</integer>
      <key>Weekday</key><integer>5</integer>
    </dict>"
      done
    done

    mkdir -p "$HOME/Library/LaunchAgents"

    cat > "$AGENT_PATH" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>${AGENT_LABEL}</string>
  <key>ProgramArguments</key>
  <array>
    <string>${PYTHON_BIN}</string>
    <string>${SCRIPT_DIR}/ttm_scanner.py</string>
  </array>
  <key>WorkingDirectory</key><string>${SCRIPT_DIR}</string>
  <key>StandardOutPath</key><string>${LOG_OUT}</string>
  <key>StandardErrorPath</key><string>${LOG_ERR}</string>
  <key>RunAtLoad</key><false/>
  <key>StartCalendarInterval</key>
  <array>${SLOTS}
  </array>
</dict>
</plist>
PLIST

    # Unload if already loaded, then load fresh
    launchctl unload "$AGENT_PATH" 2>/dev/null || true
    launchctl load "$AGENT_PATH"

    echo ""
    echo "✓ Installed successfully."
    echo ""
    echo "  Schedule: every 15 min, 6:00am-2:45pm Calgary time (= US market hours)"
    echo "  Mon-Fri only. Your Mac runs it in the background."
    echo ""
    echo "  Logs:    $LOG_OUT"
    echo "  Errors:  $LOG_ERR"
    echo ""
    echo "  Commands:"
    echo "    bash auto_run_setup.sh status"
    echo "    bash auto_run_setup.sh logs"
    echo "    bash auto_run_setup.sh uninstall"
    ;;

  uninstall)
    echo "Removing TTM auto-scanner..."
    launchctl unload "$AGENT_PATH" 2>/dev/null || true
    rm -f "$AGENT_PATH"
    echo "✓ Uninstalled."
    ;;

  status)
    if launchctl list | grep -q "$AGENT_LABEL"; then
      echo "✓ Running (scheduled)."
      launchctl list | grep "$AGENT_LABEL"
    else
      echo "✗ Not scheduled. Run: bash auto_run_setup.sh install"
    fi
    ;;

  logs)
    if [ -f "$LOG_OUT" ]; then
      echo "=== scanner.log (last 50 lines) ==="
      tail -50 "$LOG_OUT"
    else
      echo "No log yet. Wait for the next scheduled run or run manually."
    fi
    if [ -f "$LOG_ERR" ] && [ -s "$LOG_ERR" ]; then
      echo ""
      echo "=== scanner.err.log (last 30 lines) ==="
      tail -30 "$LOG_ERR"
    fi
    ;;

  *)
    echo "Usage: bash auto_run_setup.sh {install|uninstall|status|logs}"
    exit 1
    ;;
esac
