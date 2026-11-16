"""Trade analyzer: what a trade does to both teams' lineups, week by week, through the playoffs.

Read-only, like the waiver report. It never proposes, accepts, declines or counters a trade.

Usage:
  python3 trades.py --give "Player" --get "Player"    test a trade before you send it
  Repeat --give or --get for a bigger deal; the other team is whoever has the --get players.
  Names can be partial and skip punctuation: --get "st brown" finds Amon-Ra St. Brown.
  python3 trades.py --watch    email an analysis of each new proposal sent to you or by you
                               (the 15-minute launchd job); silent when there's nothing new
"""
import json
import math
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import requests

import mailer
import values
from espn import BENCH, ESPN, IR, PRO_TEAMS, ESPNError, parse_roster_entry
from lineup import CANT_PLAY, HEALTHY, kickoff_of, plan
from values import GAMES_PER_SEASON, season_pace
from waivers import (LOOKAHEAD, POOL_SIZE, best_lineup_value, drop_candidates,
                     projected_for, replacement_level, schedule_ahead, score_pool, surplus)

SEEN = Path(__file__).with_name("cache") / "trades_seen.json"
FAILED = Path(__file__).with_name("cache") / "trades_failed_on.txt"

# ESPN gives an injury status, not a return date. Until the news check supplies one, a player
# who can't play is assumed back after this many weeks; four is the NFL's minimum stay on IR.
WEEKS_OUT = {"OUT": 1, "INJURY_RESERVE": 4, "SUSPENSION": 1}


def due_back(player, this_week, return_weeks):
    """The first week he can play: the news check's answer when there is one, else the default."""
    if player["injury_status"] not in CANT_PLAY:
        return this_week
    return return_weeks.get(player["id"], this_week + WEEKS_OUT.get(player["injury_status"], 1))


def in_week(players, week, this_week, return_weeks):
    """The roster as it would play a given week.

    This week is exactly as ESPN has it. In later weeks nobody is locked yet, everyone counts at
    his level (values.py), and the injured count from the week they're due back.
    """
    if week == this_week:
        return players
    ready = []
    for player in players:
        player = dict(player, locked=False)
        if week >= due_back(player, this_week, return_weeks):
            player.update(injury_status="ACTIVE", slot=BENCH if player["slot"] == IR else player["slot"])
        ready.append(player)
    return projected_for(ready, week, this_week)


def moves_this_week(player, games, process_at):
    """Whether he changes teams in time to play for the new one this week.

    A player whose game starts before the trade processes plays it for the team he's on now.
    """
    kickoff = kickoff_of(player, games)
    return not player["locked"] and (kickoff is None or kickoff > process_at)


def settle(roster, ctx):
    """The roster made legal again. Returns (roster, dropped, added).

    Over the limit, it drops by the waiver report's rule: bench only, least value lost, no hole
    opened in the weeks ahead. Under it, it adds the free agent the waiver report would rank
    first, as long as he helps at all.
    """
    capacity = sum(n for slot, n in ctx["slot_counts"].items() if n and slot != IR)
    roster, dropped, added = list(roster), [], []
    while True:
        target, _ = plan(roster, ctx["slot_counts"], ctx["games"])
        active = sum(1 for p in roster if target[p["id"]] != IR)
        if active > capacity:
            spare = drop_candidates(roster, target, ctx["slot_counts"], ctx["horizon"], ctx["week"],
                                    ctx["replacement"], count=1)
            cut = spare[0] if spare else min((p for p in roster if target[p["id"]] == BENCH),
                                             key=lambda p: surplus(p, ctx["replacement"]))
            roster = [p for p in roster if p["id"] != cut["id"]]
            dropped.append(cut)
        elif active < capacity:
            taken = {p["id"] for p in roster}
            ranked = score_pool(roster, [f for f in ctx["pool"] if f["id"] not in taken],
                                ctx["slot_counts"], ctx["games"], ctx["byes"], ctx["week"])
            useful = [c for c in ranked if c["ahead_gain"] > 0 or c["season_gain"] > 0]
            if not useful:
                return roster, dropped, added
            roster.append(useful[0])
            added.append(useful[0])
        else:
            return roster, dropped, added


