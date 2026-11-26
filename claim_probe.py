"""Does ESPN take a waiver claim from the bot? The first step before Slack's Bid button.

Places one $0 claim, on a player nobody wants, for you to cancel in ESPN straight away. Three runs:

    python3 claim_probe.py                    read-only: your FAAB, the least-wanted players on
                                              waivers, your bench, and the claim it would send
    python3 claim_probe.py --send ADD DROP    place the $0 claim (player ids from the first run)
                                              and read it back as pending
    python3 claim_probe.py --check CLAIM      after you cancel it in ESPN: is it gone?

A claim you leave in place goes through at the next waiver run, about 3am any night but Monday's:
you would add ADD and lose DROP. So cancel it in ESPN as soon as --send says it is pending.

Everything printed also goes to logs/claim_probe.log.
"""
import json
import sys
from datetime import datetime
from pathlib import Path

from espn import BENCH, ESPN, PRO_TEAMS, ESPNError, claim_body, parse_roster_entry

LOG = Path(__file__).with_name("logs") / "claim_probe.log"


def say(text=""):
    print(text)
    LOG.parent.mkdir(exist_ok=True)
    with open(LOG, "a") as log:
        log.write(text + "\n")


def on_waivers(espn, week, count=8, ids=None):
    """Players on waivers (not cleared free agents, who would be added at once), least rostered first."""
    rules = {"players": {"filterStatus": {"value": ["WAIVERS"]}, "limit": 300,
                         "sortPercOwned": {"sortPriority": 1, "sortAsc": True}}}
    if ids:
        rules["players"]["filterIds"] = {"value": list(ids)}
    data = espn.league("kona_player_info", week=week, headers={"x-fantasy-filter": json.dumps(rules)})
    found = []
    for entry in data.get("players", []):
        if entry.get("status") != "WAIVERS":
            continue
        player = parse_roster_entry(entry, week, espn.season)
        player["clears"] = entry.get("waiverProcessDate")
        found.append(player)
    return found[:count]


def your_bench(espn, team_id, week):
    """Bench players whose games haven't locked, least valuable first: the cheapest drop if the claim
    were ever to go through."""
    players = [parse_roster_entry(e, week, espn.season) for e in espn.roster_entries(team_id, week)]
    bench = [p for p in players if p["slot"] == BENCH and not p["locked"]]
    return sorted(bench, key=lambda p: (p["season_projection"] or 0, p["projection"] or 0))


def find(data, claim_id):
    """Every transaction in an ESPN answer that is the claim, or cancels it."""
    hits = []
    if isinstance(data, dict):
        if data.get("id") == claim_id or data.get("relatedTransactionId") == claim_id:
            hits.append(data)
        for value in data.values():
            hits += find(value, claim_id)
    elif isinstance(data, list):
        for value in data:
            hits += find(value, claim_id)
    return hits


def read_back(espn, week, claim_id):
    """(still in your pending claims?, every record of it ESPN has this week)."""
    pending = find(espn.league("mPendingTransactions"), claim_id)
    records = find(espn.league("mTransactions2", week=week), claim_id)
    for record in pending + records:
        say(f"  {record.get('type')} {record.get('status')} id {record.get('id')}"
            + (f" (cancels {record['relatedTransactionId']})" if record.get("relatedTransactionId") else "")
            + (f" bid ${record['bidAmount']}" if "bidAmount" in record else ""))
    return any(r.get("id") == claim_id and r.get("status") == "PENDING" for r in pending), records


def _name(p):
    return f"{p['name']} ({p['position']}, {PRO_TEAMS.get(p['pro_team'], '?')})"


def main(argv):
    espn = ESPN.from_env()
    league = espn.league("mSettings", "mTeam")
    week = league["scoringPeriodId"]
    team = espn.my_team(league["teams"])
    say(f"--- {datetime.now():%Y-%m-%d %H:%M} claim_probe.py {' '.join(argv)}")

    if argv[:1] == ["--check"]:
        still, records = read_back(espn, week, argv[1])
        if still:
            say("STILL PENDING: cancel it in ESPN before the 3am waiver run, then run --check again.")
            return 1
        say("Not in your pending claims any more: cancelled." if records else
            "ESPN has no record of that claim id this week; check the id.")
        return 0

    if argv[:1] == ["--send"]:
        add_id, drop_id = int(argv[1]), int(argv[2])
        if not on_waivers(espn, week, ids=[add_id]):
            say(f"Refusing: player {add_id} isn't on waivers. A cleared free agent would be added at once.")
            return 1
        bench = {p["id"]: p for p in your_bench(espn, team["id"], week)}
        if drop_id not in bench:
            say(f"Refusing: player {drop_id} isn't on your bench with his game still to come.")
            return 1
        say(f"Sending: {json.dumps(claim_body(team['id'], week, add_id, drop_id, 0))}")
        try:
            result = espn.claim(team["id"], week, add_id, drop_id, bid=0)
        except ESPNError as error:
            say(f"ESPN refused or answered oddly: {error}")
            return 1
        say(f"ESPN answered: {json.dumps(result)[:1500]}")
        say("Reading it back:")
        still, _ = read_back(espn, week, result.get("id"))
        say(f"\nPending: {'yes' if still else 'not found in mPendingTransactions'}. Claim id {result.get('id')}")
        say(f"Now cancel it in ESPN (your pending claims), then run:  python3 claim_probe.py --check {result.get('id')}")
        return 0

    acquisition = league["settings"]["acquisitionSettings"]
    spent = team.get("transactionCounter", {}).get("acquisitionBudgetSpent", 0)
    say(f"Week {week}. FAAB ${acquisition.get('acquisitionBudget', 0) - spent} left; minimum bid "
        f"${acquisition.get('minimumBid', 0)}. Waivers run on: {acquisition.get('waiverProcessDays')}")
    say("\nLeast-wanted players on waivers (pick one to claim):")
    for p in on_waivers(espn, week):
        clears = datetime.fromtimestamp(p["clears"] / 1000).strftime("%a %b %d %I:%M %p") if p.get("clears") else "?"
        say(f"  {p['id']:>9}  {_name(p):<38} {p['percent_owned']:>5.1f}% rostered, clears {clears}")
    say("\nYour bench, least valuable first (pick one to drop if the claim ever went through):")
    for p in your_bench(espn, team["id"], week):
        say(f"  {p['id']:>9}  {_name(p):<38} season {p['season_projection'] or 0:>6.1f}")
    say("\nThen:  python3 claim_probe.py --send <claim id> <drop id>")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except ESPNError as error:
        say(f"Error: {error}")
        sys.exit(1)
