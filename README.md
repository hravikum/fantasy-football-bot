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
python3 auctions.py                 each waiver auction: bidders, price, what the report saw, how its bids did
python3 accuracy.py [season]        how good ESPN's projections have been, by position
python3 snap_test.py [season]       whether a rising snap share beats ESPN's projection
python3 value_test.py               whether player levels beat ESPN's season projection, on ESPN's numbers
python3 lineup_test.py [season]     whether lineups set some other way would have outscored ESPN's projection
python3 hosted_check.py             whether every outside service the bot needs answers from this machine
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
- With the lid closed the Mac sleeps, and jobs only run on its own periodic wakes, roughly once
  an hour. `run.sh` holds it awake until a run finishes (`caffeinate`), but the part that stops
  system sleep only works on power. On battery with the lid closed it goes back to sleep in the
  middle of a run. That cost 24 of the first 53 waiver reports their write-up, and it is why
  game-day check-ins have been landing about hourly instead of every 15 minutes: of eight
  Sundays through Nov 15, nothing ran at all on one (Nov 1), and on two more (Oct 11 and 18)
  nothing ran for more than four hours of the afternoon. So leave it plugged in overnight and
  on Sundays.
- Every logged run says what it ran on, for example `power: Battery Power, lid closed`, so a
  morning with no write-up can be checked against it.
- A run launchd starts as the Mac wakes can beat Wi-Fi back, so `run.sh` first waits up to two
  minutes for the network, and the log says `waited 15s for the network` when it had to.
- The Claude API is blocked on some office networks (`curl` gets a reset instead of a status
  code). A report that runs or retries there goes out with the table and a line saying the
  write-up is missing.
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
  real ones in 2025 and 0.73 in 2026. Both are bid at stash odds at most (`STREAM_ONLY` in
  `waivers.py`), off what kickers and defenses cost here, since there's always another one on
  the wire.
- Bids in `waivers.py` (see Bids below): `STARTER` and `STASH` set how often each end of a bid
  range should win. `MIN_CLAIMS` (10) is how many past claims of a kind the league must have
  priced before its prices are used; until then a bid is a share of your remaining FAAB
  (`BANDS`). `BREAKOUT_POINTS` and `BREAKOUT_JUMP` in `bids.py` define the breakout game.
  `claims.py` shows every executed claim's price and who has spent what, and writes
  `logs/claims.csv`.
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
and moves toward what the player scores, one finished game at a time. A game he missed isn't
counted, and neither is the current week, which may still be in progress. The waiver ranking,
the suggested drop, replacement level and the trade analyzer all read it, and the reports show
it as LEVEL. On Monday morning ESPN is still on the week just played, so levels there are one
game behind until Tuesday.

There are two settings. `DRIFT` is how far a level can move per game, fit on public 2025 results
(nflverse). `PRIOR_GAMES` is how many games ESPN's projection counts for, and it is set per
position from `value_test.py`, which scores the level against ESPN's number on this league's own
projections and scoring: what each said a player would average over his next four weeks, against
what he did. On weeks 1-9 of 2026 (295 players), the average miss in points a game:

| | ESPN only | 2 games | 4 | 8 | 16 | 32 |
|---|---|---|---|---|---|---|
| QB | 5.41 | 4.89 | **4.87** | 4.90 | 4.97 | 5.02 |
| RB | 3.98 | 3.91 | 3.85 | 3.82 | **3.81** | 3.82 |
| WR | 3.26 | 3.33 | 3.22 | 3.17 | **3.15** | 3.16 |
| TE | **2.74** | 2.99 | 2.88 | 2.82 | 2.79 | 2.78 |
| K | 2.96 | 3.17 | 3.06 | 2.99 | 2.95 | 2.93 |
| D/ST | **3.01** | 3.35 | 3.19 | 3.10 | 3.05 | 3.03 |