def side(roster, outgoing, incoming, ctx):
    """One team's best-lineup points for every week left, without the trade and with it."""
    week, games, process_at = ctx["week"], ctx["games"], ctx["process_at"]
    traded = [p for p in roster if p["id"] not in outgoing] + [dict(p, slot=BENCH) for p in incoming]
    before, _, added_anyway = settle(roster, ctx)
    after, dropped, added = settle(traded, ctx)

    # This week only players whose games haven't started change hands, a forced drop only costs
    # a game still to come, and a free agent can't be in place until waivers run.
    moves = lambda p: moves_this_week(p, games, process_at)
    cut_now = {p["id"] for p in dropped if moves(p)}
    now = [p for p in roster if not (p["id"] in outgoing and moves(p)) and p["id"] not in cut_now]
    now += [dict(p, slot=BENCH) for p in incoming if moves(p) and p["id"] not in cut_now]

    weeks = []
    for number, playing in ctx["weeks"]:
        if number == week:
            pair = (roster, now)
        else:
            pair = (in_week(before, number, week, ctx["return_weeks"]),
                    in_week(after, number, week, ctx["return_weeks"]))
        weeks.append((number,) + tuple(best_lineup_value(p, ctx["slot_counts"], playing) for p in pair))
    anyway = {p["id"] for p in added_anyway}
    return {"weeks": weeks, "dropped": dropped, "added": [p for p in added if p["id"] not in anyway]}


def analyze(ctx, partner, give, get):
    """Both teams, week by week, with and without the trade. give and get are player ids."""
    ctx = dict(ctx, replacement=replacement_level(ctx["pool"]),
               horizon=schedule_ahead(ctx["games"], ctx["byes"], ctx["week"]),
               weeks=schedule_ahead(ctx["games"], ctx["byes"], ctx["week"],
                                    lookahead=max(ctx["last_week"] - ctx["week"], 0)))
    mine, theirs = ctx["rosters"][ctx["me"]], ctx["rosters"][partner]
    sent = [p for p in mine if p["id"] in give]
    received = [p for p in theirs if p["id"] in get]
    return {"partner": partner, "sent": sent, "received": received,
            "you": side(mine, give, received, ctx), "them": side(theirs, get, sent, ctx)}


def windows(weeks, ctx):
    """Points gained this week, over the waiver report's window, the rest of the regular season,
    and the fantasy playoffs. Negative is points lost."""
    gain = {number: after - before for number, before, after in weeks}
    week, regular_end = ctx["week"], ctx["regular_end"]
    return {"this_week": gain.get(week, 0.0),
            "next": sum(g for number, g in gain.items() if number <= week + LOOKAHEAD),
            "regular": sum(g for number, g in gain.items() if number <= regular_end),
            "playoffs": sum(g for number, g in gain.items() if number > regular_end)}


