"""Read-only look at what the stash rule would need from ESPN, and what it would flag today.

Prints three things, and changes nothing in ESPN or in the repo:
  1. How deep the free-agent list goes when asked for far more than the report's 75, and whether
     ESPN says who is a free agent (addable now, no bid) and who is on waivers.
  2. Every next man up roles.py finds: a lead back down, the back behind him, and where that
     back is in this league.
  3. Each lead back who carries an injury tag, to see how early ESPN marks them.

Usage: python3 role_probe.py
"""
import json
import sys
import time
from collections import Counter
from datetime import datetime

import requests

import roles
from espn import ESPN, PRO_TEAMS, ESPNError, parse_roster_entry
from lineup import HEALTHY

DEPTH = 300       # free agents to ask for, most rostered first (the report asks for 75)
BACKS = 150       # and this many running backs on their own, however little they are rostered
RB_SLOT = 2


def free_agents(espn, week, limit, slots=None):
    """Raw free-agent entries, so the fields the bot doesn't parse yet can be seen. Returns (entries, seconds)."""
    rules = {"players": {"filterStatus": {"value": ["FREEAGENT", "WAIVERS"]}, "limit": limit,
                         "sortPercOwned": {"sortPriority": 1, "sortAsc": False}}}
    if slots:
        rules["players"]["filterSlotIds"] = {"value": slots}
    started = time.time()
    data = espn.league("kona_player_info", week=week, headers={"x-fantasy-filter": json.dumps(rules)})
    return data.get("players", []), time.time() - started


def depth_part(entries, seconds, asked, week, season):
    lines = [f"Free agents, {asked} most rostered: {len(entries)} came back in {seconds:.1f}s"]
    if not entries:
        return lines
    owned = [parse_roster_entry(e, week, season)["percent_owned"] for e in entries]
    lines.append(f"  rostered in {max(owned):.1f}% of ESPN leagues down to {min(owned):.1f}%"
                 f"   (the report's 75 stop at {sorted(owned, reverse=True)[min(74, len(owned) - 1)]:.1f}%)")
    lines.append("  status: " + ", ".join(f"{status} {count}" for status, count in
                                           Counter(str(e.get("status")) for e in entries).most_common()))
    lines.append("  fields on an entry: " + ", ".join(sorted(k for k in entries[0] if k != "player")))
    waiting = next((e for e in entries if e.get("waiverProcessDate")), None)
    if waiting:
        lines.append(f"  a waiverProcessDate: {datetime.fromtimestamp(waiting['waiverProcessDate'] / 1000):%a %b %d %I:%M %p}"
                     f" ({waiting['player'].get('fullName')}, {waiting.get('status')})")
    return lines


def collect(espn, now=None):
    now = now or datetime.now()
    league = espn.league("mSettings", "mTeam")
    week = league["scoringPeriodId"]
    mine = espn.my_team(league["teams"])["id"]
    names = {team["id"]: team.get("name") or f"team {team['id']}" for team in league["teams"]}

    wide, wide_seconds = free_agents(espn, week, DEPTH)
    try:
        backs_only, backs_seconds = free_agents(espn, week, BACKS, [RB_SLOT])
    except requests.exceptions.RequestException as problem:      # a filter the bot has never sent before
        print(f"(ESPN refused the running-backs-only request: {type(problem).__name__}: {str(problem)[:120]})\n")
        backs_only, backs_seconds = [], 0.0

    where, players = {}, {}
    for team in espn.league("mRoster", week=week)["teams"]:
        for entry in team["roster"]["entries"]:
            player = parse_roster_entry(entry, week, espn.season)
            players[player["id"]] = player
            where[player["id"]] = "YOURS" if team["id"] == mine else f"rostered: {names.get(team['id'], team['id'])}"
    for entry in wide + backs_only:
        player = parse_roster_entry(entry, week, espn.season)
        players[player["id"]] = player
        where[player["id"]] = {"FREEAGENT": "free agent now", "WAIVERS": "on waivers"}.get(
            entry.get("status"), str(entry.get("status")))

    team_weeks = roles.finished_weeks(week, espn.bye_weeks(), espn.pro_games(week), now)
    backs = [p for p in players.values() if p["position"] == "RB"]
    cards = espn.player_cards([p["id"] for p in backs], 18)
    for back in backs:
        back["touches"] = roles.touches(cards.get(back["id"], {}), espn.season,
                                        team_weeks.get(back["pro_team"], []))
    return {"week": week, "now": now, "wide": (wide, wide_seconds), "backs_only": (backs_only, backs_seconds),
            "season": espn.season, "backs": backs, "where": where, "team_weeks": team_weeks}


