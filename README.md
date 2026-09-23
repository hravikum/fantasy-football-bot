# Fantasy football bot

Manages one ESPN fantasy team: sets the lineup, handles the IR slot, and emails a daily
waiver report. ESPN has no official API, so this talks to the same private endpoints the
website uses, authenticated with browser cookies.

## What it will and won't do

- Only ever touches the team your SWID cookie owns. It refuses any other team id.
- Never drops a player, never trades, never places a waiver claim. Bids are suggestions.
- Sends one transaction at a time and reads the roster back to confirm it landed.
- Never retries a timed-out write blindly; it reads the roster back and reports the truth.

## Setup

```
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

`.env` in this folder (never commit it):

```
LEAGUE_ID=            # the number in your league's URL
ESPN_S2=              # DevTools > Application > Cookies > https://fantasy.espn.com
SWID=                 # same place, keep the curly braces
GMAIL_ADDRESS=
GMAIL_APP_PASSWORD=   # myaccount.google.com/apppasswords, needs 2-Step Verification
EMAIL_TO=             # optional, defaults to GMAIL_ADDRESS
ANTHROPIC_API_KEY=    # optional, adds the written recommendations to the waiver report
TYPESAFE_API_KEY=     # optional, only for --ranker jev
```

## Commands

```
python3 check.py                    league settings and your roster, read-only
python3 lineup.py                   what it would change, changes nothing
python3 lineup.py --live            apply the moves it judges worth making
python3 lineup.py --live --force    apply the best lineup even below the point bar
python3 waivers.py                  print the waiver report
python3 waivers.py --email          print it and send it
python3 waivers.py --ranker jev     rank the pool with Jev instead of projections
python3 waivers.py --jev-shadow     score and log with Jev, but rank and email as normal
python3 claims.py                   what waiver claims actually cost in your league
python3 accuracy.py                 how good ESPN's projections have been, by position
python3 test_lineup.py              optimizer and IR tests
python3 test_waivers.py             waiver scoring tests
python3 test_jev.py                 Jev scorer tests (needs typesafe-sdk)
python3 test_pricing.py             claim pricing and projection accuracy tests
python3 test_snaps.py               snap share tests
```

## Scheduling (launchd)

```
./install_schedule.sh
```

Installs two jobs. `local.fantasy.waivers` emails the report daily at 6am, after the
overnight claim run. `local.fantasy.lineup` runs `lineup.py --live` at 9am every day for
injury and IR changes, plus repeatedly on Thursday, Sunday and Monday before kickoffs.
Every run is idempotent and only emails when it actually changes something, so a missed
run is covered by the next one.

```
launchctl kickstart -k gui/$UID/local.fantasy.waivers   # run one now
launchctl bootout gui/$UID/local.fantasy.lineup         # stop a job
tail -f logs/$(date +%Y-%m).log                         # watch what it does
```

A sleeping Mac runs nothing. Unlike cron, launchd runs a missed job when the machine
wakes, which is fine for the morning email and useless for a kickoff that already passed.
So:

- `sudo pmset repeat wakeorpoweron MTWRFSU 05:55:00` wakes it before the 6am report.
- On game days keep it plugged in, with System Settings > Battery > Options >
  "Prevent automatic sleeping on power adapter when the display is off" turned on.
- Closing the lid sleeps it regardless, unless it's on power with an external display.
- macOS lists these under System Settings > General > Login Items & Extensions as
  background items. They need to stay allowed.

## Tuning

- `MIN_GAIN` in `lineup.py` (2.0): projected points an upgrade must be worth before the
  bot moves anyone. A lineup that can't play, or an IR move, happens regardless. Measured
  against this league (weeks 1-2), ESPN's projections track results at 0.58 for running backs
  and 0.48 for receivers, so a 2-point edge there is real signal.
- `STEADY_SLOTS` in `lineup.py`: kicker and defense, where the same measurement found no
  relationship between projection and result (0.28 and -0.30). Those slots only change when
  the player in them can't play. Re-run `accuracy.py` around week 6 and revisit both.
- Bid bands in `waivers.py`: once this league has priced `MIN_CLAIMS` (10) claims, bids come
  from what winning claims actually cost here, scaled by how much you want the player
  (`TIER_PERCENTILES`). Until then they fall back to shares of your remaining FAAB. `claims.py`
  shows the distribution behind them and writes `logs/claims.csv`.
- `MIN_GAIN` is still one guessed number for every position. `accuracy.py` measures what it
  should be: for each position it reports bias, the typical miss, and how closely projections
  track results. A position whose projections barely track results has no small edges worth
  chasing, whatever the threshold says. Give it three or four finished weeks before trusting it.
- `SHORTLIST` and `POOL_SIZE` in `waivers.py`: how many free agents get scored and shown.
- `LOOKAHEAD` in `waivers.py` (4): weeks past this one that the ranking, the lineup-holes
  section and the drop list plan for, with byes applied to your players and the free agents
  alike. Beyond this week each player counts at his season per-game average, since ESPN only
  projects the current week in detail. A player whose value falls in a single week, like a bye
  rental, is capped at the cheap bid band.

## The Jev ranker (experimental)

`--ranker jev` replaces the projection ordering with judgments from TypeSafe's Jev. For
each of the top 20 candidates it asks four typed questions in one call: how large and
secure the player's role is, how much he'd help this roster all season, whether his usage
has grown, and whether he's keeper-worthy. `WEIGHTS` in `jev.py` combines them, and the
result is shrunk toward neutral in proportion to Jev's own confidence, so an unsure
judgment barely moves the ranking.

The report then shows both orderings: `JEV` is the composite, `CONF` its confidence, and
`PROJ#` where the projection math had that player. A confident Jev score can also raise a
bid band, never lower one.

