"""Who just took over a job: the back behind a lead runner who is down.

A level (values.py) needs three or four games to notice a new role, and the league bids after one.
This reads the change straight off the workload instead: carries plus targets, game by game. When
a team's lead back missed or left its latest game, or is tagged out of the next one, the back who
took the work (or is next in line) is flagged.

Replayed on 2025 and 2026 (public results, box scores only), the 49 backs flagged this way averaged
9.4 points a game over their next four weeks, where their levels said 7.0. Just under half reached
10 a game and a quarter stayed under 5. Nothing told the hits from the misses beforehand: not the
back's own level, the lead's, or how much work he got in the game. So every flagged back is valued
at the same FILL_IN for those weeks. A bigger workload on its own, with no lead back down,
predicted nothing a level didn't already say, so it isn't a rule here.

The rule that an injury tag alone is enough (DOWN) could not be replayed, since past tags aren't
kept anywhere. It is there for a lead who is ruled out without a game showing it.

The rule functions are pure and work on plain dicts, so the same code runs on ESPN's data and on
past seasons.
"""
from datetime import timedelta

from lineup import CANT_PLAY

BEFORE = 3              # games before the latest that define a player's usual workload
LEAD_TOUCHES = 12       # carries plus targets a game over those games to count as a lead back...
LEAD_SHARE = 0.45       # ...holding at least this share of his backfield's work
LEFT = 0.35             # under this share of his usual work in the latest game: he missed it or left it
DOWN = CANT_PLAY | {"DOUBTFUL"}                 # tags that say the lead won't play the next one
GAME_OVER = timedelta(hours=5)                  # after kickoff, by when a game's stats are final
FILL_IN = 9.4           # points a game a flagged back is valued at...
FILL_IN_WEEKS = 4       # ...for this many weeks after the game that flagged him


def touches(card, season, weeks):
    """{week: carries plus targets} for the listed weeks he played, from an ESPN player card."""
    found = {}
    for line in card.get("stats", []):
        if (line.get("statSourceId") == 0 and line.get("seasonId") == season
                and line.get("statSplitTypeId") != 2 and line.get("stats")
                and line.get("scoringPeriodId") in weeks):
            raw = line["stats"]
            found[line["scoringPeriodId"]] = int(raw.get("23", 0) + raw.get("58", 0))   # carries, targets
    return found


def finished_weeks(week, byes, games, now):
    """{pro_team: the weeks it has finished, oldest first}. byes and games as ESPN.bye_weeks and
    ESPN.pro_games give them. This week counts once the team's game is over, which is what lets a
    Monday morning see Sunday."""
    teams = {}
    for team, off in byes.items():
        done = [played for played in range(1, week) if played not in off]
        game = games.get(team)
        if game and game[1] and game[1] + GAME_OVER < now:
            done.append(week)
        teams[team] = done
    return teams


def _work(player, weeks):
    return sum(player["touches"].get(week, 0) for week in weeks)


def backfields(players):
    """{pro_team: its running backs}."""
    rooms = {}
    for player in players:
        if player["position"] == "RB":
            rooms.setdefault(player["pro_team"], []).append(player)
    return rooms


def lead_back(backs, weeks):
    """(the back who carried this backfield over the games before the latest, his touches a game).

    None for a committee, where there is nobody's job to inherit, and before a team's third game.
    weeks: the team's finished games, oldest first.
    """
    if len(weeks) < 3:
        return None
    before = weeks[-1 - BEFORE:-1]
    lead = max(backs, key=lambda b: _work(b, before), default=None)
    if lead is None:
        return None
    usual = _work(lead, before) / len(before)
    if usual < LEAD_TOUCHES or _work(lead, before) < LEAD_SHARE * sum(_work(b, before) for b in backs):
        return None
    return lead, usual


def next_man_up(backs, weeks):
    """The back stepping in for one team's lead runner, or None.

    The lead is down if the latest game shows it (he missed it or left early) or his injury tag
    says so. If the game shows it, the next man is whoever took the work in it; if only the tag
    does, it's the back next in line.
    """
    found = lead_back(backs, weeks)
    if found is None:
        return None
    lead, usual = found
    latest, before = weeks[-1], weeks[-1 - BEFORE:-1]
    left = lead["touches"].get(latest, 0) < LEFT * usual
    tagged = lead["injury_status"] in DOWN
    if not (left or tagged):
        return None
    others = [b for b in backs if b is not lead and b["injury_status"] not in CANT_PLAY]
    order = (lambda b: b["touches"].get(latest, 0)) if left else (lambda b: _work(b, before))
    heir = max(others, key=order, default=None)
    if heir is None or order(heir) == 0:
        return None
    return {"player": heir, "lead": lead, "week": latest, "lead_usual": usual,
            "lead_latest": lead["touches"].get(latest, 0), "latest": heir["touches"].get(latest, 0),
            "usual": _work(heir, before) / len(before)}


def flags(players, team_weeks):
    """Every next man up among these players. team_weeks: {pro_team: finished weeks, oldest first}.

    Pass every running back you know of, rostered or not: a lead back on someone's roster is what
    makes the free agent behind him interesting.
    """
    found = [next_man_up(backs, team_weeks.get(team, [])) for team, backs in backfields(players).items()]
    return [flag for flag in found if flag]


def mark(free_agents, others, cards, season, team_weeks):
    """Tag every free agent who is a next man up, in place, and return them.

    others: the backs who aren't available (yours and your rivals'), needed because the lead
    back is nearly always on someone's roster. cards: {player_id: ESPN player card}.
    Each tagged back gets a role (who he is behind, and the workloads that say so) and a fill_in,
    {week: points}, that waivers.projected_for uses in place of a lower projection.
    """
    backs = [p for p in list(free_agents) + list(others) if p["position"] == "RB"]
    for back in backs:
        back["touches"] = touches(cards.get(back["id"], {}), season, team_weeks.get(back["pro_team"], ()))
    available = {id(p) for p in free_agents}
    found = []
    for flag in flags(backs, team_weeks):
        back, lead = flag["player"], flag["lead"]
        if id(back) not in available:
            continue
        back["role"] = {"lead": lead["name"], "lead_status": lead["injury_status"], "week": flag["week"],
                        "touches": flag["latest"], "usual": flag["usual"],
                        "lead_touches": flag["lead_latest"], "lead_usual": flag["lead_usual"]}
        back["fill_in"] = {week: FILL_IN for week in range(flag["week"] + 1, flag["week"] + 1 + FILL_IN_WEEKS)}
        found.append(back)
    return found


def describe(back):
    """One line on why he is flagged, for the reports and for Claude."""
    role = back["role"]
    if role["lead_touches"] == 0:
        what = f"missed week {role['week']}"
    elif role["lead_touches"] < LEFT * role["lead_usual"]:
        what = f"left week {role['week']} after {role['lead_touches']} touches (usually {role['lead_usual']:.0f})"
    else:
        what = f"is tagged {role['lead_status'].replace('_', ' ').lower()}"
    return (f"{back['name']} is next up behind {role['lead']}, who {what}. He had {role['touches']} carries "
            f"and targets in week {role['week']} (usually {role['usual']:.0f}).")
