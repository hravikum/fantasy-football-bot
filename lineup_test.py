"""Would lineups set by something other than ESPN's weekly projection have scored more?

lineup.py starts whoever ESPN projects highest. This replays every team in the league, every
finished week: the roster as it stood, each player's ESPN projection for that week, and what he
scored. The same roster is set several ways, and each lineup is scored on what its starters did:

  as set      what the manager actually started (on your team: the bot's moves and your own)
  ESPN        the best lineup by ESPN's weekly projection, which is what lineup.py aims for
  level       quarterbacks, backs and receivers ranked by their level (values.py) instead
  half        by the average of the two
  hindsight   the best lineup knowing the scores, which no projection can reach

Four cautions. ESPN's projection for a past week is its last one before kickoff, with late
scratches already at zero, so "ESPN" is ESPN at its best: a lineup set on Tuesday didn't have that
number. Past injury tags aren't kept, so a player ESPN projected for nothing (out, or on bye) is
treated as unavailable to every method, which the bot would have known. A level knows nothing about
this week (the opponent, a limited role, an injury he is playing through) and ESPN's number does.
And tight ends, kickers and defenses have no level, so every method picks them by ESPN's number.

Every roster row is written to logs/lineups.csv, so other methods can be tried on the same weeks.

Usage: python3 lineup_test.py [season]     a past season, e.g. 2025, writes its own log
"""
import csv
import sys
from collections import defaultdict
from pathlib import Path

import values
from espn import BENCH, ESPN, IR, ESPNError, parse_roster_entry
from lineup import plan

LOG = Path(__file__).with_name("logs") / "lineups.csv"
FIELDS = ["week", "team_id", "mine", "player_id", "player", "position", "slot", "projected", "actual", "level"]
PLAYING = {1: (0, None)}        # every player is put on a team with a game; who can't play is decided by available()
METHODS = {"espn": "By ESPN's weekly projection", "level": "By level (QB, RB, WR)",
           "half": "Half ESPN, half level", "hindsight": "With hindsight"}


def collect(espn):
    """(last finished week, lineup slots, rows): one row for every player on a roster in each finished week.

    A level is the one he had going into that week: his games before it and none after.
    """
    league = espn.league("mSettings", "mTeam")
    try:
        mine = espn.my_team(league["teams"])["id"]
    except ESPNError:
        mine = None     # a season you weren't in: the league-wide comparison still stands
    slot_counts = {int(slot): count for slot, count in league["settings"]["rosterSettings"]["lineupSlotCounts"].items()}
    final = (league.get("status") or {}).get("finalScoringPeriod") or league["scoringPeriodId"]
    last = min(league["scoringPeriodId"] - 1, final)
    rows = []
    for week in range(1, last + 1):
        for team in espn.league("mRoster", week=week).get("teams", []):
            for entry in team.get("roster", {}).get("entries", []):
                player = parse_roster_entry(entry, week, espn.season)
                rows.append(dict(player, week=week, team_id=team["id"], mine=team["id"] == mine, level=None))
    cards = espn.player_cards(sorted({row["id"] for row in rows if row["position"] in values.FILTERED}), 18)
    for row in rows:
        card = cards.get(row["id"])
        prior = row["season_projection"] or (card and _season_projection(card, espn.season))
        if card is not None and prior:
            games = values.games_played(card, espn.season, row["week"])
            row["level"] = values.level(prior / values.GAMES_PER_SEASON, [points for _, points in games],
                                        values.PRIOR_GAMES[row["position"]])
    return last, slot_counts, rows


def _season_projection(card, season):
    """ESPN's season projection from a player card, for a roster row that came without one."""
    return next((line.get("appliedTotal") for line in card.get("stats", [])
                 if line.get("seasonId") == season and line.get("scoringPeriodId") == 0
                 and line.get("statSourceId") == 1), None)


def available(row):
    """Whether he could have been started: ESPN projected him for something that week."""
    return bool(row["projection"]) and row["projection"] > 0


def worth(row, method):
    """What a method says he would score that week."""
    if method == "hindsight":
        return row["actual"] or 0.0
    if not available(row):
        return 0.0
    if method == "espn" or row["level"] is None:
        return row["projection"]
    return row["level"] if method == "level" else (row["projection"] + row["level"]) / 2


def best_lineup(roster, slot_counts, method):
    """(points its starters actually scored, who they were) for the best lineup a method would set."""
    view = [dict(row, slot=BENCH, locked=False, injury_status="ACTIVE", pro_team=1, projection=worth(row, method))
            for row in roster]
    target, _ = plan(view, slot_counts, PLAYING)
    starters = frozenset(row["id"] for row in roster if target[row["id"]] not in (BENCH, IR))
    return sum(row["actual"] or 0.0 for row in roster if row["id"] in starters), starters