So a quarterback's projection counts for 4 games, a back's or receiver's for 16, and tight ends,
kickers and defenses keep ESPN's number, which nothing beat by more than noise. ESPN's projection
is a weak guide at quarterback, where jobs change hands (Kirk Cousins was projected for 1.5 a
game and scored 19.1), and a good one elsewhere. That is also why one game moves a quarterback's
level about a fifth of the way and a back's or receiver's under a tenth; every position settles
near 13% a game. The gain is 10% at QB, 4% at RB and 3% at WR. Most of any miss is game-to-game
noise that nothing removes, so the gain sits in the players who have changed. Every player's
missed games were counted correctly (295 of 295). To revisit the weights, run the test again:
it marks the weight in use and shows the same comparison for only the player-weeks where the two
numbers are three or more points apart.

What a level can't see is a role that changed yesterday. A backup who takes over needs weeks as
the starter before his level says so, and the league bids on him after one game. At running back
the level did worse than ESPN's number where the two were far apart at the lighter weight (5.16
against 4.92), which is the other reason backs lean on the projection. Takeovers are what the
next section is for.

## Next man up

The players this league pays most for are backs who have just inherited a job. `roles.py` finds
them from the workload: carries plus targets, game by game, for every running back on a roster
or among the free agents. A team has a lead back when one runner averaged 12 or more over the
three games before the latest and held 45% of the backfield's work. He is down if he had under
35% of his usual work in the latest game (he missed it or left it), or if his tag is out,
doubtful, suspended or injured reserve. The next man is whoever took the work in that game, or,
when only the tag says so, the back with the most work before it. A back who has averaged more
work than the "lead" over the season is a starter returning to his own job, not a next man up,
and isn't flagged (Cam Skattebo in week 10, on the first live run).

A free agent flagged this way is listed in the waiver report whatever he adds to your lineup, on
top of the usual eight (`ROLE_SLOTS`), with a line above the table saying who he is behind and
why. His lineup gains count him at 9.5 points a game for four weeks, and his bid is priced off
what the league has paid after a breakout game (Bids, below), since for some team in the league
he is a starter. Claude is told to look up how long the lead back is out before recommending him.

Replayed on public 2025 and 2026 box scores, the rule flagged 42 backs who then played. They
averaged 9.5 a game over their next four weeks, where their levels said 6.0. Just under half
reached 10 a game and a quarter stayed under 5. Nothing told the hits from the misses beforehand
(not the back's level, the lead's, or his workload in the game), which is why every one is
valued the same. In 2026 it named Ollie Gordon, Tyrone Tracy, Audric Estime and Kaleb Johnson
before the auctions that took them for $37, $6, $19 and $11. Naming who the league will chase is
not naming who pays off: Gordon averaged 16 over his next three games, and Estime and Johnson
scored 1.6 and 4.2 in the game after theirs. A bigger workload with no lead back down predicted
nothing a level didn't already say, so there is no rule for that.

`role_probe.py` prints every flag in the league, rostered backs included, and which lead backs
carry a tag. Its first run (Nov 16) also showed that ESPN marks each free agent as on waivers or
free to add, with the time his waivers clear, and that it will send running backs only.

Three limits. Past injury tags aren't kept anywhere, so the tag half of the rule has not been
replayed. A back who is bought on news alone, before any game shows it and before ESPN tags the
lead, is still missed unless Claude's search finds him. And only four flagged backs have been
through an auction here, so their price comes from the twenty breakout claims, not from their own.

The report now reads the 200 most rostered free agents (`POOL_SIZE`), up from 75. The shorter
list ended near 3% rostered, above a receiver and a back the league went on to pay $28 and $11 for.

## What gets listed, and when

Nearly every claim in this league is decided in one run, Wednesday at 3am: 112 of the first 131
auctions and $469 of the $496 spent. A player nobody claims then becomes a free agent anyone can
add at once, for nothing, until his next game starts. ESPN says which state each player is in,
and the report shows `add now` in place of a bid for one who has cleared.

Only players worth a bid are listed. When nobody added to your lineup, the list used to fill up
with the most widely rostered free agents at +0.0 and $0-1, which is how Justin Herbert sat on it
for weeks; on Nov 16 seven of the eight rows were like that. Now a player is listed if he helps
this week, over the next five, or by 15 points or more over the season, or if he is a next man
up or Jev rates him with confidence. On a day with nobody, the report says so.

