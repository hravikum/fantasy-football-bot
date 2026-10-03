#!/bin/bash
# Runs one bot script with this project's venv, logs it, and emails if it fails.
# launchd calls this; it never depends on your shell environment.
set -uo pipefail
cd "$(dirname "$0")" || exit 1

# Keep the Mac awake until this run finishes; otherwise one started during a brief
# maintenance wake can be put back to sleep halfway. -s only works on power.
caffeinate -i -s -w "$$" &

PYTHON="./.venv/bin/python3"
mkdir -p logs
LOG="logs/$(date +%Y-%m).log"

HEADER="--- $(date '+%F %T') $* ---"
if [ ! -x "$PYTHON" ]; then
    # Without the venv there's no Python to send the failure email, so say it where you'll see it.
    printf '%s\nno virtual environment at %s/.venv, so nothing ran\n' "$HEADER" "$PWD" >>"$LOG"
    osascript -e 'display notification "No .venv in the project folder, so nothing ran." with title "Fantasy bot"' 2>/dev/null
    exit 1
fi

# launchd starts a missed run the moment the Mac wakes, often before Wi-Fi is back, and
# every failure so far was exactly that: ESPN's name wouldn't resolve. Give it two minutes.
waited=0
until "$PYTHON" -c "import socket; socket.getaddrinfo('lm-api-reads.fantasy.espn.com', 443)" 2>/dev/null \
        || [ "$waited" -ge 120 ]; do
    sleep 5
    waited=$((waited + 5))
done

OUTPUT=$("$PYTHON" "$@" 2>&1)
STATUS=$?
# The 15-minute check-in prints nothing when no game is near. Logging those would bury the rest.
if [ -n "$OUTPUT" ] || [ "$STATUS" -ne 0 ]; then
    {
        echo "$HEADER"
        [ "$waited" -gt 0 ] && echo "waited ${waited}s for the network"
        printf '%s\nexit %s\n\n' "$OUTPUT" "$STATUS"
    } >>"$LOG"
fi

if [ "$STATUS" -ne 0 ]; then
    # A crash before the script's own error handling still has to reach you.
    "$PYTHON" -c "import sys, mailer; mailer.send('Fantasy bot: $1 failed', sys.stdin.read())" <<<"$OUTPUT"
fi
exit "$STATUS"
