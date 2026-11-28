"""Odds for the rest of the regular season: who finishes last, and who makes the playoffs.

Every regular-season week left is simulated for every team: the matchup, plus the game against the
league median when ESPN's records count it (wins.py). A team's score is normal around its best
lineup's projection, the way trades.py projects one (ESPN's number this week, the bot's levels
after), with the spread this league's weekly scores have shown (wins.spread). This week counts
what has already been scored, and only the part still to play is uncertain. Standings sort by
record, then by ESPN's tiebreak setting.

The same simulation prices points: what ten more points in a given week would do to your chance
of finishing last. The trade finder (finder.py) uses it.

Known limit: every team is assumed to start its best lineup. A manager who has stopped setting
his scores less than this says.

Usage:
  python3 odds.py           print the odds
  python3 odds.py --post    and post them to Slack
"""
import math
import random
import sys
from collections import Counter, defaultdict
from datetime import datetime
from statistics import median

import mailer
import slack
import trades
import wins
from espn import BENCH, IR, ESPN, ESPNError
from lineup import expected_points, plan
from waivers import lineup_value_and_holes, schedule_ahead, shortfalls

SIMS = 20000
SEED = 2026      # the same draws every run, so a change in the odds is a change in the data
BUMP = 10.0      # points, for "what a point is worth": about one decent player's swing
LOW = 0.75       # a team projecting under this share of its points a game so far is flagged


def periods_left(settings, week):
    """{matchup period: [its scoring weeks from this one on]} for the regular season."""
    schedule = settings["scheduleSettings"]
    mapping = schedule.get("matchupPeriods") or {}
    left = {}
    for period in range(1, schedule["matchupPeriodCount"] + 1):
        weeks = [int(w) for w in mapping.get(str(period)) or [period]]
        if max(weeks) >= week:
            left[period] = [w for w in weeks if w >= week]
    return left


def results(schedule, regular, with_median):
    """From finished regular-season matchups: ({team: [wins, losses, ties, points]},
    {(a, b): a's matchup wins over b}, periods finished). with_median adds each week's game against
    the league median: above it a win, below it a loss."""
    periods = defaultdict(list)
    for m in schedule:
        if (m.get("matchupPeriodId") or 0) <= regular and m.get("home") and m.get("away"):
            periods[m["matchupPeriodId"]].append(m)
    record, h2h, done = defaultdict(lambda: [0, 0, 0, 0.0]), Counter(), 0
    for games in periods.values():
        if any(g.get("winner") in (None, "UNDECIDED") for g in games):
            continue
        done += 1
        scores = {}
        for g in games:
            home, away = g["home"]["teamId"], g["away"]["teamId"]
            scores[home], scores[away] = g["home"].get("totalPoints") or 0.0, g["away"].get("totalPoints") or 0.0
            if g["winner"] == "HOME":
                record[home][0] += 1
                record[away][1] += 1
                h2h[(home, away)] += 1
            elif g["winner"] == "AWAY":
                record[away][0] += 1
                record[home][1] += 1
                h2h[(away, home)] += 1
            else:
                record[home][2] += 1
                record[away][2] += 1
        for team, points in scores.items():
            record[team][3] += points
        if with_median:
            middle = median(scores.values())
            for team, points in scores.items():
                record[team][0 if points > middle else 1 if points < middle else 2] += 1
    return record, h2h, done


def espn_counts_median(teams, done):
    """Whether ESPN's records include the median game: two results a finished week, not one."""
    games = Counter()
    for team in teams:
        overall = (team.get("record") or {}).get("overall") or {}
        games[sum(overall.get(k) or 0 for k in ("wins", "losses", "ties"))] += 1
    return bool(done) and games[2 * done] > games[done]


def this_week_mean(players, slot_counts, games):
    """(points expected from the best lineup this week, share of its projection still to play).
    A locked starter counts what he has scored; the rest count their projection."""
    target, _ = plan(players, slot_counts, games)
    scored = to_come = played = 0.0
    for p in players:
        if target[p["id"]] in (BENCH, IR):
            continue
        if p["locked"]:
            scored += p.get("actual") or 0.0
            played += p.get("projection") or 0.0
        else:
            to_come += expected_points(p, games)
    share = to_come / (to_come + played) if to_come + played > 0 else 0.0
    return scored + to_come, share


