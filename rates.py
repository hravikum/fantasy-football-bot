"""A back's or receiver's scoring rate, split into the work he gets and what he does with it.

values.py moves a player's level with every fantasy point he scores, so one long touchdown moves
it as far as a doubled workload. Expected fantasy points (ffopportunity, built on nflverse
play-by-play) price each carry and target by down, distance, depth and field position: what an
average player would score with the same work. Work repeats from game to game; what a player
does with it mostly doesn't. So this keeps two levels, each the same Kalman filter values.py
uses: expected points a game, which moves quickly, and points over expected, which barely moves.
A player's rate is the sum.

Tested in rate_test.py, average miss on what a player scored a game over his next four:
  Replay on nflverse data, last season as the prior, each method's weights fit on 2019-2024 and
    scored on 2025 and 2026: backs 3.70 against the level's 3.75, receivers 3.08 against 3.12.
  The backs and receivers the waiver report sent to Jev in 2026 (logs/jev_scores.csv), with ESPN's
    preseason projection as the prior, as both run live, scored on games that kicked off after
    each run: 2.60 against 2.63, and the rate's order matched what they went on to score better
    (rank correlation 0.29 against 0.21).
  Quarterbacks were worse on that second test, and tight ends keep ESPN's number in values.py
  already, so both are left as they are.

Runs in shadow only for now: shadow.py ranks the wire with it beside the current ranking.
"""
import csv
import io
from datetime import datetime

import snaps
import values
from values import GAMES_PER_SEASON

URL = "https://github.com/ffverse/ffopportunity/releases/download/latest-data/ep_weekly_{season}.csv"
MAX_AGE = 6 * 3600   # ffopportunity rebuilds after each game day

# Half PPR, the stats ffopportunity models. Rebuilt this way, nflverse matched this league's
# ESPN points to the tenth in 97% of rostered player-weeks of 2025 and 2026; the rest are mostly
# fumbles on sacks, which only quarterbacks have.
SCORING = {"pass_yards_gained": 0.04, "pass_touchdown": 4, "pass_interception": -2, "pass_two_point_conv": 2,
           "rush_yards_gained": 0.1, "rush_touchdown": 6, "rush_two_point_conv": 2,
           "receptions": 0.5, "rec_yards_gained": 0.1, "rec_touchdown": 6, "rec_two_point_conv": 2}
FUMBLES = {"rush_fumble_lost": -2, "rec_fumble_lost": -2}   # in what he scored; there's no expected column

# How many games ESPN's preseason projection counts for, measured in each series' own game-to-game
# noise (values.level). Expected points are far steadier than points, so a game of them says more:
# 2 here, where values.py uses 16 for points. Points over expected is mostly noise, so the position's
# average holds for a long time. Fit in rate_test.py.
OPPORTUNITY_GAMES = {"RB": 2, "WR": 2}
EFFICIENCY_GAMES = {"RB": 16, "WR": 32}
# How far the work level can move per game, as in values.py. Letting it move faster, or giving
# ESPN's projection less weight, so a jump in work shows sooner, was no better on the replay and
# worse on the wire, worst of all in players' first two games: most jumps don't last
# (rate_test.py, part 3).
WORK_DRIFT = values.DRIFT
# Points over expected a game for the average player at the position, 2019-2024.
EFFICIENCY_BASE = {"RB": 0.0, "WR": 0.2}
RATED = set(OPPORTUNITY_GAMES)
# A player counts as playing if he played in at least one of his team's last this-many games. The
# rate counts only games he plays, so without this a back hurt or benched in week 7 keeps his
# week-7 workload indefinitely.
RECENT = 2


def _number(value):
    try:
        return float(value) if value not in ("", "NA") else 0.0
    except ValueError:
        return 0.0


def game_points(row):
    """(expected, scored) half-PPR points for one row of ffopportunity's weekly file."""
    expected = sum(weight * _number(row.get(f"{stat}_exp")) for stat, weight in SCORING.items())
    scored = sum(weight * _number(row.get(stat)) for stat, weight in {**SCORING, **FUMBLES}.items())
    return expected, scored


def kickoffs(schedule_text):
    """{game id: kickoff}, local (Eastern) time as nflverse's schedule gives it."""
    found = {}
    for row in csv.DictReader(io.StringIO(schedule_text)):
        if row.get("gameday") and row.get("gametime"):
            found[row["game_id"]] = datetime.fromisoformat(f"{row['gameday']}T{row['gametime']}")
    return found


def join(players_text, weekly_text, schedule_text=""):
    """({espn_id: {week: (expected, scored, kickoff)}}, {espn_id: weeks his team's game is in the
    file}, latest week).

    nflverse keys players by GSIS id; its player table maps those to ESPN ids (as in snaps.py).
    A week the file has his team's game but no row for him is a game with no carries or targets;
    a week it doesn't have his team's game yet (Monday night's, say) says nothing. Kickoff comes
    from nflverse's schedule (None without it) and is only for judging which games came after a run.
    """
    starts = kickoffs(schedule_text) if schedule_text else {}
    to_espn = {}
    for row in csv.DictReader(io.StringIO(players_text)):
        espn_id = snaps._espn_id(row.get("espn_id"))
        if espn_id and row.get("gsis_id"):
            to_espn[row["gsis_id"]] = espn_id
    found, team_of, covered = {}, {}, {}
    for row in csv.DictReader(io.StringIO(weekly_text)):
        week = int(_number(row.get("week")))
        covered.setdefault(week, set()).add(row.get("posteam"))
        espn_id = to_espn.get(row.get("player_id"))
        if espn_id is not None:
            found.setdefault(espn_id, {})[week] = game_points(row) + (starts.get(row.get("game_id")),)
            if week >= team_of.get(espn_id, (0, None))[0]:
                team_of[espn_id] = (week, row.get("posteam"))
    played = {espn_id: {week for week, teams in covered.items() if team in teams}
              for espn_id, (_, team) in team_of.items()}
    return found, played, max(covered, default=0)


