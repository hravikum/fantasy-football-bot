"""What waiver claims actually cost in this league.

Reads every executed claim this season, prints the price distribution and who has spent what,
and writes them to logs/claims.csv. The bids in the waiver report are priced off the same claims
split by kind (bids.py), and the report prints those prices itself.

Usage: python3 claims.py
"""
import csv
import sys
from collections import Counter
from pathlib import Path

from espn import ESPN, ESPNError
from waivers import percentile

LOG = Path(__file__).with_name("logs") / "claims.csv"
FIELDS = ["processed", "week", "team", "bid", "player"]


def collect(espn):
    league = espn.league("mSettings", "mTeam")
    week = league["scoringPeriodId"]
    names = {team["id"]: team.get("name") or f"team {team['id']}" for team in league["teams"]}
    claims = espn.waiver_claims(range(1, week + 1))
    players = espn.player_cards([pid for claim in claims for pid in claim["added"]], week)
    for claim in claims:
        claim["team"] = names.get(claim["team_id"], f"team {claim['team_id']}")
        claim["player"] = ", ".join(players.get(pid, {}).get("fullName", str(pid)) for pid in claim["added"])
    return league["settings"]["name"], week, claims


def write_log(claims, path=None):
    path = path or LOG
    path.parent.mkdir(exist_ok=True)
    with open(path, "w", newline="") as handle:          # rewritten each run: ESPN is the record
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        for claim in sorted(claims, key=lambda c: (c["week"], -c["bid"])):
            writer.writerow({"processed": claim["processed"].isoformat(timespec="minutes")
                             if claim["processed"] else "", "week": claim["week"],
                             "team": claim["team"], "bid": claim["bid"], "player": claim["player"]})
    return len(claims)


def report(name, week, claims):
    prices = [claim["bid"] for claim in claims]
    lines = [f"Waiver claims in {name}, weeks 1-{week}", ""]
    if not claims:
        return "\n".join(lines + ["No claims have processed yet."])

    free = sum(1 for price in prices if price == 0)
    lines.append(f"{len(claims)} executed claims, {free} of them free")
    lines.append(f"median ${percentile(prices, 0.5):.0f}   75th ${percentile(prices, 0.75):.0f}   "
                 f"90th ${percentile(prices, 0.90):.0f}   highest ${max(prices):.0f}")
    dearest = max(claims, key=lambda c: c["bid"])
    lines.append(f"most expensive: {dearest['player']} for ${dearest['bid']} "
                 f"({dearest['team']}, week {dearest['week']})")

    spenders = Counter()
    for claim in claims:
        spenders[claim["team"]] += claim["bid"]
    if any(spenders.values()):
        lines += ["", "Spent so far: " + ", ".join(f"{team} ${spent}" for team, spent in spenders.most_common(5))]
    return "\n".join(lines)


def main():
    espn = ESPN.from_env()
    name, week, claims = collect(espn)
    print(report(name, week, claims))
    print(f"\n{write_log(claims)} claims written to {LOG}")


if __name__ == "__main__":
    try:
        main()
    except ESPNError as error:
        sys.exit(f"Error: {error}")
