"""Does the level in values.py beat ESPN's season projection at saying what a player scores next?

For every player on a roster in this league or near the top of the wire, and every week he played:
what ESPN's season projection per game said he would average over his next four weeks, what his
level said, and what he did. It also checks the one thing the filter has to get right about ESPN's
data, that a week he sat out isn't read as a zero.

Two cautions. The players are the ones rostered or wanted today, so the ones who beat their
projection are over-represented, which flatters the level. And ESPN's season projection is read as
it stands today, so wherever ESPN did revise one, the old number looks better than it was.

Usage: python3 value_test.py
"""
import sys
from collections import defaultdict

import values
from espn import ESPN, ESPNError, parse_roster_entry
from waivers import LOOKAHEAD

WIRE = 150                      # free agents to include, most rostered first
ORDER = ["QB", "RB", "WR", "TE", "K", "D/ST"]
PRIOR_CHECKS = (2, 4, 8, 16, 32)   # games the preseason number could count for, to choose PRIOR_GAMES from
APART = 3.0                     # points a game between the level and ESPN's number that would change a ranking


def collect(espn):
    """(week, players): each with his finished games and how many games ESPN says he has played."""
    league = espn.league("mSettings", "mTeam")
    week = league["scoringPeriodId"]
    mine = espn.my_team(league["teams"])["id"]
    players = []
    for team in espn.league("mRoster", week=week)["teams"]:
        for entry in team["roster"]["entries"]:
            players.append(dict(parse_roster_entry(entry, week, espn.season), mine=team["id"] == mine))
    players += [dict(p, mine=False) for p in espn.free_agents(week, WIRE)]
    cards = espn.player_cards([p["id"] for p in players], 18)
    found = []
    for player in players:
        card = cards.get(player["id"])
        if card is None or not player["season_projection"]:
            continue
        season_line = next((s for s in card.get("stats", []) if s.get("seasonId") == espn.season
                            and s.get("scoringPeriodId") == 0 and s.get("statSourceId") == 0), {})
        played = (season_line.get("stats") or {}).get("210")     # ESPN stat 210: games played
        found.append(dict(player, games=values.games_played(card, espn.season, week),
                          counted=len(values.games_played(card, espn.season, week + 1)),
                          espn_games=None if played is None else int(played)))
    return week, found


def compare(players, prior_games=values.PRIOR_GAMES, apart=0.0):
    """{position: (player-weeks, ESPN's average miss, the level's average miss)}, in points a game.

    apart keeps only the player-weeks where the two numbers disagreed by at least that much.
    """
    misses = defaultdict(lambda: ([], []))
    for player in players:
        prior = player["season_projection"] / values.GAMES_PER_SEASON
        games = player["games"]
        for index, (week, _) in enumerate(games):
            ahead = [points for later, points in games if week < later <= week + LOOKAHEAD]
            if not ahead:
                continue
            actual = sum(ahead) / len(ahead)
            estimate = values.level(prior, [points for _, points in games[:index + 1]], prior_games)
            if abs(estimate - prior) < apart:
                continue
            misses[player["position"]][0].append(abs(prior - actual))
            misses[player["position"]][1].append(abs(estimate - actual))
    mean = lambda numbers: sum(numbers) / len(numbers)
    return {position: (len(espn), mean(espn), mean(ours)) for position, (espn, ours) in misses.items()}


def overall(summary):
    """The level's average miss across the positions it is used for, weighted by player-weeks."""
    rated = [summary[position] for position in values.FILTERED if position in summary]
    weeks = sum(n for n, _, _ in rated)
    return sum(n * ours for n, _, ours in rated) / weeks if weeks else None


def miscounted(players):
    """(counted more weeks than ESPN says he played, counted fewer). The first kind are the harmful
    ones: a week he missed that would be read as a game he scored nothing in."""
    known = [p for p in players if p["espn_games"] is not None]
    return ([p for p in known if p["counted"] > p["espn_games"]],
            [p for p in known if p["counted"] < p["espn_games"]], len(known))