def describe(result, ctx):
    week, regular_end, last_week = ctx["week"], ctx["regular_end"], ctx["last_week"]
    partner = ctx["names"].get(result["partner"], f"team {result['partner']}")
    label = lambda p: (f"{p['name']} ({p['position']}, {PRO_TEAMS.get(p['pro_team'], '?')}"
                       + ("" if p["injury_status"] in HEALTHY else f", {p['injury_status']}") + ")")
    lines = [f"Trade check, week {week}: you and {partner}",
             "  You give: " + ", ".join(label(p) for p in result["sent"]),
             "  You get:  " + ", ".join(label(p) for p in result["received"]), ""]
    review = f" after the {ctx['review_hours']}-hour review" if ctx["review_hours"] else ""
    lines += [f"Accepted now, it would process around {ctx['process_at']:%a %I:%M %p}{review}. Anyone "
              "whose game starts before then plays this week for the team he's on now.", ""]

    lines.append("Projected lineup points, with the trade minus without:")
    lines.append(f"{'':<20} {'THIS WK':>8} {'NEXT ' + str(LOOKAHEAD + 1):>8} {'TO WK ' + str(regular_end):>9} "
                 f"{'PLAYOFFS':>9}")
    for who, key in (("You", "you"), (partner[:20], "them")):
        w = windows(result[key]["weeks"], ctx)
        lines.append(f"{who:<20} {w['this_week']:>+8.1f} {w['next']:>+8.1f} {w['regular']:>+9.1f} "
                     f"{w['playoffs']:>+9.1f}")
    lines.append(f"Playoffs are weeks {regular_end + 1}-{last_week}. Byes are included.")

    lines += ["", f"{'In the deal':<36} {'THIS WK':>7} {'SEASON/G':>9} {'LEVEL':>6} {'ACTUAL/G':>10}"]
    for p in result["sent"] + result["received"]:
        pace = f"{p['scored'] / p['games_played']:.1f} ({p['games_played']})" if p.get("games_played") else "-"
        lines.append(f"  {label(p)[:34]:<34} {p['projection'] or 0:>7.1f} "
                     f"{(p['season_projection'] or 0) / GAMES_PER_SEASON:>9.1f} "
                     f"{season_pace(p) / GAMES_PER_SEASON:>6.1f} {pace:>10}")
    lines += ["Weeks after this one count each player at LEVEL: SEASON/G, ESPN's preseason projection per",
              "game, moved toward what he has scored in each game since (values.py). For kickers and",
              "defenses LEVEL is still SEASON/G."]

    lines.append("")
    for who, key in (("You", "you"), (partner, "them")):
        part = result[key]
        if part["dropped"]:
            lines.append(f"{who} would drop {', '.join(p['name'] for p in part['dropped'])} to make room")
        if part["added"]:
            lines.append(f"{who} would pick up {', '.join(label(p) for p in part['added'])} for the open "
                         "spot, counted from next week")
        gains = "  ".join(f"{number} {after - before:+.1f}" for number, before, after in part["weeks"])
        lines.append(f"{who}, week by week: {gains}")

    hurt = [p for team in (ctx["rosters"][ctx["me"]], ctx["rosters"][result["partner"]]) for p in team
            if p["injury_status"] in CANT_PLAY]
    if hurt:
        lines += ["", "Assumed back (defaults by status until the news check is in): " + ", ".join(
            f"{p['name']} ({p['injury_status']}) week {due_back(p, week, ctx['return_weeks'])}" for p in hurt)]
    lines += ["", "Read-only: nothing was proposed or sent to ESPN."]
    return "\n".join(lines)


def _plain(text):
    return re.sub(r"[^a-z0-9]", "", text.lower())


def find(name, players):
    """Players whose name contains this one, ignoring case and punctuation. An exact match wins."""
    wanted = _plain(name)
    hits = [p for p in players if wanted and wanted in _plain(p["name"])]
    return [p for p in hits if _plain(p["name"]) == wanted] or hits


def _one(name, hits, where):
    if len(hits) == 1:
        return hits[0]
    if not hits:
        raise ValueError(f"No player matching '{name}' {where}.")
    raise ValueError(f"'{name}' matches {', '.join(p['name'] for p in hits[:6])} {where}; be more specific.")


def pick_players(give_names, get_names, rosters, me):
    """(ids you give, ids you get, the other team's id). ValueError says what's wrong."""
    give = {_one(name, find(name, rosters[me]), "on your roster")["id"] for name in give_names}
    others = [dict(p, team=team) for team, players in rosters.items() if team != me for p in players]
    wanted = [_one(name, find(name, others), "on another team") for name in get_names]
    teams = {p["team"] for p in wanted}
    if len(teams) > 1:
        raise ValueError("ESPN trades are between two teams, and those players are on different ones.")
    return give, {p["id"] for p in wanted}, teams.pop()


