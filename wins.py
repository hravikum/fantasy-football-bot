"""Points into wins: what a point added to your lineup is worth in a given week of this league.

A regular-season week here is two games: the matchup, and one against the league median (the
bonus win for finishing in the top half of the week's scores). A point is worth most in a week
your lineup is projected close to your opponent's or to the median, and little in a week you're
far ahead or far behind. A playoff week is one game.

Each team's score is taken as normal around its projected lineup, with the spread this league's
weekly scores have shown around each team's own average. Used by shadow.py, to rank the wire by
wins added beside points added.
"""
import math
from collections import defaultdict
from statistics import NormalDist, fmean, median

NORMAL = NormalDist()
SPREAD = 25.0          # points; a team's weekly score around its own average, until the league shows its own
MIN_TEAM_WEEKS = 30    # finished team-weeks needed before the league's own spread is used


def spread(schedule):
    """How far a team's weekly score lands from its own season average, from finished matchups."""
    scores = defaultdict(list)
    for matchup in schedule:
        if matchup.get("winner") in (None, "UNDECIDED"):
            continue
        for side in ("home", "away"):
            team = matchup.get(side) or {}
            if team.get("teamId") is not None and team.get("totalPoints") is not None:
                scores[team["teamId"]].append(team["totalPoints"])
    teams = [points for points in scores.values() if len(points) >= 2]
    count = sum(len(points) for points in teams)
    if count < MIN_TEAM_WEEKS:
        return SPREAD
    squares = sum((p - fmean(points)) ** 2 for points in teams for p in points)
    return math.sqrt(squares / (count - len(teams)))


def opponents(schedule, team_id):
    """{matchup period: the team you play}, as far as ESPN's schedule says."""
    found = {}
    for matchup in schedule:
        home, away = (matchup.get("home") or {}).get("teamId"), (matchup.get("away") or {}).get("teamId")
        if team_id in (home, away) and home is not None and away is not None:
            found[matchup.get("matchupPeriodId")] = away if home == team_id else home
    return found


def week_wins(mine, opponent, others, sd, playoff=False):
    """Expected wins in one week: your projected score against your opponent's and the median's.

    The median here is the middle of the other teams' projections; it moves less than one team's
    score (the spread of a median of n is about sqrt(pi / 2n) of one score's).
    """
    matchup = NORMAL.cdf((mine - opponent) / (sd * math.sqrt(2)))
    if playoff or not others:
        return matchup
    middle_sd = sd * math.sqrt(1 + math.pi / (2 * len(others)))
    return matchup + NORMAL.cdf((mine - median(others)) / middle_sd)


def wins_added(gains, weeks, sd):
    """Wins a player adds across the window. gains is the points he adds each week (score_pool's
    by_week); weeks is, for each, (your projected score, your opponent's, the others', playoff)."""
    return sum(week_wins(mine + gain, opponent, others, sd, playoff) - week_wins(mine, opponent, others, sd, playoff)
               for gain, (mine, opponent, others, playoff) in zip(gains, weeks))
