"""Every waiver auction this season: how many teams bid, what won, and what the report saw first.

ESPN returns every claim, the losing ones included, so each night's waivers read as sealed-bid
auctions, one per player. This shows how the price depends on how many teams bid, lists the
recent contested auctions, and writes logs/auctions.csv. Each auction is matched to the report's
last look at that player before the run (logs/pool.csv, appended by every waivers.py run). That
match is what will show whether anything the report can see at 6am, the add rate above all,
predicts a bidding war.

Usage: python3 auctions.py
"""
import csv
import json
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

from espn import ESPN, ESPNError, parse_roster_entry
from waivers import POOL_LOG, percentile

LOG = Path(__file__).with_name("logs") / "auctions.csv"
FIELDS = ["processed", "week", "player_id", "player", "position", "teams", "price", "runner_up", "winner",
          "your_bid", "snapshot_at", "percent_owned", "percent_change", "projection", "band", "report_bid"]
OUTBID = "FAILED_INVALIDPLAYERSOURCE"    # a live bid that lost: by the time it ran, he was someone else's
NOT_BIDS = {"CANCELED", "PENDING"}
SNAPSHOT_AGE = timedelta(hours=30)       # the 6am report comes about 21 hours before the next claims run
RECENT = timedelta(days=14)
BUCKETS = [("1", 1, 1), ("2", 2, 2), ("3-4", 3, 4), ("5 or more", 5, 99)]


def all_bids(espn, week):
    """Every waiver bid ESPN returns, whatever its status. espn.waiver_claims() keeps only winners."""
    rules = {"transactions": {"filterType": {"value": ["WAIVER"]}}}
    bids, seen = [], set()
    for played in range(1, week + 1):
        try:
            data = espn.league("mTransactions2", week=played, headers={"x-fantasy-filter": json.dumps(rules)})
        except ESPNError:
            continue
        for transaction in data.get("transactions", []):
            key = transaction.get("id") or id(transaction)
            if key in seen:
                continue
            seen.add(key)
            stamp = transaction.get("processDate")
            for item in transaction.get("items", []):
                if item.get("type") == "ADD":
                    bids.append({"week": played, "team_id": transaction.get("teamId"),
                                 "status": transaction.get("status"), "bid": transaction.get("bidAmount") or 0,
                                 "player_id": item.get("playerId"),
                                 "processed": datetime.fromtimestamp(stamp / 1000) if stamp else None})
    return bids


def auctions(bids, me):
    """One auction per player per waiver run, oldest first.

    teams counts everyone who claimed him, even a claim that failed on its own terms (a drop
    already used, a full roster): that's still a team that wanted him. runner_up is only the
    best of the live bids that lost, the number the winner actually had to beat.
    """
    grouped = defaultdict(list)
    for bid in bids:
        if bid["status"] not in NOT_BIDS and bid["processed"]:
            grouped[(bid["player_id"], bid["processed"].date())].append(bid)
    found = []
    for (player_id, _), field in grouped.items():
        winner = next((b for b in field if b["status"] == "EXECUTED"), None)
        lost = [b["bid"] for b in field if b["status"] == OUTBID]
        found.append({"processed": (winner or field[0])["processed"], "week": field[0]["week"],
                      "player_id": player_id, "teams": len({b["team_id"] for b in field}),
                      "price": winner["bid"] if winner else None,
                      "winner": winner["team_id"] if winner else None,
                      "runner_up": max(lost) if lost else None,
                      "your_bid": next((b["bid"] for b in field if b["team_id"] == me), None)})
    return sorted(found, key=lambda a: a["processed"])


def read_pool(path=None):
    """{player_id: [snapshots, oldest first]} from the waiver report's log; {} before its first run."""
    path = path or POOL_LOG
    pool = defaultdict(list)
    if path.exists():
        with open(path, newline="") as handle:
            for row in csv.DictReader(handle):
                row["logged_at"] = datetime.fromisoformat(row["logged_at"])
                pool[int(row["player_id"])].append(row)
    return pool


def snapshot(auction, pool):
    """The report's last look at him before the run, if it was recent enough to be that run's report."""
    seen = [row for row in pool.get(auction["player_id"], [])
            if row["logged_at"] < auction["processed"] <= row["logged_at"] + SNAPSHOT_AGE]
    return seen[-1] if seen else None