What to watch while comparing:

- Jev cannot look anything up. It judges only the state in `player_state()`, which is a
  handful of ESPN numbers. Whatever that state omits, Jev cannot know.
- Its score is a judgment about our numbers, not new information. The news that made the
  written recommendations useful still comes from Claude.
- Pin the model in `jev.py` once thresholds are tuned; `jev-latest` moves under you. Every
  report prints the version that answered.

### Shadow mode and the log

The scheduled 6am job runs with `--jev-shadow`: Jev scores the day's candidates and every
judgment is appended to `logs/jev_scores.csv`, but the ranking, the bids, the email and
Claude's brief are exactly what they would be without it. Same pool, same day, both
rankings on file. After a few weeks that log can be checked against what the players
actually scored, which is the only fair test of whether Jev's confident calls were right.
To stop the experiment, remove `--jev-shadow` from `install_schedule.sh` and reinstall.

Fall back by dropping the flag. Missing key, missing SDK, or failed calls all fall back on
their own and say so in the report.

## Snap share

ESPN doesn't publish snap counts, so `snaps.py` takes them from nflverse and matches players to
ESPN through nflverse's id table. The report shows the last two weeks in the SNAP% column:
`40→78` is a role growing, `72→out` means he didn't play (a missed game is shown but never read
as a shrinking role), `-` means no snap data. The trend is computed in code; a change of
`TREND_POINTS` (15) percentage points counts as rising or falling.

Snap share goes to Claude's brief and Jev's state, but deliberately not into the ranking yet:
first it has to show it predicts points in this league, the way `accuracy.py` tested ESPN's
projections. The player table (7 MB) is cached in `cache/` for a week and snap counts for six
hours; if GitHub is down, the last cached copy is used, and with none the report goes out
without the column.

Jev's inputs changed on 2026-09-23, when snap share was added. When reviewing
`logs/jev_scores.csv`, compare judgments before and after that date separately.

## When it breaks

- **Everything returns 401.** The ESPN cookies changed. Copy them again.
- **Emails stop.** Google revokes app passwords whenever you change your account password.
- **A run reports a refusal.** ESPN validates a transaction as a whole and applies none of
  it on failure, so your roster is untouched. The email names the reason.
- **ESPN changes the API.** It's unofficial and can change without notice. `check.py` is
  the fastest way to see what still parses.
