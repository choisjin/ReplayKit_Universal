#!/usr/bin/env bash
# sync_and_run.sh - sync + start server (Linux equivalent of sync_and_run.bat)
cd "$(dirname "$(readlink -f "$0")")"

ENTRY="server.py"
[ -f "_launcher.py" ] && ENTRY="_launcher.py"

if [ -x "venv/bin/python" ]; then
    exec venv/bin/python "$ENTRY"
else
    echo "[ERROR] Python not found. Run ./setup.sh first."
    exit 1
fi
