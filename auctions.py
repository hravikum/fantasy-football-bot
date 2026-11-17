"""Every waiver auction this season: how many teams bid, what won, and what the report saw first.

ESPN returns every claim, the losing ones included, so each night's waivers read as sealed-bid
auctions, one per player. This shows how the price depends on how many teams bid, lists the
recent contested auctions, and writes logs/auctions.csv. Each auction is matched to the report's
last look at that player before the run (logs/pool.csv, appended by every waivers.py run). That
match showed that the add rate at 6am doesn't predict a bidding war (bids.py has what does), and
it is how the report's own bids are checked: of the claims it had priced the morning before, how
many each end of its range would have won.

Usage: python3 auctions.py
"""
import csv
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

from bids import all_bids, auctions, to_beat
from espn import ESPN, ESPNError, parse_roster_entry
from waivers import POOL_LOG, percentile

LOG = Path(__file__).with_name("logs") / "auctions.csv"
FIELDS = ["processed", "week", "player_id", "player", "position", "teams", "price", "runner_up", "winner",
          "your_bid", "to_beat", "snapshot_at", "percent_owned", "percent_change", "projection", "band", "report_bid"]
SNAPSHOT_AGE = timedelta(hours=30)       # the 6am report comes about 21 hours before the next claims run
RECENT = timedelta(days=14)
CHECKED = timedelta(days=28)             # how far back the report's own bids are held against what won
BUCKETS = [("1", 1, 1), ("2", 2, 2), ("3-4", 3, 4), ("5 or more", 5, 99)]


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


def bids_checked(won, now):
    """(claims the report had priced, how many its low end would have won, how many its high end).

    The low end is meant to win three in four for a player who would start and half for a stash
    (waivers.STARTER and STASH), so over a month this should land between the two.
    """
    rows = [(a["to_beat"], [int(end) for end in a["snapshot"]["bid"].split("-")]) for a in won
            if a.get("snapshot") and a["snapshot"]["bid"] and now - a["processed"] <= CHECKED]
    return (len(rows), sum(1 for beat, ends in rows if ends[0] > beat),
            sum(1 for beat, ends in rows if ends[-1] > beat))


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
        a["to_beat"] = to_beat(a, me) if a["price"] is not None else None
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
    priced, low, high = bids_checked(won, now)
    if priced:
        lines += ["", f"The report's own bids, last {CHECKED.days} days: of the {priced} claims it had priced the morning "
                      f"before,", f"the low end of its range would have won {low} and the high end {high}."]
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
                "your_bid": a["your_bid"], "to_beat": a.get("to_beat"),
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