def means_for(roster, ctx, weeks):
    """{scoring week: (expected points, share still uncertain, starting slots nobody can fill)}
    for one team's roster."""
    this_week = ctx["week"]
    horizon = dict(schedule_ahead(ctx["games"], ctx["byes"], this_week, lookahead=max(max(weeks) - this_week, 0)))
    found = {}
    for week in weeks:
        if week == this_week:
            holes = sum(shortfalls(roster, ctx["slot_counts"], ctx["games"]).values())
            found[week] = this_week_mean(roster, ctx["slot_counts"], ctx["games"]) + (holes,)
        else:
            ready = trades.in_week(roster, week, this_week, ctx["return_weeks"])
            points, holes = lineup_value_and_holes(ready, ctx["slot_counts"], horizon[week])
            found[week] = (points, 1.0, holes)
    return found


class Model:
    """The rest of the regular season, ready to simulate. Draws are made once, so two runs with
    different inputs differ only by those inputs (common random numbers)."""

    def __init__(self, teams, start, h2h, pairs, means, sd, with_median, rule, spots, sims=SIMS, seed=SEED):
        self.teams = list(teams)                       # team ids
        self.start = start                             # {team: (wins counting ties as half, points for)}
        self.h2h = h2h                                 # {(a, b): matchup wins of a over b}
        self.pairs = pairs                             # {period: [(home, away)]}
        self.means = means                             # {period: {team: (points, share uncertain)}}
        self.sd = sd
        self.with_median = with_median
        self.by_h2h = "H2H" in (rule or "")
        self.spots = spots
        self.periods = sorted(means)
        rng = random.Random(seed)
        self.draws = [[[rng.gauss(0.0, 1.0) for _ in self.teams] for _ in self.periods] for _ in range(sims)]

    def run(self, extra=None, watch=None):
        """{team: (chance of finishing last, chance of a playoff spot)}, and for watch (a team id)
        {period: chance of winning its matchup}. extra is {period: {team: points added}}."""
        extra = extra or {}
        index = {team: i for i, team in enumerate(self.teams)}
        mean = [[self.means[p][t][0] + extra.get(p, {}).get(t, 0.0) for t in self.teams] for p in self.periods]
        spread = [[self.sd * math.sqrt(self.means[p][t][1]) for t in self.teams] for p in self.periods]
        pairs = [[(index[a], index[b]) for a, b in self.pairs.get(p, ()) if a in index and b in index]
                 for p in self.periods]
        last, playoffs, matchup = Counter(), Counter(), Counter()
        n = len(self.teams)
        for draw in self.draws:
            won = [self.start[t][0] for t in self.teams]
            points = [self.start[t][1] for t in self.teams]
            beat = Counter() if self.by_h2h else None
            for k, z in enumerate(draw):
                score = [mean[k][i] + spread[k][i] * z[i] for i in range(n)]
                for a, b in pairs[k]:
                    if score[a] > score[b]:
                        won[a] += 1
                        if beat is not None:
                            beat[(a, b)] += 1
                    elif score[b] > score[a]:
                        won[b] += 1
                        if beat is not None:
                            beat[(b, a)] += 1
                    if watch is not None and index.get(watch) in (a, b):
                        mine, theirs = (a, b) if a == index[watch] else (b, a)
                        matchup[self.periods[k]] += score[mine] > score[theirs]
                if self.with_median:
                    middle = median(score)
                    for i in range(n):
                        won[i] += 1.0 if score[i] > middle else 0.5 if score[i] == middle else 0.0
                for i in range(n):
                    points[i] += score[i]
            order = self.standings(won, points, beat)
            last[self.teams[order[-1]]] += 1
            for i in order[:self.spots]:
                playoffs[self.teams[i]] += 1
        sims = len(self.draws)
        odds = {t: (last[t] / sims, playoffs[t] / sims) for t in self.teams}
        return (odds, {p: c / sims for p, c in matchup.items()}) if watch is not None else odds

    def standings(self, won, points, beat):
        """Indexes best first: record, then head-to-head among the tied when ESPN says so, then points."""
        order = sorted(range(len(won)), key=lambda i: (-won[i], -points[i]))
        if not self.by_h2h:
            return order
        tied = defaultdict(list)
        for i in order:
            tied[won[i]].append(i)
        ranked = []
        for record in sorted(tied, reverse=True):
            group = tied[record]
            within = {i: sum(self.h2h.get((self.teams[i], self.teams[j]), 0) + (beat or {}).get((i, j), 0)
                             for j in group if j != i) for i in group}
            ranked += sorted(group, key=lambda i: (-within[i], -points[i]))
        return ranked


