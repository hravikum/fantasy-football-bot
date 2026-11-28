"""What it has taken to win a waiver claim in this league, by the kind of claim.

Every claim is a sealed bid, and once a run is over ESPN returns the losing bids along with the
winner. So each past claim says what a new bid would have had to beat: the winning price, or the
runner-up where the winner was you. The waiver report prices a bid off past claims of the same kind
(waivers.suggest_bid), and auctions.py reports on the same auctions.

There are three kinds because they cost different money here. Through week 10 of 2026 (138 claims),
the smallest bids that would have won half and three quarters of each kind were:
  a kicker or a defense (56)                          $2 and $4
  a back or receiver after a breakout game (20)       $8 and $18, and $38 to win them all
  everyone else (62)                                  $3 and $5

A breakout game is the one thing visible beforehand that told the expensive claims apart: 14 of the 20
that drew three or more teams followed one, and 6 of the 62 that drew one or two did. ESPN's add rate
couldn't: it resets on Tuesday, before the bidding, and on the Monday before it had moved for one of
the eight contested players the pool log holds. The other six of the 20 had no such game, and two
hadn't played that week at all. Whatever moved the league there, most likely news, the box score
didn't show it, and only the write-up's search can.

A next man up (roles.py) is priced with the breakouts. All four flagged before a claim this season
had also had the game, and they went for $6, $11, $19 and $37.

What the money buys is modest. Backs and receivers who cost $10 or more averaged 6.9 points a game
over their next four weeks, against 5.2 for the cheaper ones, and one in five reached 10 a game. So a
breakout game sets the price of a claim. It is not a reason to make one.
"""
import json
from collections import defaultdict
from datetime import datetime, timedelta

from espn import ESPNError, parse_roster_entry
from values import games_played

OUTBID = "FAILED_INVALIDPLAYERSOURCE"    # a live bid that lost: by the time it ran, he was someone else's
NOT_BIDS = {"CANCELED", "PENDING"}
BREAKOUT_POINTS = 10.0                   # a breakout game: at least this many points...
BREAKOUT_JUMP = 5.0                      # ...and this many more than he had averaged before it
BREAKOUT_POSITIONS = {"RB", "WR"}        # ten points is an ordinary game for a quarterback, and no
                                         # tight end's claim has drawn a crowd here
