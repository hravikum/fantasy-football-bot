"""Step 1: read-only check. Confirms the ESPN connection and prints what the bot sees.

Usage: python check.py
"""
import sys

from espn import BENCH, ESPN, IR, PRO_TEAMS, SLOT_NAMES, SLOT_ORDER, ESPNError, parse_roster_entry

OUT_STATUSES = {"OUT", "INJURY_RESERVE", "SUSPENSION"}


def main():
    espn = ESPN.from_env()
    league = espn.league("mSettings", "mTeam")
    settings = league["settings"]
    week = league["scoringPeriodId"]
    team = espn.my_team(league["teams"])
    team_name = team.get("name") or f"{team.get('location', '')} {team.get('nickname', '')}".strip()

    print(f"Connected: {settings['name']} ({espn.season}), week {week}")
    print(f"Your team: {team_name} (team id {team['id']})\n")

    # League settings the bot relies on
    counts = {int(k): v for k, v in settings["rosterSettings"]["lineupSlotCounts"].items() if v}
    print("Lineup slots: " + ", ".join(f"{SLOT_NAMES[s]} x{counts[s]}" for s in SLOT_ORDER if s in counts))
    unknown = sorted(set(counts) - set(SLOT_NAMES))
    if unknown:
        print(f"  !! Slot ids the bot doesn't know yet: {unknown}")
    ppr = next((i.get("points") for i in settings["scoringSettings"]["scoringItems"] if i.get("statId") == 53), 0)
    keepers = settings.get("draftSettings", {}).get("keeperCount")
    print(f"Teams: {settings['size']}   Points per reception: {ppr}   Keepers: {keepers}")

    acq = settings["acquisitionSettings"]
    if acq.get("isUsingAcquisitionBudget"):
        budget = acq.get("acquisitionBudget", 0)
        spent = team.get("transactionCounter", {}).get("acquisitionBudgetSpent", 0)
        print(f"FAAB: ${budget - spent} left of ${budget} (minimum bid ${acq.get('minimumBid', 0)})")
    else:
        print("FAAB: not used by this league")
    print(f"Waivers process on: {acq.get('waiverProcessDays')} (hour setting: {acq.get('waiverProcessHour')})\n")

    # Roster for the current week
    roster = espn.league("mRoster", week=week)
    entries = next(t for t in roster["teams"] if t["id"] == team["id"])["roster"]["entries"]
    players = [parse_roster_entry(e, week, espn.season) for e in entries]
    players.sort(key=lambda p: (SLOT_ORDER.index(p["slot"]) if p["slot"] in SLOT_ORDER else 99,
                                -(p["projection"] or 0)))
    games = espn.pro_games(week)

    print(f"{'SLOT':<5} {'PLAYER':<24} {'POS':<5} {'OPP':<4} {'KICKOFF':<12} {'STATUS':<15} {'PROJ':>5}  LOCKED")
    for p in players:
        game = games.get(p["pro_team"])
        opp, kickoff = (PRO_TEAMS.get(game[0], "?"), game[1].strftime("%a %I:%M%p").replace(" 0", " ")) if game else ("", "BYE")
        proj = "" if p["projection"] is None else f"{p['projection']:.1f}"
        print(f"{SLOT_NAMES.get(p['slot'], p['slot']):<5} {p['name'][:24]:<24} {p['position']:<5} {opp:<4} "
              f"{kickoff:<12} {p['injury_status']:<15} {proj:>5}  {'yes' if p['locked'] else ''}")

    # Sanity checks
    playing = [p for p in players if p["pro_team"] in games]
    with_proj = [p for p in playing if p["projection"] is not None]
    print(f"\nProjections found for {len(with_proj)} of {len(playing)} players with a game this week")
    heads_up = []
    for p in players:
        if p["slot"] in (BENCH, IR):
            if p["slot"] == IR and p["injury_status"] == "ACTIVE":
                heads_up.append(f"{p['name']} is in an IR slot but listed ACTIVE; ESPN may block add/drops until he's moved")
        elif p["pro_team"] not in games:
            heads_up.append(f"{p['name']} is starting but on bye")
        elif p["injury_status"] in OUT_STATUSES:
            heads_up.append(f"{p['name']} is starting but listed {p['injury_status']}")
    for line in heads_up:
        print(f"Heads-up: {line}")


if __name__ == "__main__":
    try:
        main()
    except ESPNError as e:
        sys.exit(f"Error: {e}")
