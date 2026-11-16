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
python3 lineup.py --live --near-kickoff   the same, only if a game kicks off within 95 minutes
python3 waivers.py                  print the waiver report
python3 waivers.py --email          print it and send it
python3 waivers.py --ranker jev     rank the pool with Jev instead of projections
python3 waivers.py --jev-shadow     score and log with Jev, but rank and email as normal
python3 trades.py --give "A" --get "B"   what a trade does to both lineups, read-only
python3 trades.py --watch           email an analysis of each new proposal sent to you or by you
python3 trade_probe.py              what ESPN sends for trades, schedule and draft, read-only
python3 claims.py                   what waiver claims actually cost in your league
python3 auctions.py                 each waiver auction: bidders, price, what the report saw
python3 accuracy.py [season]        how good ESPN's projections have been, by position
python3 snap_test.py [season]       whether a rising snap share beats ESPN's projection
python3 value_test.py               whether player levels beat ESPN's season projection, on ESPN's numbers
python3 role_probe.py               every next man up right now, and how deep the free-agent list goes, read-only
python3 test_lineup.py              optimizer and IR tests
python3 test_waivers.py             waiver scoring tests
python3 test_jev.py                 Jev scorer tests (needs typesafe-sdk)
python3 test_pricing.py             claim pricing and projection accuracy tests
python3 test_snaps.py               snap share tests
python3 test_trades.py              trade analyzer tests
python3 test_values.py              player level tests
python3 test_roles.py               next-man-up tests
```

## Scheduling (launchd)

```
./install_schedule.sh
```

Installs four jobs. `local.fantasy.waivers` emails the report daily at 6am, after the
overnight claim run. `local.fantasy.lineup` runs `lineup.py --live` at 9:05 every day for
injury and IR changes. `local.fantasy.gameday` checks in every 15 minutes and runs the same
pass whenever an NFL game kicks off within the next 95 minutes, which is when inactives come
out. Kickoff times come from ESPN's NFL schedule, kept in `cache/kickoffs.json` and refreshed
daily, so Thursday, Saturday and holiday games are covered without editing anything. A
check-in with no game near prints nothing and stays out of the log. Every run is idempotent
and only emails when it actually changes something, so a missed run is covered by the next
one. The "roster needs you" email comes from the morning pass only, not every check-in.

`local.fantasy.trades` checks at :07, :22, :37 and :52 for trade proposals sent to you or by
you, and emails an analysis of each new one. It never answers a proposal. A failure there is
logged every time but emailed once a day, since it runs 96 times a day.

When two starters can trade slots at no cost, the one whose game starts later goes to FLEX,
so a late scratch there can be replaced from any position. That move is made even though it
gains no points, because once the earlier game kicks off the swap is locked out.

```
launchctl kickstart -k gui/$UID/local.fantasy.waivers   # run one now
launchctl bootout gui/$UID/local.fantasy.gameday        # stop a job
tail -f logs/$(date +%Y-%m).log                         # watch what it does
```

A sleeping Mac runs nothing. Unlike cron, launchd runs a missed job when the machine
wakes, which is fine for the morning email and useless for a kickoff that already passed.
So:

- `sudo pmset repeat wakeorpoweron MTWRFSU 05:55:00` wakes it before the 6am report.
- On game days keep it plugged in, with System Settings > Battery > Options >
  "Prevent automatic sleeping on power adapter when the display is off" turned on.
  `pmset -g custom` should then show `sleep 0` under AC Power.
- Closing the lid sleeps it regardless, unless it's on power with an external display.
- `run.sh` holds the Mac awake until each run finishes (`caffeinate`; the part that stops
  system sleep only works on power). A run launchd starts as the Mac wakes can beat Wi-Fi
  back, so it first waits up to two minutes for the network, and the log says
  `waited 15s for the network` when it had to.
- macOS lists these under System Settings > General > Login Items & Extensions as
  background items. They need to stay allowed.

## Tuning

- `MIN_GAIN` in `lineup.py` (0.5): projected points an upgrade must be worth before the
  bot moves anyone. A lineup that can't play, or an IR move, happens regardless. Over the
  2025 season one projected point was worth about 0.9 real points at RB, WR and TE
  (`python3 accuracy.py 2025`), so every upgrade held back costs nearly its whole edge. The
  bar is only there to stop the lineup flipping back and forth.
- A kicker who can play keeps his slot (`STEADY_SLOTS` in `lineup.py`). One projected kicker
  point was worth +1.04 real points over 2025 and -1.70 over 2026's first five weeks, about
  zero together, so swapping one kicker who plays for another is a coin flip. The waiver report
  follows the same rule: a new kicker is only worth something in a week yours can't play.
  Defenses follow projections like every other slot, since a projected point was worth 0.65
  real ones in 2025 and 0.73 in 2026. Both are bid in the cheap range at most (`STREAM_ONLY` in
  `waivers.py`), since there's always another one on the wire.
- Bid bands in `waivers.py`: once this league has priced `MIN_CLAIMS` (10) claims, bids come
  from what winning claims actually cost here, scaled by how much you want the player
  (`TIER_PERCENTILES`). Until then they fall back to shares of your remaining FAAB. `claims.py`
  shows the distribution behind them and writes `logs/claims.csv`.
- `accuracy.py` reports PER PROJ PT for each position: what one projected point has been
  worth in real points, so each upgrade `MIN_GAIN` holds back costs about that times its edge.
  The size of the misses doesn't change that; it only means any single call can go wrong.
- `SHORTLIST` and `POOL_SIZE` in `waivers.py`: how many free agents get scored and shown.
- `LOOKAHEAD` in `waivers.py` (4): weeks past this one that the ranking, the lineup-holes
  section and the drop list plan for, with byes applied to your players and the free agents
  alike. Beyond this week each player counts at his level (see Player levels), since ESPN only
  projects the current week in detail. A player whose value falls in a single week, like a bye
  rental, is capped at the cheap bid band.

## Player levels

ESPN projects the current week in detail and nothing after it, so every later week needs a number
for what a player scores a game. That number used to be ESPN's season projection divided by 17,
and ESPN almost never revises it: of 44 players in `logs/jev_scores.csv` for ten days or more, 39
never moved by a point. Justin Herbert's stayed at 276 (16.2 a game) through 39 days while he
scored 10.1 a game, which is why the report listed him on 30 of 32 mornings.

`values.py` replaces it with a level: a Kalman filter that starts at ESPN's projection per game
and moves toward what the player scores, one finished game at a time. The first game moves it
about a fifth of the way, and later ones settle near 13% each. A game he missed isn't counted,
and neither is the current week, which may still be in progress. The waiver ranking, the
suggested drop, replacement level and the trade analyzer all read it, and the reports show it as
LEVEL. Kickers and defenses keep ESPN's number, since the two settings (`PRIOR_GAMES`, `DRIFT`)
were fit on quarterbacks, backs, receivers and tight ends only. On Monday morning ESPN is still
on the week just played, so levels there are one game behind until Tuesday.

The settings were fit on public 2025 results (nflverse) and scored on 2026: against a fixed
preseason number, the level missed a player's next four weeks by 8% less at QB, 10% at RB, 8% at
WR and 4% at TE. Most of any miss is game-to-game noise that nothing removes, so the gain sits in
the players who have changed.

`value_test.py` runs the same comparison on ESPN's own projections and this league's scoring. On
weeks 1-9 of 2026 (296 players) the level missed by 10% less at QB, 3% less at RB and 2% less at
WR, and by 5% more at TE. Every player's missed games were counted correctly. ESPN's projection
is a better starting point than the public stand-in was, most of all at tight end, so it likely
deserves more weight than `PRIOR_GAMES = 4`. The test prints the miss at each weight for each
position, and separately for the player-weeks where the two numbers are three or more points
apart, which are the ones that change a ranking. Kickers and defenses came out worse with a level
(3% and 6%), so they stay on ESPN's number.

What a level can't see is a role that changed yesterday. A backup who takes over needs three or
four games as the starter before his level says so, and the league bids on him after one. That
is what the next section is for.

## Next man up

The players this league pays most for are backs who have just inherited a job. `roles.py` finds
them from the workload: carries plus targets, game by game, for every running back on a roster
or among the free agents. A team has a lead back when one runner averaged 12 or more over the
three games before the latest and held 45% of the backfield's work. He is down if he had under
35% of his usual work in the latest game (he missed it or left it), or if his tag is out,
doubtful, suspended or injured reserve. The next man is whoever took the work in that game, or,
when only the tag says so, the back with the most work before it.

A free agent flagged this way is listed in the waiver report whatever he adds to your lineup, on
top of the usual eight (`ROLE_SLOTS`), with a line above the table saying who he is behind and
why. His lineup gains count him at 9.4 points a game for four weeks, and he is bid in the top
band, since for some team in the league he is a starter. Claude is told to look up how long the
lead back is out before recommending him.

Replayed on public 2025 and 2026 box scores, the rule flagged 49 backs who then played. They
averaged 9.4 a game over their next four weeks, where their levels said 7.0. Just under half
reached 10 a game and a quarter stayed under 5. Nothing told the hits from the misses beforehand
(not the back's level, the lead's, or his workload in the game), which is why every one is
valued the same. In 2026 it named Ollie Gordon, Tyrone Tracy, Audric Estime and Kaleb Johnson
before the auctions that took them for $37, $6, $19 and $11. A bigger workload with no lead back
down predicted nothing a level didn't already say, so there is no rule for that.

Three limits. Past injury tags aren't kept anywhere, so the tag half of the rule has not been
replayed. A back who is bought on news alone, before any game shows it and before ESPN tags the
lead, is still missed unless Claude's search finds him. And the bid is a wide range from the
league's top claims, not yet a price for a contested player.

The report now reads the 200 most rostered free agents (`POOL_SIZE`), up from 75. The shorter
list ended near 3% rostered, above a receiver and a back the league went on to pay $28 and $11 for.

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
projections; `snap_test.py` is that test. Over the 2025 season it found nothing: players whose
snap share had just risen beat their projection by only 0.2 points a game more than steady ones
(±0.5), so ESPN's projections already price role changes and snap share stays out of the
ranking. The first five weeks of 2026 agree: -0.5 ±0.9 over 374 player-weeks. The player
table (7 MB) is cached in `cache/` for a week and snap counts for six hours; if GitHub is down,
the last cached copy is used, and with none the report goes out without the column.

Jev's inputs changed on 2026-09-23, when snap share was added. When reviewing
`logs/jev_scores.csv`, compare judgments before and after that date separately. The `math_rank`
column changed meaning when player levels went in (November 2026), since the ranking it records
now values later weeks at each player's level.

## Waiver auctions

ESPN returns every waiver claim, the losing ones included, so each night's run reads as a set
of sealed-bid auctions. `auctions.py` groups the claims by player and night, shows what a claim
cost by how many teams bid, lists the recent contested ones, and writes `logs/auctions.csv`.
Through week 6 the auctions showed the bid rule's blind spot: bids are priced by how much a
player helps you, but the price is set by how many rivals want him. Players nobody else claimed
went for whatever was bid, and every claim you lost was a player the report rated as a stash or
never listed, in an auction that drew three to eight bids.

To find out whether that can be seen coming, every `waivers.py` run appends what it knew about
each free agent it fetched to `logs/pool.csv`: his rostered %, ESPN's change in it (the `+/-`
column, now in the report as well), the week's projection, and the report's band and bid.
`auctions.py` matches each auction to the last of those rows from before the run. That was meant
to answer two questions. Does the add rate predict how many teams bid and how high the top rival
goes? And do free agents who are being added beat their projections?

A month of `logs/pool.csv` answered the first: no, and it can't. Nearly every claim here processes
in the Wednesday run (112 of 131 auctions through week 8, and $469 of the $496 spent), and ESPN
resets the change every Tuesday, so on the morning before that run it reads about zero for
everyone: an average of 0.04 to 0.13 points across the pool on the four Tuesdays logged. Rostered %
itself barely moves between Sunday and Tuesday either, since most ESPN leagues also wait for
Wednesday. The add rate only moves after the auction, so it stays display-only. What the
contested players had in common was on the field: across the ten claims of $10 or more at RB, WR
and TE, the player's last game averaged 11.3 points on 10.1 carries plus targets, against 3.0 and
3.2 before it. Those players went on to score 8.7 a game, against 5.2 for the $3-9 claims.

## When it breaks

- **Everything returns 401.** The ESPN cookies changed. Copy them again.
- **A run fails with "Failed to resolve lm-api-reads.fantasy.espn.com".** The Mac had no
  network for two minutes after it woke. It was almost certainly asleep on battery; see Scheduling.
- **Emails stop.** Google revokes app passwords whenever you change your account password.
- **A run reports a refusal.** ESPN validates a transaction as a whole and applies none of
  it on failure, so your roster is untouched. The email names the reason.
- **ESPN changes the API.** It's unofficial and can change without notice. `check.py` is
  the fastest way to see what still parses.
- **A log line says a lookup failed with "Connection reset by peer".** ESPN closes a connection
  the report left idle while it did other work. Jev's usage lookup lost 6 of 14 November mornings
  to this, and judged without usage on those days. Every ESPN read now retries once, which opens
  a new connection.