def team_name(team):
    return (team.get("name") or f"{team.get('location', '')} {team.get('nickname', '')}".strip()
            or f"team {team['id']}")


def roster_player(entry, week, season):
    """parse_roster_entry, plus what he has actually scored this season, for the report."""
    stats = entry["playerPoolEntry"]["player"].get("stats", [])
    line = next((s for s in stats if s.get("seasonId") == season and s.get("scoringPeriodId") == 0
                 and s.get("statSourceId") == 0), {})
    return dict(parse_roster_entry(entry, week, season), scored=line.get("appliedTotal"),
                games_played=int((line.get("stats") or {}).get("210") or 0))   # ESPN stat 210: games played


def gather(espn):
    league = espn.league("mSettings", "mTeam")
    settings = league["settings"]
    week = league["scoringPeriodId"]
    me = espn.my_team(league["teams"])
    schedule = settings["scheduleSettings"]
    regular = schedule["matchupPeriodCount"]
    regular_end = max((schedule.get("matchupPeriods") or {}).get(str(regular)) or [regular])
    rounds = math.ceil(math.log2(max(schedule.get("playoffTeamCount", 2), 2)))
    roster_data = espn.league("mRoster", week=week)
    ctx = {
        "week": week, "me": me["id"],
        "names": {team["id"]: team_name(team) for team in league["teams"]},
        "rosters": {team["id"]: [roster_player(e, week, espn.season) for e in team["roster"]["entries"]]
                    for team in roster_data["teams"]},
        "slot_counts": {int(k): v for k, v in settings["rosterSettings"]["lineupSlotCounts"].items()},
        "games": espn.pro_games(week), "byes": espn.bye_weeks(), "pool": espn.free_agents(week, POOL_SIZE),
        "regular_end": regular_end,
        "last_week": regular_end + rounds * (schedule.get("playoffMatchupPeriodLength") or 1),
        "review_hours": settings.get("tradeSettings", {}).get("revisionHours") or 0,
        "return_weeks": {},
    }
    try:
        values.attach(espn, [p for team in ctx["rosters"].values() for p in team] + ctx["pool"], week)
    except (ESPNError, requests.exceptions.RequestException) as problem:
        print(f"(player levels unavailable: {type(problem).__name__}); later weeks use ESPN's season projection")
    return ctx


def open_proposals(espn, week):
    """Every trade proposal ESPN lists for last week and this one. A proposal expires 48 hours
    after it's made, so those two weeks hold every one that can still be open."""
    rules = {"transactions": {"filterType": {"value": ["TRADE_PROPOSAL"]}}}
    found = {}
    for period in range(max(week - 1, 1), week + 1):
        data = espn.league("mTransactions2", week=period, headers={"x-fantasy-filter": json.dumps(rules)})
        for proposal in data.get("transactions", []):
            found[proposal.get("id")] = proposal
    return list(found.values())


def new_proposals(proposals, me, seen, now_ms):
    """Open proposals involving your team, sent by you or to you, that haven't been analyzed yet."""
    involves_me = lambda p: any(me in (i.get("fromTeamId"), i.get("toTeamId")) for i in p.get("items") or [])
    return [p for p in proposals if p.get("status") == "PENDING" and p.get("id") not in seen
            and (p.get("expirationDate") or now_ms + 1) > now_ms and involves_me(p)]


def deal_from(proposal, me):
    """(ids you'd give, ids you'd get, the other team's id) from an ESPN trade proposal."""
    items = [item for item in proposal.get("items") or [] if item.get("type") == "TRADE"]
    give = {item["playerId"] for item in items if item.get("fromTeamId") == me}
    get = {item["playerId"] for item in items if item.get("toTeamId") == me}
    others = {item.get("fromTeamId") for item in items} | {item.get("toTeamId") for item in items}
    others.discard(me)
    return give, get, others.pop() if len(others) == 1 else None


def _when(ms):
    return datetime.fromtimestamp(ms / 1000).strftime("%a %b %d %I:%M %p").replace(" 0", " ") if ms else "?"


