#!/bin/bash
# ==============================================================
# TTM Dashboard  --  Launcher
# ==============================================================
# Starts a tiny local web server so the dashboard can read
# latest.json / history.json (browsers block fetch() on file://).
# Then opens the dashboard in your default browser.
#
# Usage:  bash run_dashboard.sh
# Stop:   Ctrl+C in this terminal window
# ==============================================================

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
cd "$SCRIPT_DIR"

PORT=8765

echo ""
echo "============================================"
echo "  TTM Breakout Terminal"
echo "============================================"
echo ""
echo "  Dashboard: http://localhost:${PORT}/dashboard.html"
echo ""
echo "  Opening in browser in 2 seconds..."
echo "  Press Ctrl+C to stop the server."
echo ""

# Open browser in background (slight delay so server is up)
(sleep 2 && open "http://localhost:${PORT}/dashboard.html") &

# Start Python's built-in HTTP server (blocks until Ctrl+C)
python3 -m http.server $PORT