def build(espn, ctx=None, sims=SIMS):
    """(Model, facts for the report). ctx is trades.gather's, when the caller already has it."""
    ctx = ctx or trades.gather(espn)
    league = espn.league("mSettings", "mTeam")
    settings = league["settings"]
    schedule = espn.league("mMatchupScore").get("schedule", [])
    regular = settings["scheduleSettings"]["matchupPeriodCount"]
    teams = [t["id"] for t in league["teams"]]

    done = results(schedule, regular, False)[2]
    with_median = espn_counts_median(league["teams"], done)
    computed, h2h, _ = results(schedule, regular, with_median)
    start, mismatched = {}, []
    for team in league["teams"]:
        overall = (team.get("record") or {}).get("overall") or {}
        mine = computed[team["id"]]
        if overall:
            start[team["id"]] = ((overall.get("wins") or 0) + 0.5 * (overall.get("ties") or 0),
                                 overall.get("pointsFor") or 0.0)
            if (overall.get("wins"), overall.get("losses")) != (mine[0], mine[1]):
                mismatched.append(team["id"])
        else:
            start[team["id"]] = (mine[0] + 0.5 * mine[2], mine[3])

    left = periods_left(settings, ctx["week"])
    weeks = sorted(w for ws in left.values() for w in ws)
    per_team = {team: means_for(ctx["rosters"].get(team, []), ctx, weeks) if weeks else {} for team in teams}
    means = {}
    for period, ws in left.items():
        means[period] = {}
        for team in teams:
            points = sum(per_team[team][w][0] for w in ws)
            share = (sum(per_team[team][w][1] for w in ws) / len(ws)) if ws else 0.0
            means[period][team] = (points, share)
    pairs = defaultdict(list)
    for m in schedule:
        if m.get("matchupPeriodId") in left and m.get("home") and m.get("away"):
            pairs[m["matchupPeriodId"]].append((m["home"]["teamId"], m["away"]["teamId"]))

    sd = wins.spread(schedule)
    rule = settings["scheduleSettings"].get("playoffSeedingRule")
    spots = settings["scheduleSettings"].get("playoffTeamCount") or 0
    model = Model(teams, start, h2h, dict(pairs), means, sd, with_median, rule, spots, sims=sims)
    later = [w for w in weeks if w != ctx["week"]] or weeks
    ahead = {team: sum(per_team[team][w][0] for w in later) / len(later) if later else 0.0 for team in teams}
    holes = {team: max((per_team[team][w][2] for w in later), default=0) for team in teams}
    deadline = (settings.get("tradeSettings") or {}).get("deadlineDate")
    facts = {"names": ctx["names"], "me": ctx["me"], "week": ctx["week"], "left": left, "sd": sd, "rule": rule,
             "with_median": with_median, "mismatched": mismatched, "done": done, "spots": spots,
             "records": {t["id"]: (t.get("record") or {}).get("overall") or {} for t in league["teams"]},
             "deadline": datetime.fromtimestamp(deadline / 1000) if deadline else None,
             "divisions": len(settings["scheduleSettings"].get("divisions") or []) > 1,
             "ahead": ahead, "holes": holes, "later": later}
    return model, facts


def point_values(model, me):
    """{period: your chance of last with BUMP more points that week}."""
    return {period: model.run({period: {me: BUMP}})[me][0] for period in model.periods}