STREAMED, IN_DEMAND, OTHER = "kicker or defense", "in demand", "other"
KINDS = {IN_DEMAND: "Backs and receivers after a breakout game, and next men up",
         OTHER: "Everyone else at quarterback, back, receiver and tight end",
         STREAMED: "Kickers and defenses"}


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
            drop = next((item.get("playerId") for item in transaction.get("items", []) if item.get("type") == "DROP"),
                        None)
            for item in transaction.get("items", []):
                if item.get("type") == "ADD":
                    bids.append({"week": played, "team_id": transaction.get("teamId"),
                                 "status": transaction.get("status"), "bid": transaction.get("bidAmount") or 0,
                                 "player_id": item.get("playerId"), "drop_id": drop,
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


LAST_NIGHT = timedelta(hours=24)         # the morning report shows the run it follows, once
WHY_NOT = {"FAILED_ROSTERLIMIT": "your roster was full",
           "FAILED_AUCTIONBUDGETEXCEEDED": "the bid was more than your FAAB left",
           "FAILED_PLAYERALREADYDROPPED": "the player you were dropping was already gone"}


def last_night(bids, me, now):
    """Your bids in the latest waiver run, if it ran within LAST_NIGHT: how each went, with the winner
    and his price, how many teams claimed the player, and the best losing bid besides yours."""
    done = [b for b in bids if b["processed"] and b["status"] not in NOT_BIDS]
    if not done:
        return []
    latest = max(b["processed"] for b in done)
    if now - latest > LAST_NIGHT:
        return []
    run = [b for b in done if b["processed"].date() == latest.date()]
    found = []
    for mine in (b for b in run if b["team_id"] == me):
        field = [b for b in run if b["player_id"] == mine["player_id"]]
        winner = next((b for b in field if b["status"] == "EXECUTED"), None)
        others = [b["bid"] for b in field if b is not mine and b["status"] == OUTBID]
        found.append({"player_id": mine["player_id"], "drop_id": mine.get("drop_id"), "bid": mine["bid"],
                      "status": mine["status"], "won": mine["status"] == "EXECUTED",
                      "winner": winner["team_id"] if winner else None, "price": winner["bid"] if winner else None,
                      "teams": len({b["team_id"] for b in field}), "next_best": max(others) if others else None})
    return sorted(found, key=lambda r: (not r["won"], -r["bid"]))


def season_record(bids, me):
    """(won, decided by price) over your bids this season. A claim that failed on its own terms, a
    full roster or a drop already gone, never reached the auction and isn't counted."""
    mine = [b for b in bids if b["team_id"] == me and b["status"] in ("EXECUTED", OUTBID)]
    return sum(1 for b in mine if b["status"] == "EXECUTED"), len(mine)


def result_line(result, players, teams):
    """("won" | "lost" | "failed", one line on how a bid of yours went). players and teams map ids to names."""
    name = players.get(result["player_id"], f"player {result['player_id']}")
    if result["won"]:
        if result["next_best"] is not None:
            beaten = f"next best bid ${result['next_best']}"
        elif result["teams"] > 1:
            beaten = "the other claims failed on their own terms"
        else:
            beaten = "nobody else claimed him"
        line = f"Won {name} for ${result['bid']} ({beaten})"
        if result["drop_id"]:
            line += f"; dropped {players.get(result['drop_id'], 'player ' + str(result['drop_id']))}"
        return "won", line
    if result["status"] == OUTBID:
        if result["winner"] is None:
            return "lost", f"Lost {name}: he went to another team before your claim ran"
        team = teams.get(result["winner"], f"team {result['winner']}")
        if result["price"] == result["bid"]:
            return "lost", f"Lost {name} on a tie at ${result['price']}: {team} was ahead in waiver order"
        return "lost", f"Lost {name} to {team}: ${result['price']} against your ${result['bid']}"
    reason = WHY_NOT.get(result["status"], (result["status"] or "unknown").replace("FAILED_", "").lower())
    return "failed", f"{name}: your ${result['bid']} claim didn't run: {reason}"


def to_beat(auction, me):
    """What a new bid on him had to top: the winning price, or the runner-up where the winner was you."""
    if auction["winner"] == me:
        return auction["runner_up"] or 0
    return auction["price"]


def breakout(games, week):
    """His breakout game if that week was one, else None. games: [(week, points)], oldest first."""
    points = dict(games).get(week)
    if points is None:
        return None
    earlier = [scored for played, scored in games if played < week]
    before = sum(earlier) / len(earlier) if earlier else 0.0
    if points >= BREAKOUT_POINTS and points - before >= BREAKOUT_JUMP:
        return {"week": week, "points": points, "before": before}
    return None


def kind(position, in_demand):
    """Which past claims a claim on him is priced against."""
    if position in ("K", "D/ST"):
        return STREAMED
    return IN_DEMAND if in_demand else OTHER


def kind_of(candidate):
    """The kind a claim on this free agent would be: in demand if roles.py or mark() below tagged him."""
    return kind(candidate.get("position"), bool(candidate.get("role") or candidate.get("breakout")))


def history(espn, bids, me, cards=None, final_week=18):
    """{kind: [what each past claim of that kind took to win, oldest first]} for this season.

    A past claim is in demand if the game before it was a breakout, whatever week the claim ran in.
    Past next-man-up flags aren't replayed: that takes every team's backfield as it stood that week.
    cards: player cards already in hand, so only the missing ones are fetched.
    """
    won = [a for a in auctions(bids, me) if a["price"] is not None]
    cards = dict(cards or {})
    cards.update(espn.player_cards(sorted({a["player_id"] for a in won} - set(cards)), final_week))
    past = {name: [] for name in KINDS}
    for auction in won:
        card = cards.get(auction["player_id"])
        if card is None:
            continue
        position = parse_roster_entry({"player": card}, auction["week"], espn.season)["position"]
        hot = position in BREAKOUT_POSITIONS and breakout(
            games_played(card, espn.season, auction["week"]), auction["week"] - 1)
        past[kind(position, bool(hot))].append(to_beat(auction, me))
    return past


def mark(free_agents, cards, season, week, team_weeks):
    """Tag each free-agent back or receiver coming off a breakout game, in place, and return them.

    team_weeks: {pro_team: finished weeks, oldest first}, as roles.finished_weeks gives it, so a
    Monday morning sees Sunday. A game from before a bye doesn't count: the league has already had
    a waiver run to bid on it.
    """
    found = []
    for player in free_agents:
        weeks = team_weeks.get(player["pro_team"]) or []
        card = cards.get(player["id"])
        if player["position"] not in BREAKOUT_POSITIONS or card is None or not weeks or weeks[-1] < week - 1:
            continue
        game = breakout(games_played(card, season, weeks[-1] + 1), weeks[-1])
        if game:
            player["breakout"] = game
            found.append(player)
    return found


def describe(candidate):
    """Why he is priced as in demand, in a few words."""
    if candidate.get("role"):
        return f"{candidate['name']} (next man up)"
    game = candidate["breakout"]
    return (f"{candidate['name']} ({game['points']:.1f} points in week {game['week']}, "
            f"{game['before']:.1f} a game before)")
