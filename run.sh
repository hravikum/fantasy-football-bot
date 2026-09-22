#!/bin/bash
# Runs one bot script with this project's venv, logs it, and emails if it fails.
# launchd calls this; it never depends on your shell environment.
set -uo pipefail
cd "$(dirname "$0")" || exit 1

PYTHON="./.venv/bin/python3"
mkdir -p logs
LOG="logs/$(date +%Y-%m).log"

echo "--- $(date '+%F %T') $* ---" >>"$LOG"
if [ ! -x "$PYTHON" ]; then
    # Without the venv there's no Python to send the failure email, so say it where you'll see it.
    echo "no virtual environment at $PWD/.venv, so nothing ran" >>"$LOG"
    osascript -e 'display notification "No .venv in the project folder, so nothing ran." with title "Fantasy bot"' 2>/dev/null
    exit 1
fi
OUTPUT=$("$PYTHON" "$@" 2>&1)
STATUS=$?
printf '%s\nexit %s\n\n' "$OUTPUT" "$STATUS" >>"$LOG"

if [ "$STATUS" -ne 0 ]; then
    # A crash before the script's own error handling still has to reach you.
    "$PYTHON" -c "import sys, mailer; mailer.send('Fantasy bot: $1 failed', sys.stdin.read())" <<<"$OUTPUT"
fi
exit "$STATUS"
