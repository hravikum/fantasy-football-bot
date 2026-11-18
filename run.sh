#!/bin/bash
# Runs one bot script with this project's venv, logs it, and emails if it fails.
# launchd calls this; it never depends on your shell environment. On GitHub, hosted_run.sh
# calls it with PYTHON set, since there is no venv there.
set -uo pipefail
cd "$(dirname "$0")" || exit 1

ON_MAC=""
if [ "$(uname)" = "Darwin" ]; then
    ON_MAC=1
    # Keep the Mac awake until this run finishes; otherwise one started during a brief
    # maintenance wake can be put back to sleep halfway. -s only works on power.
    caffeinate -i -s -w "$$" &

    # What the Mac was running on, for the log. With the lid closed and no charger it goes back to
    # sleep mid-run whatever caffeinate asks; that cost 24 of the first 53 waiver reports their write-up.
    POWER="$(pmset -g batt 2>/dev/null | sed -n "1s/.*'\(.*\)'.*/\1/p")"
    LID_CLOSED="$(ioreg -r -k AppleClamshellState -d 4 2>/dev/null | grep -c '"AppleClamshellState" = Yes')"
fi

PYTHON="${PYTHON:-./.venv/bin/python3}"
mkdir -p logs
LOG="logs/$(date +%Y-%m).log"

HEADER="--- $(date '+%F %T') $* ---"
if ! command -v "$PYTHON" >/dev/null; then
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
        if [ -z "$ON_MAC" ]; then :
        elif [ "${LID_CLOSED:-0}" = "0" ]; then echo "power: ${POWER:-unknown}, lid open"
        else echo "power: ${POWER:-unknown}, lid closed"; fi
        printf '%s\nexit %s\n\n' "$OUTPUT" "$STATUS"
    } >>"$LOG"
fi

if [ "$STATUS" -ne 0 ]; then
    # A crash before the script's own error handling still has to reach you.
    "$PYTHON" -c "import sys, mailer; mailer.send('$1 failed', sys.stdin.read())" <<<"$OUTPUT"
fi
exit "$STATUS"