def score(rows, slot_counts):
    """One result per team per week: {"week", "team_id", "mine", "as_set", and each method: (points, starters)}."""
    rosters = defaultdict(list)
    for row in rows:
        rosters[(row["week"], row["team_id"])].append(row)
    results = []
    for (week, team_id), roster in sorted(rosters.items()):
        started = frozenset(row["id"] for row in roster if row["slot"] not in (BENCH, IR))
        result = {"week": week, "team_id": team_id, "mine": roster[0]["mine"],
                  "as_set": (sum(row["actual"] or 0.0 for row in roster if row["id"] in started), started)}
        for method in METHODS:
            result[method] = best_lineup(roster, slot_counts, method)
        results.append(result)
    return results


def against_espn(results, method):
    """(points a week, how far that is from ESPN's lineup, the standard error of the gap, weeks the lineup differed)."""
    gaps = [result[method][0] - result["espn"][0] for result in results]
    n = len(gaps)
    mean = sum(gaps) / n
    error = (sum((gap - mean) ** 2 for gap in gaps) / (n - 1) / n) ** 0.5 if n > 1 else None
    differed = sum(1 for result in results if result[method][1] != result["espn"][1])
    return sum(result[method][0] for result in results) / n, mean, error, differed


def _table(results):
    lines = [f"{'':<30} {'POINTS A WEEK':>13} {'AGAINST ESPN':>13} {'':>6} {'LINEUP DIFFERED':>16}"]
    for method, label in [("as_set", "As set")] + list(METHODS.items()):
        points, gap, error, differed = against_espn(results, method)
        if method == "espn":
            lines.append(f"{label:<30} {points:>13.1f}")
            continue
        margin = "" if error is None or method == "hindsight" else f"±{error:.1f}"
        lines.append(f"{label:<30} {points:>13.1f} {gap:>+13.1f} {margin:>6} {f'{differed} of {len(results)}':>16}")
    return lines


def report(last, results):
    lines = [f"Lineups set five ways, weeks 1-{last}", ""]
    if not results:
        return "\n".join(lines + ["Nothing has finished yet."])
    teams = len({result["team_id"] for result in results})
    lines += [f"Every team ({teams} teams, {len(results)} team-weeks):"] + _table(results)
    mine = [result for result in results if result["mine"]]
    if mine:
        lines += ["", f"Your team ({len(mine)} weeks):"] + _table(mine)
        lines += ["", f"{'WEEK':<5} {'AS SET':>7} {'ESPN':>7} {'LEVEL':>7} {'HALF':>7} {'HINDSIGHT':>10}"]
        for result in mine:
            lines.append(f"{result['week']:<5} {result['as_set'][0]:>7.1f} {result['espn'][0]:>7.1f} "
                         f"{result['level'][0]:>7.1f} {result['half'][0]:>7.1f} {result['hindsight'][0]:>10.1f}")
    lines += ["", "AGAINST ESPN is the points a week a method scored over the lineup ESPN's projection would have set,",
              "and ± is one standard error: a gap inside two of them is not a difference. As set is what was",
              "actually started. Hindsight is the ceiling: what the best lineup scored once the games were known."]
    return "\n".join(lines)


def write_log(rows, path=None):
    path = path or LOG
    path.parent.mkdir(exist_ok=True)
    with open(path, "w", newline="") as handle:          # rewritten each run: ESPN keeps the history
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        for row in rows:
            writer.writerow({"week": row["week"], "team_id": row["team_id"], "mine": int(row["mine"]),
                             "player_id": row["id"], "player": row["name"], "position": row["position"],
                             "slot": row["slot"], "projected": row["projection"], "actual": row["actual"],
                             "level": None if row["level"] is None else round(row["level"], 2)})
    return len(rows)


def main(season=None):
    espn = ESPN.from_env()
    path = LOG
    if season:   # a past season gets its own log, so this season's stays as it is
        espn.season = season
        path = LOG.with_name(f"lineups_{season}.csv")
    last, slot_counts, rows = collect(espn)
    print(report(last, score(rows, slot_counts)))
    print(f"\n{write_log(rows, path)} roster rows written to {path}")


if __name__ == "__main__":
    try:
        main(int(sys.argv[1]) if len(sys.argv) > 1 else None)
    except ESPNError as error:
        sys.exit(f"Error: {error}")
