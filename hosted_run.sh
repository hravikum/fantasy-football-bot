#!/bin/bash
# Runs one job of the bot on GitHub's machines (.github/workflows/bot.yml says when).
#
# GitHub starts every run on a fresh machine, so the files the bot keeps between runs (logs/, and
# the trade watch's memory in cache/) live on the bot-state branch: this brings them back, runs the
# job through run.sh, and saves whatever changed. Until the repository variable BOT_LIVE is "true",
# a run is a shadow: no email goes out and no lineup is changed, so it can run beside the Mac.
#
# Usage: hosted_run.sh <schedule or job>     the cron line that fired, or waivers|lineup|gameday|trades|claim
# (claim: a bid from Slack's Bid button, which the Worker passes in CLAIM_ARGS)
set -uo pipefail
cd "$(dirname "$0")" || exit 1

case "${1:-}" in
    "7 6 * * *" | waivers)  JOB=(waivers.py --email --rate-shadow) ;;
    "5 9 * * *" | lineup)   JOB=(lineup.py) ;;
    "2,17,32,47 "* | gameday) JOB=(lineup.py --near-kickoff) ;;
    "3 8,23 * * *" | trades) JOB=(trades.py --watch) ;;
    claim) JOB=(claim.py) ;;
    *) echo "No job for '${1:-}'. The schedules in bot.yml and this list have to match." >&2; exit 2 ;;
esac
if [ "${BOT_LIVE:-}" = "true" ]; then
    if [ "${JOB[0]}" = "lineup.py" ] || [ "${JOB[0]}" = "claim.py" ]; then JOB+=(--live); fi
else
    export MAIL_TO_LOG=1
    echo "Shadow run: no email goes out and no lineup is changed (repository variable BOT_LIVE is not true)."
fi

# Small files the bot reads back on its next run. players.csv and the snap counts are left out:
# they are public downloads, fetched again in a second or two.
KEPT=(cache/trades_seen.json cache/trades_failed_on.txt cache/kickoffs.json cache/needs_you.json)
STATE="${RUNNER_TEMP:-/tmp}/bot-state"
rm -rf "$STATE"
if git fetch -q --depth=1 origin bot-state 2>/dev/null; then
    git worktree add -q --detach "$STATE" FETCH_HEAD
else
    echo "No bot-state branch yet, so this run starts with no logs. seed_state.sh copies the Mac's."
    git worktree add -q --orphan -b bot-state "$STATE"
fi
mkdir -p logs cache
if [ -d "$STATE/logs" ]; then cp -R "$STATE/logs/." logs/; fi
for kept in "${KEPT[@]}"; do
    if [ -f "$STATE/$kept" ]; then cp "$STATE/$kept" "$kept"; fi
done

LOG="logs/$(date +%Y-%m).log"
BEFORE=0
if [ -f "$LOG" ]; then BEFORE=$(wc -c <"$LOG"); fi
PYTHON="${PYTHON:-python}" bash ./run.sh "${JOB[@]}"
STATUS=$?
# run.sh writes only to the log; this puts the same lines in GitHub's view of the run.
if [ -f "$LOG" ]; then tail -c +"$((BEFORE + 1))" "$LOG"; fi
# mailer.py prints this when a message reached neither Slack nor email. The job has done its work
# and its files are still saved below; failing the run is what gets GitHub to email you.
if [ -f "$LOG" ] && tail -c +"$((BEFORE + 1))" "$LOG" | grep -qF "(nothing delivered:"; then
    echo "A message reached neither Slack nor email (above), so this run is marked failed." >&2
    STATUS=1
fi

mkdir -p "$STATE/logs" "$STATE/cache"
cp -R logs/. "$STATE/logs/"
for kept in "${KEPT[@]}"; do
    if [ -f "$kept" ]; then cp "$kept" "$STATE/$kept"; fi
done
git -C "$STATE" add -A
if ! git -C "$STATE" diff --cached --quiet; then
    git -C "$STATE" -c user.name="fantasy bot" -c user.email="41898282+github-actions[bot]@users.noreply.github.com" \
        commit -qm "${JOB[*]}, $(date '+%a %b %d %H:%M')"
    if ! git -C "$STATE" push -q origin HEAD:refs/heads/bot-state; then
        # The trade watch would forget what it has reported, so this has to reach you.
        echo "Could not save to the bot-state branch: the next run starts from the last save." >&2
        STATUS=1
    fi
fi
exit "$STATUS"