def by_teams(found):
    """[(how many teams bid, auctions, median price, highest)] over the auctions someone won."""
    rows = []
    for label, low, high in BUCKETS:
        prices = [a["price"] for a in found if a["price"] is not None and low <= a["teams"] <= high]
        if prices:
            rows.append((label, len(prices), percentile(prices, 0.5), max(prices)))
    return rows


def collect(espn, pool_path=None):
    league = espn.league("mSettings", "mTeam")
    week = league["scoringPeriodId"]
    me = espn.my_team(league["teams"])["id"]
    names = {team["id"]: team.get("name") or f"team {team['id']}" for team in league["teams"]}
    found = auctions(all_bids(espn, week), me)
    cards = espn.player_cards(sorted({a["player_id"] for a in found}), week)
    pool = read_pool(pool_path)
    for a in found:
        card = cards.get(a["player_id"])
        player = parse_roster_entry({"player": card}, week, espn.season) if card else {}
        a["player"] = player.get("name", str(a["player_id"]))
        a["position"] = player.get("position", "?")
        a["winner"] = names.get(a["winner"], a["winner"])
        a["snapshot"] = snapshot(a, pool)
    return league["settings"]["name"], week, found


def _money(amount):
    return "-" if amount is None else f"${amount}"


def report(name, week, found, now):
    won = [a for a in found if a["price"] is not None]
    lines = [f"Waiver auctions in {name}, weeks 1-{week}", ""]
    if not won:
        return "\n".join(lines + ["No claims have processed yet."])
    lines += [f"Auctions someone won: {len(won)}", "",
              f"{'TEAMS BIDDING':<14} {'AUCTIONS':>8} {'MEDIAN PRICE':>13} {'HIGHEST':>8}"]
    for label, count, median, highest in by_teams(won):
        lines.append(f"{label:<14} {count:>8} {'$' + format(median, '.0f'):>13} {'$' + str(highest):>8}")

    recent = [a for a in won if a["teams"] > 1 and now - a["processed"] <= RECENT]
    lines += ["", f"Contested in the last {RECENT.days} days:"]
    if not recent:
        lines.append("  none")
    else:
        lines.append(f"  {'DATE':<10}  {'PLAYER':<22} {'POS':<5} {'TEAMS':>5} {'PRICE':>6} {'NEXT':>5} "
                     f"{'YOU':>4} {'OWN%':>6} {'+/-':>6}  REPORT")
        for a in recent:
            seen = a["snapshot"]
            if seen:
                owned, change = f"{float(seen['percent_owned']):.0f}%", f"{float(seen['percent_change']):+.1f}"
                said = f"${seen['bid']}" if seen["bid"] else "unlisted"
            else:
                owned, change, said = "-", "-", "-"
            lines.append(f"  {a['processed']:%a %b %d}  {a['player'][:22]:<22} {a['position']:<5} {a['teams']:>5} "
                         f"{_money(a['price']):>6} {_money(a['runner_up']):>5} {_money(a['your_bid']):>4} "
                         f"{owned:>6} {change:>6}  {said}")
        lines += ["", "OWN%, +/- and REPORT are what the waiver report saw before that run (logs/pool.csv).",
                  "\"unlisted\" means it saw him but left him off the shortlist; \"-\" means it never saw him."]
    return "\n".join(lines)


def write_log(found, path=None):
    path = path or LOG
    path.parent.mkdir(exist_ok=True)
    with open(path, "w", newline="") as handle:          # rewritten each run: ESPN keeps the history
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        for a in found:
            seen = a.get("snapshot") or {}
            writer.writerow({
                "processed": a["processed"].isoformat(timespec="minutes"), "week": a["week"],
                "player_id": a["player_id"], "player": a["player"], "position": a["position"],
                "teams": a["teams"], "price": a["price"], "runner_up": a["runner_up"], "winner": a["winner"],
                "your_bid": a["your_bid"],
                "snapshot_at": seen["logged_at"].isoformat(timespec="minutes") if seen else None,
                "percent_owned": seen.get("percent_owned"), "percent_change": seen.get("percent_change"),
                "projection": seen.get("projection"), "band": seen.get("band"), "report_bid": seen.get("bid")})
    return len(found)


def main():
    espn = ESPN.from_env()
    name, week, found = collect(espn)
    print(report(name, week, found, datetime.now()))
    print(f"\n{write_log(found)} auctions written to {LOG}")


if __name__ == "__main__":
    try:
        main()
    except ESPNError as error:
        sys.exit(f"Error: {error}")
