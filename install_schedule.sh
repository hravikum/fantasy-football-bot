#!/bin/bash
# Installs the launchd jobs. Run from the project folder: ./install_schedule.sh
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
AGENTS="$HOME/Library/LaunchAgents"
mkdir -p "$AGENTS"

if [ ! -x "$DIR/.venv/bin/python3" ]; then
    echo "No virtual environment at $DIR/.venv. Create it first:" >&2
    echo "  python3 -m venv .venv && .venv/bin/pip install -r requirements.txt" >&2
    exit 1
fi

# Lineup, morning pass: injuries, IR, and anything the overnight waivers changed. 9:05 so it
# never runs alongside the 9:00 check-in below. Like every lineup run, it only emails on a change.
cat >"$AGENTS/local.fantasy.lineup.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>local.fantasy.lineup</string>
  <key>ProgramArguments</key>
  <array><string>/bin/bash</string><string>$DIR/run.sh</string><string>lineup.py</string><string>--live</string></array>
  <key>WorkingDirectory</key><string>$DIR</string>
  <key>StandardOutPath</key><string>$DIR/logs/launchd.out</string>
  <key>StandardErrorPath</key><string>$DIR/logs/launchd.err</string>
  <key>StartCalendarInterval</key>
  <array>
    <dict><key>Hour</key><integer>9</integer><key>Minute</key><integer>5</integer></dict>
  </array>
</dict>
</plist>
PLIST

# Lineup, game time: checks in every 15 minutes and acts only when a game kicks off within 95
# minutes, by ESPN's real schedule, so Saturday and holiday games count without editing this.
cat >"$AGENTS/local.fantasy.gameday.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>local.fantasy.gameday</string>
  <key>ProgramArguments</key>
  <array><string>/bin/bash</string><string>$DIR/run.sh</string><string>lineup.py</string><string>--live</string><string>--near-kickoff</string></array>
  <key>WorkingDirectory</key><string>$DIR</string>
  <key>StandardOutPath</key><string>$DIR/logs/launchd.out</string>
  <key>StandardErrorPath</key><string>$DIR/logs/launchd.err</string>
  <key>StartCalendarInterval</key>
  <array>
    <dict><key>Minute</key><integer>0</integer></dict>
    <dict><key>Minute</key><integer>15</integer></dict>
    <dict><key>Minute</key><integer>30</integer></dict>
    <dict><key>Minute</key><integer>45</integer></dict>
  </array>
</dict>
</plist>
PLIST

# Waiver report: every morning at 6, after the overnight claim run. --jev-shadow logs Jev's
# judgments to logs/jev_scores.csv without changing the email; drop it to stop the experiment.
cat >"$AGENTS/local.fantasy.waivers.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>local.fantasy.waivers</string>
  <key>ProgramArguments</key>
  <array><string>/bin/bash</string><string>$DIR/run.sh</string><string>waivers.py</string><string>--email</string><string>--jev-shadow</string></array>
  <key>WorkingDirectory</key><string>$DIR</string>
  <key>StandardOutPath</key><string>$DIR/logs/launchd.out</string>
  <key>StandardErrorPath</key><string>$DIR/logs/launchd.err</string>
  <key>StartCalendarInterval</key>
  <array>
    <dict><key>Hour</key><integer>6</integer><key>Minute</key><integer>0</integer></dict>
  </array>
</dict>
</plist>
PLIST

# Trades: every 15 minutes, offset from the gameday check-ins, look for proposals sent to you or
# by you and email an analysis of each new one. Silent when there's nothing new; read-only always.
cat >"$AGENTS/local.fantasy.trades.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>local.fantasy.trades</string>
  <key>ProgramArguments</key>
  <array><string>/bin/bash</string><string>$DIR/run.sh</string><string>trades.py</string><string>--watch</string></array>
  <key>WorkingDirectory</key><string>$DIR</string>
  <key>StandardOutPath</key><string>$DIR/logs/launchd.out</string>
  <key>StandardErrorPath</key><string>$DIR/logs/launchd.err</string>
  <key>StartCalendarInterval</key>
  <array>
    <dict><key>Minute</key><integer>7</integer></dict>
    <dict><key>Minute</key><integer>22</integer></dict>
    <dict><key>Minute</key><integer>37</integer></dict>
    <dict><key>Minute</key><integer>52</integer></dict>
  </array>
</dict>
</plist>
PLIST

for job in local.fantasy.lineup local.fantasy.gameday local.fantasy.waivers local.fantasy.trades; do
    launchctl bootout "gui/$UID/$job" 2>/dev/null || true
    launchctl bootstrap "gui/$UID" "$AGENTS/$job.plist"
    echo "installed $job"
done
echo
echo "Test one now:  launchctl kickstart -k gui/$UID/local.fantasy.waivers"
echo "Stop one:      launchctl bootout gui/$UID/local.fantasy.gameday"