A kicker, a defense, or a player who fills one later week and nothing else is not listed until
the week he is needed (`held` in `waivers.py`). There will be one like him on the wire that
week, and until then he costs a roster spot. Before this, kickers and defenses took 37% of the
list. A player who helps this week, helps in more than one week, makes your lineup better over
the season, or is a next man up is never held. The
"Lineup holes" block still names every hole in the window, so you can see them coming. Claude
is told the same: a hole in a later week is not a claim today, and a kicker or defense replaces
the one you have and never costs a player at another position.

Two things made the reports from Thursday night to Tuesday morning wrong, and Monday's most of
all, because ESPN stays on the finished week until Tuesday. A free agent whose game had already
been played still counted as able to score this week; now he is locked once his game kicks off.
And players who had played were kept locked in their slots when later weeks were valued, so the
report saw holes your bench could fill; now nobody is locked in a later week. Tuesday's report,
the one before the Wednesday run, was not affected. One Monday quirk remains: the snap-share
column reads `out` for the week just played until the public snap counts are published later
that day.

## Bids

A bid is priced off what it took to win past claims of the same kind in this league. ESPN returns
the losing bids with the winner once a run is over, so every past claim says what a new bid would
have had to beat: the winning price, or the runner-up where the winner was you. `bids.py` sorts
the claims into three kinds. Through week 10 of 2026:

| Kind of claim | Claims | Won half | Won three in four | Won nine in ten |
| --- | ---: | ---: | ---: | ---: |
| A back or receiver after a breakout game, or a next man up | 20 | $8 | $18 | $31 |
| Anyone else at quarterback, back, receiver or tight end | 62 | $3 | $5 | $12 |
| A kicker or a defense | 56 | $2 | $4 | $7 |

Each figure is the smallest bid that would have beaten that share of the claims, and the report
prints the current table under its candidates on every run. A player who would start for you gets
the range from three in four to nine in ten. A stash gets half to three in four, and so does any
kicker or defense. Those odds are `STARTER` and `STASH`: losing one claim in four on a starter and
one in two on a stash was the target, and the high end is there for the claim you can't miss.

A breakout game is at least 10 points and at least 5 more than he had averaged before it, in the
game before the claim, for a back or a receiver. It is the one thing visible beforehand that told
the expensive claims apart: 14 of the 20 claims that drew three or more teams followed one, and 6
of the 62 that drew one or two did. The add rate doesn't (Waiver auctions, below). The old rule
priced a claim by how much the player helped you, off every winning price pooled together, so a
useful receiver was $3-6 whoever else wanted him: Caleb Douglas and Tank Dell were listed at that
and went for $11 and $14.

The test was to walk forward through the season, pricing each claim only from the ones before it.
The three-in-four bid won 36 of 47 ordinary claims and 33 of 41 kicker and defense claims. For
breakout claims there were only ten to test (weeks 7-10): it won six, and three of the four misses
came in week 10, when four ran at once and none went for less than $11. The nine-in-ten bid won
all ten. So read the low end for an in-demand player as a floor.

What this can't do:

- See a claim the box score doesn't explain. Six of the twenty contested claims had no breakout
  game behind them, and two of those players hadn't played that week at all; news is the likely
  reason. Claude is told to price a player off the in-demand line when its search shows he is that
  week's most wanted pickup.
- Tell you he is worth it. Backs and receivers who cost $10 or more averaged 6.9 points a game
  over their next four weeks, against 5.2 for the cheaper ones, and one in five reached 10 a
  game. A breakout game prices a claim; the lineup gain is still the reason to make one.
- Set the budget. What a claim costs is decided by the other thirteen teams, so a bid isn't
  scaled by what you have left, only capped by it. The report's second line shows what is left
  per week of waivers and what you have spent per week so far, because FAAB left after the last
  week buys nothing. At the start of week 11 that read $9 a week left against $4 a week spent.

`python3 auctions.py` checks it as the season goes: of the claims the report had priced the
morning before, over the last 28 days, how many each end of its range would have won.

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

