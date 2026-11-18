#!/bin/bash
# Copies this Mac's logs and the trade watch's memory to a new bot-state branch on GitHub, which
# the GitHub runs (hosted_run.sh) start from and add to. Run it once, from the project folder,
# before the first GitHub run. It won't touch a bot-state branch that already exists.
set -euo pipefail
cd "$(dirname "$0")"
URL="$(git remote get-url origin)"
if git ls-remote --exit-code --heads "$URL" bot-state >/dev/null 2>&1; then
    echo "GitHub already has a bot-state branch, so nothing was changed." >&2
    exit 1
fi
SEED="$(mktemp -d)"
mkdir -p "$SEED/logs" "$SEED/cache"
if [ -d logs ]; then cp -R logs/. "$SEED/logs/"; fi
for kept in cache/trades_seen.json cache/trades_failed_on.txt cache/kickoffs.json; do
    if [ -f "$kept" ]; then cp "$kept" "$SEED/$kept"; fi
done
cd "$SEED"
git init -q
git checkout -q -b bot-state
git add -A
git -c user.name="fantasy bot" -c user.email="41898282+github-actions[bot]@users.noreply.github.com" \
    commit -qm "Seeded from the Mac"
git push -q "$URL" bot-state
echo "Created bot-state on GitHub with $(git ls-files | wc -l | tr -d ' ') files from this Mac."
rm -rf "$SEED"