def offer_line(proposal, me, partner):
    expires = _when(proposal.get("expirationDate"))
    if proposal.get("teamId") == me:
        return (f"You offered this to {partner} {_when(proposal.get('proposedDate'))}. "
                f"It expires {expires} if they don't answer.")
    return (f"{partner} offered this {_when(proposal.get('proposedDate'))}. It expires {expires}. "
            "Accept or decline it in ESPN.")


def subject(proposal, result, me, partner):
    total = lambda key: sum(after - before for _, before, after in result[key]["weeks"])
    heading = f"Your offer to {partner}" if proposal.get("teamId") == me else f"Trade offer from {partner}"
    return f"{heading}: you {total('you'):+.0f}, them {total('them'):+.0f} pts rest of season"


def watch(espn, seen_path=SEEN, now=None):
    """Email an analysis of each new proposal involving your team. Prints nothing when there's
    nothing new, so run.sh leaves the log alone, the same as the gameday check-in."""
    now = now or datetime.now()
    league = espn.league("mSettings", "mTeam")
    deadline = league["settings"].get("tradeSettings", {}).get("deadlineDate")
    if deadline and now.timestamp() * 1000 > deadline:
        return
    me = espn.my_team(league["teams"])["id"]
    seen = json.loads(seen_path.read_text()) if seen_path.exists() else {}
    fresh = new_proposals(open_proposals(espn, league["scoringPeriodId"]), me, seen, now.timestamp() * 1000)
    if not fresh:
        return
    ctx = gather(espn)
    ctx["process_at"] = now + timedelta(hours=ctx["review_hours"])
    for proposal in fresh:
        give, get, partner = deal_from(proposal, me)
        result = analyze(ctx, partner, give, get) if partner in ctx["rosters"] else None
        if result is None or len(result["sent"]) != len(give) or len(result["received"]) != len(get):
            print(f"Skipped proposal {proposal['id'][:8]}: a player in it is no longer where it says")
        else:
            name = ctx["names"].get(partner, f"team {partner}")
            body = offer_line(proposal, me, name) + "\n\n" + describe(result, ctx)
            print(body)
            if not mailer.send(subject(proposal, result, me, name), body):
                continue   # not marked seen, so the next check-in tries the email again
        seen[proposal["id"]] = now.isoformat(timespec="minutes")
        seen_path.parent.mkdir(exist_ok=True)
        seen_path.write_text(json.dumps(seen, indent=1))


def report_failure(message, path=FAILED, today=None, send=mailer.send):
    """Log every failure, email only the first each day: the watch runs every 15 minutes."""
    today = (today or date.today()).isoformat()
    print(f"Trade watch failed: {message}")
    if path.exists() and path.read_text().strip() == today:
        return False
    path.parent.mkdir(exist_ok=True)
    path.write_text(today)
    send("Fantasy bot: trade watch failed",
         f"{message}\n\nOnly the first failure each day is emailed; logs/ has every one.")
    return True


def requested(argv, flag):
    """Every value given after a flag: --give A --give B."""
    return [argv[i + 1] for i, arg in enumerate(argv[:-1]) if arg == flag]


def main(argv):
    if "--watch" in argv:
        try:
            watch(ESPN.from_env())
        except Exception as problem:  # broad on purpose: a dead watch must reach you, but once a day
            report_failure(f"{type(problem).__name__}: {problem}")
        return 0
    give_names, get_names = requested(argv, "--give"), requested(argv, "--get")
    if not give_names or not get_names:
        print(__doc__)
        return 1
    ctx = gather(ESPN.from_env())
    try:
        give, get, partner = pick_players(give_names, get_names, ctx["rosters"], ctx["me"])
    except ValueError as problem:
        print(problem)
        return 1
    ctx["process_at"] = datetime.now() + timedelta(hours=ctx["review_hours"])
    print(describe(analyze(ctx, partner, give, get), ctx))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except ESPNError as error:
        sys.exit(f"Error: {error}")