def describe(model, facts):
    names, me = facts["names"], facts["me"]
    odds, matchup = model.run(watch=me)
    left = facts["left"]
    span = f"week {min(left)}" if len(left) == 1 else f"weeks {min(left)}-{max(left)}"
    lines = [f"Odds for the rest of the regular season, {datetime.now():%a %b %d %I:%M %p} "
             f"({span} left; {len(model.draws):,} simulations)", ""]
    if not left:
        return "\n".join(lines + ["The regular season is over."])
    mine = odds[me]
    lines.append(f"Your chance of finishing last: {mine[0]:.0%}    of a playoff spot: {mine[1]:.0%}")
    racing = sorted((t for t in model.teams if t != me and odds[t][0] >= 0.02), key=lambda t: -odds[t][0])
    if racing:
        lines.append("Who else could finish last: " + ", ".join(f"{names[t]} {odds[t][0]:.0%}" for t in racing))

    lines += ["", "Your games left (and the median game each week):" if model.with_median else "Your games left:"]
    for period in model.periods:
        opponent = next((b if a == me else a for a, b in model.pairs.get(period, ()) if me in (a, b)), None)
        you, them = model.means[period][me][0], model.means[period].get(opponent, (0.0,))[0]
        racer = "  (also in the race for last)" if opponent in racing else ""
        lines.append(f"  week {period}: v {names.get(opponent, '?')}, you {you:.0f} v {them:.0f}, "
                     f"{matchup.get(period, 0.0):.0%} to win{racer}")

    lines += ["", f"What {BUMP:.0f} more points would do to your chance of last:"]
    for period, after in point_values(model, me).items():
        lines.append(f"  in week {period}: {mine[0]:.1%} -> {after:.1%}")

    lines += ["", f"  {'TEAM':<26} {'RECORD':>8} {'PF':>7} {'PF/WK':>6} {'AHEAD':>6} {'LAST':>5} {'PLAYOFFS':>8}"]
    so_far = {t: model.start[t][1] / facts["done"] if facts["done"] else 0.0 for t in model.teams}
    for team in sorted(model.teams, key=lambda t: (-model.start[t][0], -model.start[t][1])):
        overall = facts["records"].get(team) or {}
        record = f"{overall.get('wins', 0)}-{overall.get('losses', 0)}" + (f"-{overall['ties']}" if overall.get("ties") else "")
        marker = "*" if team == me else " "
        lines.append(f" {marker}{names[team][:26]:<26} {record:>8} {model.start[team][1]:>7.1f} {so_far[team]:>6.1f} "
                     f"{facts['ahead'][team]:>6.1f} {odds[team][0]:>5.0%} {odds[team][1]:>8.0%}")
    weeks_ahead = ", ".join(str(w) for w in facts["later"])
    lines.append(f"PF/WK is points a game so far; AHEAD is the best lineup's projection a week (weeks {weeks_ahead}).")
    low = [t for t in model.teams if so_far[t] and facts["ahead"][t] < LOW * so_far[t]]
    if low:
        lines += ["", "Projection check: these teams project far below what they have scored. If a manager has "
                      "stopped setting his lineup, that's right; if not, players are missing from the projection:"]
        for team in low:
            empty = facts["holes"][team]
            lines.append(f"  {names[team]}: {facts['ahead'][team]:.0f} a week against {so_far[team]:.0f} so far"
                         + (f"; {empty} starting slot{'s' if empty != 1 else ''} nobody can fill" if empty else ""))

    rule = "head-to-head, then total points" if model.by_h2h else "total points"
    lines += ["", f"Each team starts its best lineup; scores spread about {facts['sd']:.0f} points a week "
                  "(this league's own); ties broken by " + f"{rule} ({facts['rule'] or 'ESPN gave no rule'}); "
                  f"top {facts['spots']} make the playoffs."]
    if facts["divisions"]:
        lines.append("This league has divisions; the playoff odds ignore division winners.")
    check = (f"ESPN's records {'include' if model.with_median else 'leave out'} the median game")
    if facts["mismatched"]:
        check += (f"; for {len(facts['mismatched'])} teams they don't match the schedule "
                  f"({', '.join(names[t] for t in facts['mismatched'])}), so ESPN's numbers were used")
    else:
        check += f", and match the schedule for all {len(model.teams)} teams"
    lines.append(f"Check: {check}.")
    if facts["deadline"]:
        lines.append(f"Trade deadline: {facts['deadline']:%a %b %d, %I:%M %p}.")
    return "\n".join(lines)


def main(argv):
    espn = ESPN.from_env()
    model, facts = build(espn)
    body = describe(model, facts)
    print(body)
    if "--post" in argv:
        mine = model.run()[facts["me"]][0]
        mailer.send(f"Odds: {mine:.0%} chance you finish last", body, blocks=slack.monospace(body))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except ESPNError as error:
        sys.exit(f"Error: {error}")
