"""The wire ranked four ways beside today's report: points or wins, level or rate.

Every waiver run with --rate-shadow ranks the free agents as the report does, by the points each
adds to your lineup this week and the next LOOKAHEAD after the drop he forces, and three more
ways: with backs' and receivers' rates (rates.py) in place of their levels (values.py), and each
of those by wins added (wins.py) instead of points. Nothing reaches the report, the bids or
Claude's brief. Every scored free agent goes in logs/shadow.csv, and on Tuesdays the run also
emails a comparison: today's lists side by side, the hidden value (free agents whose work says
they're 2 or more points a game better than ESPN's number) and how past flags paid off, where the
rate and the level disagree and why, and what each list's top picks have scored since.

Usage:
  python3 shadow.py            print the comparison from the log
  python3 shadow.py --email    print it and send it
"""
import csv
import html
import sys
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path
from statistics import fmean, median

import mailer
import rates
import values
import waivers
import wins
from espn import BENCH, parse_roster_entry
from lineup import CANT_PLAY, plan
from values import GAMES_PER_SEASON, season_pace

LOG = Path(__file__).with_name("logs") / "shadow.csv"
FIELDS = ["logged_at", "week", "player_id", "name", "position", "percent_owned", "listed", "espn", "espn_week",
          "level", "rate", "rate_work", "rate_beyond", "work_recent", "work_average", "active", "bench_floor",
          "points_now", "points_new", "wins_now", "wins_new",
          "rank_points_now", "rank_points_new", "rank_wins_now", "rank_wins_new", "xfp_through"]
LISTS = {"points_now": "points, level (the report)", "points_new": "points, rate",
         "wins_now": "wins, level", "wins_new": "wins, rate"}
# Only backs and receivers are valued differently by the level and the rate, so the email compares
# them alone. With quarterbacks and tight ends in, a day when nobody adds lineup points filled every
# list with quarterbacks (13 a game against 6 for the best back), the same in all four.
SKILL = tuple(sorted(rates.RATED))
TOP = 8               # how deep the email compares today's lists
PICKS = 5             # how many of each list's top the scorecard follows
AHEAD = 4             # weeks after a run that a pick's points count over
SUMMARY_WEEKDAY = 1   # Tuesday: the week's games are in, and it's the day before the main waiver run
# A free agent is hidden value when he has played lately (rates.recently_played), his rate is at
# least MIN_RATE and beats your worst healthy bench player at his position (bench_floors), and it is
# HIDDEN points a game over ESPN's season projection a game. MIN_RATE is there because a bench as
# thin as 1.8 a game lets through backs worth 2. On the 2026 wire replay (rate_test.py, 15
# player-weeks, 9 players) such players were projected 3.5 by ESPN and 6.9 by the rate, and scored
# 4.8 over their next four games; 5 of the 15 scored 2+ over ESPN's number. The 9 left out for a
# rate under 5 scored 4.0. The bench floor isn't in that replay: past rosters aren't logged.
HIDDEN = 2.0
MIN_RATE = 5.0
HIDDEN_SHOWN = 10


def matchup_weeks(mine, theirs, schedule, team_id, settings, horizon, this_week, slot_counts):
    """For each week of the window: (your projected score, your opponent's, everyone else's, playoff).

    Every team's lineup is projected the way yours is (waivers.projected_for). A playoff opponent
    isn't known yet, so a playoff week is played against the middle of the other teams.
    """
    period_of = {int(scoring): int(period) for period, scorings in (settings.get("matchupPeriods") or {}).items()
                 for scoring in scorings}
    regular = settings.get("matchupPeriodCount") or 99
    opponent_in = wins.opponents(schedule, team_id)
    found = []
    for week, games in horizon:
        def total(players):
            return waivers.lineup_value_and_holes(waivers.projected_for(players, week, this_week), slot_counts, games)[0]
        others = {team: total(roster) for team, roster in theirs.items()}
        period = period_of.get(week, week)
        playoff = period > regular
        opponent = None if playoff else others.get(opponent_in.get(period))
        found.append((total(mine), median(others.values()) if opponent is None else opponent,
                      list(others.values()), playoff))
    return found


def per_game(player):
    """Points a game from here on as a list values him: his level, or his rate in the rate's lists."""
    return season_pace(player) / GAMES_PER_SEASON