## Lineup projections

`lineup.py` starts whoever ESPN projects highest that week. `lineup_test.py` asks whether anything
else would have scored more. It replays every team in the league, every finished week, from the
rosters as they stood: what the manager started, the best lineup by ESPN's final projection, the
best by each player's level (`values.py`), the best by the average of the two, and the best with
hindsight. Each lineup is scored on what its starters actually did, and every roster row goes to
`logs/lineups.csv` (or `lineups_2025.csv`) so other methods can be tried on the same weeks.

Run on Nov 16, 2026 over all of 2025 and weeks 1-9 of 2026 (364 team-weeks), in points a week
against the lineup ESPN's projection sets, with one standard error:

| Lineup | 2025 | 2026 |
| --- | ---: | ---: |
| As the managers set them | -1.3 ±0.5 | -0.1 ±0.4 |
| By level | -3.6 ±0.6 | +0.5 ±0.7 |
| Half ESPN, half level | -0.3 ±0.4 | +0.3 ±0.5 |
| With hindsight | +12.5 | +11.3 |

Nothing beats ESPN's number, so `lineup.py` keeps it. Tried on the same logs, outside this repo:
other weights on the level, the level as a tie-break between players ESPN has close together, a
correction for ESPN's bias by position, a level built from usage-based expected points
(ffopportunity), and FantasyPros' expert rankings as DynastyProcess archives them each Friday,
alone and averaged with ESPN. The best two, the tie-break and the average with the experts, came
out about half a point a week ahead (the average: +0.3 ±0.4 in 2025, +1.0 ±0.4 in 2026). That is
around two standard errors for the best of some fifteen tries, which is what chance produces, and
too little to take on a second data source for. A first look at single players instead of
lineups, on `logs/accuracy*.csv`, said the same: ESPN missed by 5.4 to 5.7 points a player, a level
by 5.7 to 5.9, and fitted blends landed within 0.1 of ESPN.

What "the projections didn't pan out" looks like in these numbers: a started lineup finishes 15
points from its projected total in an average week, and 20 or more away in three weeks out of
ten. ESPN's lineup scored 89 to 90% of what the best lineup scored with hindsight. The other 11 to
12 points a week sat on benches all over the league, and no method tested got back more of it
than chance would.
ESPN also runs 2 to 3 points a week high for everyone, which doesn't change who starts.

On your own team: in 2025, before the bot, your lineups trailed ESPN's by 4.9 a week (±2.2). In
2026, set by the bot, they were ESPN's lineup in eight weeks of nine, so the Mac being asleep on
game days has not cost lineup points so far. They also scored under their projection in six of
those nine weeks, by 7 a week over the nine, with 14 a week left on your bench against 11 for the
league. That is a cold run, not a method to fix: weeks 3 and 4 alone left 29 and 37.

Worth running again at the end of the season, with twice the 2026 weeks. The average with the
experts is not in the script, so it would have to be rerun from their archive the same way; if it
is still half a point or more ahead then, it is worth wiring in.

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
and TE through week 8, the player's last game averaged 11.3 points on 10.1 carries plus targets,
against 3.0 and 3.2 before it. That became the breakout rule the bids are now priced with (Bids,
above).

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
- **The waiver email has no write-up, or arrives an hour late.** Through Nov 15, 30 of 53 reports
  went out without one. On 24 of them the log says the call to Claude failed after 900 to 6,300
  seconds, against a 120-second timeout: the Mac went back to sleep in the middle of the call,
  and the call died when it next woke, which is also when the email went. The call is now tried
  three times, so a report that wakes up late still gets its write-up, and an email with none
  says so. Getting it at 6am needs the Mac awake for the few minutes the run takes; see
  Scheduling. Four of the rest, and one since, were the connection to `api.anthropic.com` being
  reset at once on every try; three of those five came on weekday mornings between 9 and 11. If
  that happens, run
  `curl -sS -o /dev/null -w '%{http_code}\n' https://api.anthropic.com/v1/messages` on the same
  network: any three-digit number means the API is reachable, and a reset means that network is
  blocking it.