def load(season):
    """join() for this season's file."""
    players = snaps._fetch(f"{snaps.BASE}/players/players.csv", snaps.CACHE / "players.csv", snaps.PLAYERS_MAX_AGE)
    weekly = snaps._fetch(URL.format(season=season), snaps.CACHE / f"ep_weekly_{season}.csv", MAX_AGE)
    schedule = snaps._fetch(f"{snaps.BASE}/schedules/games.csv", snaps.CACHE / "games.csv", MAX_AGE)
    return join(players, weekly, schedule)


def rate(prior, games, weeks, played, position):
    """(points a game from here on, the work part, the what-he-does-with-it part).

    prior is ESPN's preseason projection a game; games is [(week, points)] in this league's
    scoring, oldest first (values.games_played); weeks is his {week: (expected, scored)} and
    played the weeks the file has his team's game (join). A game the file doesn't have yet is left
    out until it does: counting its raw points as work would make one big game look like a new role.
    """
    base = EFFICIENCY_BASE[position]
    counted = counted_games(games, weeks, played)
    work = [expected for expected, _ in counted]
    beyond = [points - expected for expected, points in counted]
    opportunity = values.level(prior - base, work, OPPORTUNITY_GAMES[position], WORK_DRIFT)
    efficiency = values.level(base, beyond, EFFICIENCY_GAMES[position])
    return opportunity + efficiency, opportunity, efficiency


def counted_games(games, weeks, played):
    """[(expected, points)] for the games the file covers, oldest first; see rate()."""
    return [(weeks.get(week, (0.0, 0.0))[0], points) for week, points in games
            if week in weeks or week in played]


def recently_played(games, weeks, played, week):
    """Whether he played in one of his team's last RECENT games before this week.

    His games are ESPN's (a week with stats) and the file's (a week with a carry or target);
    his team's are the weeks the file has its game. With no team games yet, he gets the benefit.
    """
    team_games = sorted(w for w in played if w < week)[-RECENT:]
    his = {w for w, _ in games} | set(weeks)
    return not team_games or any(w in his for w in team_games)


def attach(players, cards, season, week, data):
    """Give each back and receiver with games and expected points his rate, in place.

    data is load()'s result. Anyone else, or anyone the expected-points file doesn't know, gets no
    rate and is valued at his level, as now. Each rated player also gets active: whether he has
    played lately (recently_played). Returns how many got a rate.
    """
    by_player, played, _ = data
    rated = 0
    for player in players:
        card, weeks = cards.get(player["id"]), by_player.get(player["id"])
        if player["position"] not in RATED or card is None or not weeks:
            continue
        prior = (player["season_projection"] or 0.0) / GAMES_PER_SEASON
        games, his_weeks = values.games_played(card, season, week), played.get(player["id"], set())
        total, work, beyond = rate(prior, games, weeks, his_weeks, player["position"])
        expected = [x for x, _ in counted_games(games, weeks, his_weeks)]
        player.update(rate=total, rate_work=work, rate_beyond=beyond,
                      active=recently_played(games, weeks, his_weeks, week),
                      work_recent=sum(expected[-2:]) / len(expected[-2:]) if expected else None,
                      work_average=sum(expected) / len(expected) if expected else None)
        rated += 1
    return rated


def scored_after(weeks, week, logged_at, ahead):
    """What he scored in each game that kicked off after a run in ESPN week `week`, over that week
    and the next ahead - 1. weeks is his {week: (expected, scored, kickoff)} (join).

    Games played before the run are what put many players on the wire that day, so counting them
    would flatter whatever picked them. A game with no kickoff on file counts only if the run came
    before its week's first game (ESPN's week runs Tuesday to Monday; Thursday night is first).
    Used by shadow.py's scorecard and tally and by rate_test.py's wire check.
    """
    when = datetime.fromisoformat(logged_at)
    before_the_week = when.weekday() in (1, 2) or (when.weekday() == 3 and when.hour < 20)
    found = []
    for game_week, row in sorted(weeks.items()):
        if not week <= game_week < week + ahead:
            continue
        kickoff = row[2] if len(row) > 2 else None
        if (kickoff > when) if kickoff else (game_week > week or before_the_week):
            found.append(row[1])
    return found


def by_rate(players):
    """Copies valued by the rate wherever there is one, this week included.

    The rate stands in for the level, which is all a later week reads (season_pace), and for
    ESPN's projection this week, which then no longer starts every list from ESPN's number. It
    doesn't where ESPN has him at zero (a bye, out, inactive) or his game has already started,
    and not at all for a player who hasn't played lately: his rate is old work, so he keeps his
    level as the report has it.
    """
    copies = []
    for player in players:
        copy = dict(player)
        if player.get("rate") is not None and player.get("active", True):
            copy["level"] = player["rate"]
            if (player.get("projection") or 0) > 0 and not player.get("locked"):
                copy["projection"] = player["rate"]
        copies.append(copy)
    return copies