def report(week, players):
    lines = [f"Level against ESPN's season projection, weeks 1-{week - 1}, {len(players)} players", "",
             f"Average miss on what a player scored a game over his next {LOOKAHEAD} weeks:",
             f"{'POS':<5} {'WEEKS':>6} {'ESPN SEASON/17':>15} {'LEVEL':>7} {'CHANGE':>8}"]
    summary = compare(players)
    for position in ORDER:
        if position not in summary:
            continue
        n, espn, ours = summary[position]
        unused = "" if position in values.FILTERED else "   not used: the report keeps ESPN's number here"
        lines.append(f"{position:<5} {n:>6} {espn:>15.2f} {ours:>7.2f} {100 * (ours - espn) / espn:>+7.0f}%{unused}")

    shown = [position for position in ORDER if position in summary]
    used = [position for position in shown if position in values.FILTERED]
    grid = {games: compare(players, games) for games in PRIOR_CHECKS}
    mark = lambda games: f"{games} games" + ("*" if games == values.PRIOR_GAMES else "")
    lines += ["", "How much the preseason number should count for (the level's average miss; * is what values.py uses):",
              f"{'POS':<5} {'ESPN ONLY':>10} " + " ".join(f"{mark(games):>10}" for games in PRIOR_CHECKS)]
    for position in shown:
        lines.append(f"{position:<5} {summary[position][1]:>10.2f} "
                     + " ".join(f"{grid[games][position][2]:>10.2f}" for games in PRIOR_CHECKS))
    if used:
        weeks = sum(summary[position][0] for position in used)
        lines.append(f"{'USED':<5} {sum(summary[p][0] * summary[p][1] for p in used) / weeks:>10.2f} "
                     + " ".join(f"{overall(grid[games]):>10.2f}" for games in PRIOR_CHECKS)
                     + "   " + ", ".join(used) + " together")

    split = compare(players, apart=APART)
    lines += ["", f"Only where the level and ESPN's number are {APART:.0f} or more points a game apart, which is",
              "where the choice between them changes a ranking:",
              f"{'POS':<5} {'WEEKS':>6} {'ESPN SEASON/17':>15} {'LEVEL':>7} {'CHANGE':>8}"]
    for position in shown:
        if position in split:
            n, espn, ours = split[position]
            lines.append(f"{position:<5} {n:>6} {espn:>15.2f} {ours:>7.2f} {100 * (ours - espn) / espn:>+7.0f}%")

    more, fewer, known = miscounted(players)
    lines += ["", f"Missed games: for {known - len(more) - len(fewer)} of {known} players the weeks counted match "
                  "ESPN's games-played stat."]
    if not known:
        lines.append("  ESPN sent no games-played stat, so this couldn't be checked.")
    for label, group in (("counted MORE weeks than he played, so a missed game reads as a zero", more),
                         ("counted fewer weeks than he played", fewer)):
        if group:
            lines.append(f"  {len(group)} {label}: " + ", ".join(
                f"{p['name']} ({p['counted']} vs {p['espn_games']})" for p in group[:6]))

    moved = sorted((p for p in players if p["position"] in values.FILTERED and p["games"]),
                   key=lambda p: -abs(_level(p) - p["season_projection"] / values.GAMES_PER_SEASON))
    lines += ["", "Furthest from ESPN's number (* is on your roster):",
              f"  {'PLAYER':<22} {'POS':<4} {'GP':>3} {'SEASON/17':>10} {'LEVEL':>6} {'ACTUAL/G':>9}"]
    for p in moved[:15]:
        scores = [points for _, points in p["games"]]
        lines.append(f"{'*' if p['mine'] else ' '} {p['name'][:22]:<22} {p['position']:<4} {len(scores):>3} "
                     f"{p['season_projection'] / values.GAMES_PER_SEASON:>10.1f} {_level(p):>6.1f} "
                     f"{sum(scores) / len(scores):>9.1f}")
    return "\n".join(lines)


def _level(player):
    return values.level(player["season_projection"] / values.GAMES_PER_SEASON,
                        [points for _, points in player["games"]])


if __name__ == "__main__":
    try:
        print(report(*collect(ESPN.from_env())))
    except ESPNError as error:
        sys.exit(f"Error: {error}")