def ranks(scored, key):
    """{player id: place}: points or wins added first, then the report's season gain, then points a
    game, then ownership.

    The report itself goes from season gain straight to ownership. On a day nobody on the wire
    improves your lineup, every back and receiver ties at zero, and its order is just who is most
    rostered; each list's own points a game keeps the comparison about value.
    """
    # Wins to the thousandth: a player whose points added round to zero can come out a hair under
    # zero in wins, which would otherwise sink him below every exact zero.
    order = sorted(scored, key=lambda c: (-round(c[key], 3), -c["ahead_gain"], -c["season_gain"], -per_game(c),
                                          -c["percent_owned"]))
    return {c["id"]: place for place, c in enumerate(order, start=1)}


def bench_floors(roster, slot_counts, games):
    """{RB/WR: points a game of your worst healthy bench player there}, valued by the rate.

    A free agent who can't beat it isn't someone you'd pick up. Backs and receivers both fill the
    flex, so a position with nobody healthy on your bench borrows the worst bench back or receiver.
    """
    target, _ = plan(roster, slot_counts, games)
    bench = [p for p in rates.by_rate(roster) if target.get(p["id"]) == BENCH
             and p["injury_status"] not in CANT_PLAY and p["position"] in rates.RATED]
    floors = {}
    for position in rates.RATED:
        values_there = [per_game(p) for p in bench if p["position"] == position] or [per_game(p) for p in bench]
        if values_there:
            floors[position] = min(values_there)
    return floors


def record(espn, league, team_id, roster, pool, cards, slot_counts, games, byes, week, current, listed,
           path=None, now=None):
    """Rank the wire the three other ways, log all four, and return a line for the run's log.

    current is the report's scored pool in the report's order; listed is its shortlist.
    """
    data = rates.load(espn.season)
    rivals = {team["id"]: [parse_roster_entry(entry, week, espn.season) for entry in team["roster"]["entries"]]
              for team in espn.league("mRoster", week=week)["teams"] if team["id"] != team_id}
    everyone = [p for players in rivals.values() for p in players]
    cards = {**cards, **values.attach(espn, everyone, week)}
    rated = rates.attach(roster + pool + everyone, cards, espn.season, week, data)
    new = waivers.score_pool(rates.by_rate(roster), rates.by_rate(pool), slot_counts, games, byes, week)

    schedule = espn.league("mMatchupScore").get("schedule", [])
    sd = wins.spread(schedule)
    horizon = waivers.schedule_ahead(games, byes, week)
    settings = league["settings"]["scheduleSettings"]
    versions = (("now", roster, rivals, current),
                ("new", rates.by_rate(roster), {team: rates.by_rate(p) for team, p in rivals.items()}, new))
    for version, mine, theirs, scored in versions:
        weeks = matchup_weeks(mine, theirs, schedule, team_id, settings, horizon, week, slot_counts)
        for candidate in scored:
            candidate[f"wins_{version}"] = wins.wins_added(candidate["by_week"], weeks, sd)

    place = {"points_now": ranks(current, "ahead_gain"), "points_new": ranks(new, "ahead_gain"),
             "wins_now": ranks(current, "wins_now"), "wins_new": ranks(new, "wins_new")}
    floors = bench_floors(roster, slot_counts, games)
    log(current, new, pool, place, {c["id"] for c in listed}, week, data[2], floors, path, now)
    agree = len(set(top_ids(current, place["points_now"])) & set(top_ids(new, place["points_new"])))
    return (f"(rate shadow: {rated} backs and receivers rated, expected points through week {data[2]}; "
            f"{len(current)} free agents logged; the top {TOP} by points agree on {agree})")


def top_ids(scored, place, count=TOP):
    return [c["id"] for c in sorted((c for c in scored if c["position"] in SKILL), key=lambda c: place[c["id"]])][:count]


