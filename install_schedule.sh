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

# Lineup: a daily injury sweep, plus repeated runs on game days. Each run is idempotent,
# so a missed one is covered by the next, and it only emails when it actually changes something.
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
    <dict><key>Hour</key><integer>9</integer><key>Minute</key><integer>0</integer></dict>
    <dict><key>Weekday</key><integer>4</integer><key>Hour</key><integer>18</integer><key>Minute</key><integer>0</integer></dict>
    <dict><key>Weekday</key><integer>4</integer><key>Hour</key><integer>19</integer><key>Minute</key><integer>45</integer></dict>
    <dict><key>Weekday</key><integer>0</integer><key>Hour</key><integer>8</integer><key>Minute</key><integer>15</integer></dict>
    <dict><key>Weekday</key><integer>0</integer><key>Hour</key><integer>11</integer><key>Minute</key><integer>35</integer></dict>
    <dict><key>Weekday</key><integer>0</integer><key>Hour</key><integer>12</integer><key>Minute</key><integer>45</integer></dict>
    <dict><key>Weekday</key><integer>0</integer><key>Hour</key><integer>15</integer><key>Minute</key><integer>5</integer></dict>
    <dict><key>Weekday</key><integer>0</integer><key>Hour</key><integer>16</integer><key>Minute</key><integer>10</integer></dict>
    <dict><key>Weekday</key><integer>0</integer><key>Hour</key><integer>19</integer><key>Minute</key><integer>50</integer></dict>
    <dict><key>Weekday</key><integer>1</integer><key>Hour</key><integer>18</integer><key>Minute</key><integer>0</integer></dict>
    <dict><key>Weekday</key><integer>1</integer><key>Hour</key><integer>19</integer><key>Minute</key><integer>45</integer></dict>
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

for job in local.fantasy.lineup local.fantasy.waivers; do
    launchctl bootout "gui/$UID/$job" 2>/dev/null || true
    launchctl bootstrap "gui/$UID" "$AGENTS/$job.plist"
    echo "installed $job"
done
echo
echo "Test one now:  launchctl kickstart -k gui/$UID/local.fantasy.waivers"
echo "Stop them:     launchctl bootout gui/$UID/local.fantasy.lineup"