def leads(backs, team_weeks):
    """[(lead back, his usual touches, his touches in the latest game, that game's week)], down or not."""
    found = []
    for team, room in roles.backfields(backs).items():
        weeks = team_weeks.get(team, [])
        lead = roles.lead_back(room, weeks)
        if lead:
            found.append((lead[0], lead[1], lead[0]["touches"].get(weeks[-1], 0), weeks[-1]))
    return found


def report(found):
    week, backs, where = found["week"], found["backs"], found["where"]
    lines = [f"Role check, week {week}, {found['now']:%a %b %d %I:%M %p}. Read-only.", ""]
    lines += depth_part(*found["wide"], DEPTH, week, found["season"])
    only, seconds = found["backs_only"]
    kinds = Counter(parse_roster_entry(e, week, found["season"])["position"] for e in only)
    lines += ["", f"Running backs only ({BACKS} asked): {len(only)} came back in {seconds:.1f}s, by position: "
                  + (", ".join(f"{pos} {count}" for pos, count in kinds.most_common()) or "none")]
    with_work = sum(1 for b in backs if b["touches"])
    lines.append(f"Backs known: {len(backs)}, {with_work} with a game on record this season")

    flags = sorted(roles.flags(backs, found["team_weeks"]),
                   key=lambda f: (where[f["player"]["id"]].startswith("rostered"), -f["latest"]))
    lines += ["", "Next man up: the lead back is down, and this back took the work or is next in line",
              f"  {'BACK':<20} {'TEAM':<4} {'WHERE HE IS':<28} {'WK':>2} {'LATEST':>6} {'USUAL':>5}   "
              f"{'LEAD BACK':<20} {'TAG':<15} {'LATEST':>6} {'USUAL':>5}"]
    for flag in flags:
        back, lead = flag["player"], flag["lead"]
        lines.append(f"  {back['name'][:20]:<20} {PRO_TEAMS.get(back['pro_team'], '?'):<4} "
                     f"{where[back['id']][:28]:<28} {flag['week']:>2} {flag['latest']:>6} {flag['usual']:>5.1f}   "
                     f"{lead['name'][:20]:<20} {lead['injury_status']:<15} {flag['lead_latest']:>6} "
                     f"{flag['lead_usual']:>5.1f}")
    if not flags:
        lines.append("  none")
    lines += ["  LATEST and USUAL are carries plus targets: in the game in week WK, and a game over the three before it.",
              f"  In 2025-26, backs flagged this way averaged {roles.FILL_IN} points a game over their next "
              f"{roles.FILL_IN_WEEKS} weeks.",
              "  Just under half reached 10 a game, and a quarter stayed under 5."]

    tagged = [(lead, usual, latest, wk) for lead, usual, latest, wk in leads(backs, found["team_weeks"])
              if lead["injury_status"] not in HEALTHY]
    lines += ["", "Lead backs carrying a tag right now:"]
    for lead, usual, latest, wk in sorted(tagged, key=lambda row: row[0]["name"]):
        lines.append(f"  {lead['name'][:22]:<22} {PRO_TEAMS.get(lead['pro_team'], '?'):<4} {lead['injury_status']:<15} "
                     f"week {wk}: {latest} touches, usually {usual:.1f}   ({where[lead['id']]})")
    if not tagged:
        lines.append("  none")
    return "\n".join(lines)


if __name__ == "__main__":
    try:
        print(report(collect(ESPN.from_env())))
    except ESPNError as error:
        sys.exit(f"Error: {error}")