def log(current, new, pool, place, listed, week, through, floors, path=None, now=None):
    path = path or LOG
    path.parent.mkdir(exist_ok=True)
    upgrade(path)
    new_file = not path.exists()
    stamp = (now or datetime.now()).isoformat(timespec="seconds")   # two runs in a minute stay apart
    by_id = {p["id"]: p for p in pool}
    shadow = {c["id"]: c for c in new}
    with open(path, "a", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        if new_file:
            writer.writeheader()
        for c in current:
            n, p = shadow[c["id"]], by_id.get(c["id"], {})
            rounded = lambda value: "" if value is None else round(value, 2)
            writer.writerow({"logged_at": stamp, "week": week, "player_id": c["id"], "name": c["name"],
                             "position": c["position"], "percent_owned": c["percent_owned"],
                             "listed": int(c["id"] in listed),
                             "espn": round((c["season_projection"] or 0.0) / GAMES_PER_SEASON, 2),
                             "espn_week": rounded(c["projection"]),
                             "level": round(season_pace(c) / GAMES_PER_SEASON, 2),
                             "rate": rounded(p.get("rate")), "rate_work": rounded(p.get("rate_work")),
                             "rate_beyond": rounded(p.get("rate_beyond")), "work_recent": rounded(p.get("work_recent")),
                             "work_average": rounded(p.get("work_average")),
                             "active": "" if "active" not in p else int(p["active"]),
                             "bench_floor": rounded(floors.get(c["position"])),
                             "points_now": c["ahead_gain"], "points_new": n["ahead_gain"],
                             "wins_now": round(c["wins_now"], 3), "wins_new": round(n["wins_new"], 3),
                             **{f"rank_{key}": place[key][c["id"]] for key in LISTS}, "xfp_through": through})


def upgrade(path):
    """Rewrite a log kept under older columns with today's, blanks where a column is new."""
    if not path.exists():
        return
    with open(path) as handle:
        header = next(csv.reader(handle), None)
    if header == FIELDS:
        return
    rows = read(path)
    with open(path, "w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def read(path=None):
    path = path or LOG
    if not path.exists():
        return []
    with open(path) as handle:
        return list(csv.DictReader(handle))


def top(run, key, count=None):
    """The skill players a run's list put first, best first."""
    return sorted((r for r in run if r["position"] in SKILL), key=lambda r: int(r[f"rank_{key}"]))[:count]


def skill_places(run):
    """{list: {player id: place among backs and receivers}}."""
    return {key: {r["player_id"]: place for place, r in enumerate(top(run, key), start=1)} for key in LISTS}


def picks(rows):
    """{list: {(week, player id): when}}: each list's top PICKS at every run, once a week per
    player, counted from the first run that picked him that week."""
    runs = defaultdict(list)
    for row in rows:
        runs[row["logged_at"]].append(row)
    found = defaultdict(dict)
    for stamp in sorted(runs):
        for key in LISTS:
            for r in top(runs[stamp], key, PICKS):
                week = int(r["week"])
                found[key].setdefault((week, int(r["player_id"])), stamp)
    return found


def scored_after(week, logged_at, player_id, by_player):
    """Points a game over his games that kicked off after the run, in its week and the next three, or None."""
    later = rates.scored_after(by_player.get(player_id, {}), week, logged_at, AHEAD)
    return fmean(later) if later else None


def average(chosen, by_player):
    """(how many have played since, their points a game) for {(week, player id): when picked}."""
    found = [s for s in (scored_after(week, stamp, pid, by_player) for (week, pid), stamp in chosen.items())
             if s is not None]
    return len(found), (fmean(found) if found else None)


def rate_shown(row):
    """The rate's lists' points a game for him: his rate, or "idle" where he hasn't played lately
    and those lists value him at his level."""
    if row.get("active") == "0":
        return "idle"
    return f"{float(row['rate']) if row['rate'] else float(row['level']):.1f}"


def gap(row):
    """How far his rate is over ESPN's season projection a game; None without a rate."""
    return float(row["rate"]) - float(row["espn"]) if row["rate"] and row["espn"] else None


def flagged(row):
    """Hidden value: playing lately, better than your worst bench player there and than MIN_RATE, and
    HIDDEN over ESPN."""
    return (row["position"] in rates.RATED and row.get("active") == "1" and gap(row) is not None
            and gap(row) >= HIDDEN and float(row["rate"]) >= MIN_RATE
            and (not row.get("bench_floor") or float(row["rate"]) > float(row["bench_floor"])))


def hidden_value(run, rows, by_player):
    """Lines for today's hidden value and for how every earlier flag has done since."""
    today = sorted((r for r in run if flagged(r)), key=lambda r: -gap(r))
    place = skill_places(run)["points_new"]
    lines = [f"HIDDEN VALUE: backs and receivers who have played in one of their team's last {rates.RECENT} games,",
             f"whose rate is at least {MIN_RATE:.0f} a game, beats your worst healthy bench player at the position",
             f"(BENCH), and is {HIDDEN:.0f}+ a game over ESPN's season projection a game. ESPN never revises that",
             "number; ESPN WK is this week's projection, which does move, and is 0 for a player on bye or not",
             "expected to play."]
    if today:
        lines.append(f"  {'PLAYER':<24}{'POS':<4}{'OWN%':>5}{'ESPN':>6}{'ESPN WK':>8}{'LEVEL':>7}{'RATE':>6}"
                     f"{'BENCH':>7}{'WORK LAST 2/SEASON':>20}{'PTS/RATE':>10}")
        for r in today[:HIDDEN_SHOWN]:
            week_projection = f"{float(r['espn_week']):.1f}" if r["espn_week"] else "-"
            bench = f"{float(r['bench_floor']):.1f}" if r["bench_floor"] else "-"
            work = (f"{float(r['work_recent']):.1f} / {float(r['work_average']):.1f}"
                    if r["work_recent"] and r["work_average"] else "-")
            lines.append(f"  {r['name'][:23]:<24}{r['position']:<4}{float(r['percent_owned']):>5.0f}"
                         f"{float(r['espn']):>6.1f}{week_projection:>8}{float(r['level']):>7.1f}{float(r['rate']):>6.1f}"
                         f"{bench:>7}{work:>20}{place[r['player_id']]:>10}")
        if len(today) > HIDDEN_SHOWN:
            lines.append(f"  and {len(today) - HIDDEN_SHOWN} more.")
        lines.append("  Work is expected points a game from his carries and targets; last 2 above season is a role growing.")
    else:
        lines.append("  Nobody on today's wire.")

    first = {}
    for row in sorted(rows, key=lambda r: r["logged_at"]):
        if flagged(row):
            first.setdefault((int(row["week"]), int(row["player_id"])), row)
    since = [(row, scored_after(week, row["logged_at"], pid, by_player))
             for (week, pid), row in first.items()]
    since = [(row, scored) for row, scored in since if scored is not None]
    lines += ["", f"  How the flags have done: {len(first)} flagged so far (once a week per player), "
                  f"{len(since)} have played since."]
    if since:
        paid = sum(1 for row, scored in since if scored >= float(row["espn"]) + HIDDEN)
        lines += [f"  Over the next {AHEAD} weeks: ESPN said {fmean(float(r['espn']) for r, _ in since):.1f} a game, "
                  f"the rate {fmean(float(r['rate']) for r, _ in since):.1f}, they scored "
                  f"{fmean(scored for _, scored in since):.1f}.",
                  f"  {paid} of {len(since)} scored {HIDDEN:.0f}+ a game over ESPN's number."]
    return lines + [""], today


def _a_game(count, value):
    return f"{value:5.1f} a game from {count:>3} picks" if count else "  nothing played yet"


def summary(rows, by_player):
    """(subject, text) comparing the latest run's four lists and scoring every list's picks so far."""
    if not rows:
        return "rate shadow: nothing logged yet", "No shadow runs logged yet (waivers.py --rate-shadow logs them)."
    latest = max(r["logged_at"] for r in rows)
    run = list({r["player_id"]: r for r in rows if r["logged_at"] == latest}.values())   # one row a player
    week, through = run[0]["week"], run[0]["xfp_through"]
    lists = {key: top(run, key, TOP) for key in LISTS}
    agree = len({r["player_id"] for r in lists["points_now"]} & {r["player_id"] for r in lists["points_new"]})
    lines = [f"Shadow comparison, week {week}, from the run at {latest.replace('T', ' ')}.",
             "",
             "Four ways to rank the same free agents. All four count what a player adds to your lineup",
             f"this week and the next {waivers.LOOKAHEAD}, after the drop he forces, as the report does.",
             "  Level: later weeks valued as the report values them now (values.py).",
             "  Rate: a back's or receiver's scoring split into his work (expected points from his carries",
             "  and targets) and what he makes of it, trusting the work more (rates.py). It values this week",
             "  too, in place of ESPN's projection, except where ESPN has him at zero.",
             "  Wins: each week's points turned into wins, your matchup plus the median game, so a point",
             "  counts most in a close week (wins.py).",
             "Quarterbacks and tight ends are valued the same in every list, so this compares backs and",
             f"receivers only. Expected points are in through week {through}. Nothing here changes the report",
             "or its bids.",
             "",
             f"TODAY'S TOP {TOP} BACKS AND RECEIVERS. Place in each list among backs and receivers; ties on",
             "points or wins added go to the list's own points a game:",
             f"  {'PLAYER':<24}{'POS':<4}{'OWN%':>5}{'LEVEL':>7}{'RATE':>6}  "
             + "".join(f"{label:>12}" for label in ("PTS/LEVEL", "PTS/RATE", "WINS/LEVEL", "WINS/RATE")),
             ]
    shown = {r["player_id"]: r for key in LISTS for r in lists[key]}
    places = skill_places(run)
    for r in sorted(shown.values(), key=lambda r: min(places[key][r["player_id"]] for key in LISTS)):
        cells = "".join(f"{places[key][r['player_id']]:>12}" for key in LISTS)
        lines.append(f"  {r['name'][:23]:<24}{r['position']:<4}{float(r['percent_owned']):>5.0f}"
                     f"{float(r['level']):>7.1f}{rate_shown(r):>6}  {cells}")
    lines += [f"  RATE \"idle\": no game in his team's last {rates.RECENT}; the rate's lists value him at his level.",
              "", f"The report's list and the rate's list share {agree} of their top {TOP}.", ""]
    found, today = hidden_value(run, rows, by_player)
    lines += found

    apart = sorted((r for r in run if r["rate"] and r["position"] in SKILL and r.get("active") != "0"),
                   key=lambda r: -abs(float(r["rate"]) - float(r["level"])))[:6]
    if apart:
        lines += ["WHERE THE RATE AND THE LEVEL DISAGREE MOST (points a game):",
                  f"  {'PLAYER':<24}{'POS':<4}{'LEVEL':>6}{'RATE':>6}   = work and beyond"]
        for r in apart:
            beyond = float(r["rate_beyond"])
            lines.append(f"  {r['name'][:23]:<24}{r['position']:<4}{float(r['level']):>6.1f}{float(r['rate']):>6.1f}"
                         f"   = {float(r['rate_work']):.1f} {'-' if beyond < 0 else '+'} {abs(beyond):.1f}")
        lines += ["  Work is expected points a game from his carries and targets; beyond is what he has scored",
                  "  over that, held near the position's average because it rarely repeats.", ""]

    chosen = picks(rows)
    lines += [f"SCORECARD: what each list's top {PICKS} have scored a game since, in games that kicked off after",
              f"the pick, over its week and the next {AHEAD - 1} (games played; a player picked on several days of",
              "a week counts once):"]
    for key, label in LISTS.items():
        lines.append(f"  {label:<34}{_a_game(*average(chosen[key], by_player))}")
    for first, second, title in (("points_new", "points_now", "rate against level, by points"),
                                 ("wins_new", "points_new", "wins against points, with the rate")):
        only_first = {k: v for k, v in chosen[first].items() if k not in chosen[second]}
        only_second = {k: v for k, v in chosen[second].items() if k not in chosen[first]}
        lines += [f"  Where they picked differently, {title}:",
                  f"    only {LISTS[first]:<27}{_a_game(*average(only_first, by_player))}",
                  f"    only {LISTS[second]:<27}{_a_game(*average(only_second, by_player))}"]
    lines += ["  A handful of picks says little; read this once each side has a few dozen."]
    subject = (f"Rate shadow week {week}: {len(today)} hidden-value free agent{'' if len(today) == 1 else 's'}; "
               f"the report and the rate share {agree} of their top {TOP}")
    return subject, "\n".join(lines)


def as_html(text):
    return ("<pre style=\"font-family:Menlo,Consolas,monospace;font-size:12px;line-height:1.45\">"
            f"{html.escape(text)}</pre>")


def send(rows=None, by_player=None, season=None, send_mail=mailer.send):
    """Print the comparison and email it. Returns the subject."""
    if by_player is None:
        by_player = rates.load(season or date.today().year)[0]
    subject, text = summary(read() if rows is None else rows, by_player)
    print(text)
    send_mail(subject, text, as_html(text))
    return subject


def is_summary_day(now=None):
    return (now or datetime.now()).weekday() == SUMMARY_WEEKDAY


if __name__ == "__main__":
    if "--email" in sys.argv:
        send()
    else:
        print(summary(read(), rates.load(date.today().year)[0])[1])
