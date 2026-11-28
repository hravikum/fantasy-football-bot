"""Keeper value, roughly: what a player is worth next season against what he'd cost to keep.

This league keeps one player. Only a player drafted in the 9th round or later, or picked up during
the season, can be kept, and he costs a 9th-round pick. One kept in the 9th this season costs a 5th
next season, and one kept in the 5th costs a 2nd. The draft says who went in which round and who
was kept.

Worth comes from DynastyProcess's free dynasty values (FantasyPros' dynasty consensus, 1QB),
read as a draft slot: the 40th-ranked player is worth about a 3rd-round pick in a 14-team draft.
His surplus is his value less the value of the player ranked where his cost pick falls (your
pick in a round is random, so the middle of it). Rough on purpose: dynasty rankings count several
seasons, so they rate young players a little above what one keeper year is worth.

Usage:
  python3 keepers.py    your keeper candidates, and the best ones on other teams
"""
import csv
import io
import math
import sys

import snaps
from espn import ESPN, ESPNError

FILES = "https://raw.githubusercontent.com/dynastyprocess/data/master/files"
VALUES_MAX_AGE = 86400            # DynastyProcess scrapes FantasyPros daily
IDS_MAX_AGE = 7 * 86400
COST_AFTER = {None: 9, 9: 5, 5: 2}  # the round he was kept in this season -> the round he costs next season
FIRST_KEEPABLE = 9                 # drafted earlier than this round and not kept: he can't be kept
SHOWN = 8


def load():
    """{ESPN id: {"rank", "value", "age", "name"}} from DynastyProcess, rank 1 the most valuable."""
    values = csv.DictReader(io.StringIO(snaps._fetch(f"{FILES}/values-players.csv",
                                                     snaps.CACHE / "dp_values.csv", VALUES_MAX_AGE)))
    ids = csv.DictReader(io.StringIO(snaps._fetch(f"{FILES}/db_playerids.csv",
                                                  snaps.CACHE / "dp_playerids.csv", IDS_MAX_AGE)))
    to_espn = {row["fantasypros_id"]: snaps._espn_id(row.get("espn_id")) for row in ids if row.get("fantasypros_id")}
    rows = [row for row in values if _number(row.get("ecr_1qb")) is not None]
    rows.sort(key=lambda row: _number(row["ecr_1qb"]))
    found = {}
    for rank, row in enumerate(rows, start=1):
        espn_id = to_espn.get(row.get("fp_id"))
        if espn_id is not None and espn_id not in found:
            found[espn_id] = {"rank": rank, "value": _number(row.get("value_1qb")) or 0.0,
                              "age": _number(row.get("age")), "name": row.get("player")}
    return found


def _number(text):
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def slot_values(values):
    """[value of the player at each rank], best first, for pricing a pick."""
    return [v["value"] for v in sorted(values.values(), key=lambda v: v["rank"])]


def pick_value(cost_round, teams, by_rank):
    """What a pick in the middle of that round tends to get you."""
    rank = (cost_round - 1) * teams + math.ceil(teams / 2)
    return by_rank[min(rank, len(by_rank)) - 1] if by_rank else 0.0


def kept_rounds(draft):
    """{player id: the round he was kept in} from ESPN's draft detail."""
    return {pick["playerId"]: pick.get("roundId") for pick in (draft or {}).get("picks", [])
            if pick.get("keeper") and pick.get("playerId")}


def drafted_rounds(draft):
    """{player id: the round he was drafted in}; anyone missing was picked up during the season."""
    return {pick["playerId"]: pick.get("roundId") for pick in (draft or {}).get("picks", [])
            if pick.get("playerId") and pick.get("roundId")}


def keepable(player_id, kept, drafted):
    """Kept this season, drafted in the 9th round or later, or not drafted at all."""
    return player_id in kept or drafted.get(player_id, FIRST_KEEPABLE) >= FIRST_KEEPABLE


def attach(players, values, kept, teams, drafted=None):
    """Give each player found in the values a "keeper" dict, in place; everyone else gets None.
    A player who can't be kept gets None too, and "not_keepable" says so."""
    by_rank = slot_values(values)
    drafted = drafted or {}
    for player in players:
        found = values.get(player["id"])
        cost = COST_AFTER.get(kept.get(player["id"]))
        player["not_keepable"] = not keepable(player["id"], kept, drafted)
        if not found or cost is None or player["not_keepable"]:
            player["keeper"] = None
            continue
        player["keeper"] = {"rank": found["rank"], "value": found["value"], "age": found["age"],
                            "worth_round": math.ceil(found["rank"] / teams), "cost_round": cost,
                            "surplus": found["value"] - pick_value(cost, teams, by_rank)}
    return players


def surplus(player):
    return (player.get("keeper") or {}).get("surplus", 0.0)


def best(roster):
    """(the player you'd keep, his surplus); (None, 0) when nobody beats the pick he'd cost."""
    top = max(roster, key=surplus, default=None)
    return (top, surplus(top)) if top is not None and surplus(top) > 0 else (None, 0.0)


def ordinal(n):
    return f"{n}{'th' if 10 <= n % 100 <= 20 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')}"


def label(player):
    k = player["keeper"]
    age = f", {k['age']:.0f}" if k.get("age") else ""
    return (f"{player['name']} ({player['position']}{age}): worth a round-{k['worth_round']} pick, "
            f"costs a {ordinal(k['cost_round'])}")


def describe(rosters, names, me):
    mine = sorted((p for p in rosters[me] if p.get("keeper")), key=lambda p: -surplus(p))
    lines = ["Your keeper candidates (dynasty value less what the pick he'd cost tends to get):"]
    for player in mine[:SHOWN]:
        lines.append(f"  {surplus(player):>+6.0f}  {label(player)}")
    keeper, value = best(rosters[me])
    lines.append(f"Best now: {keeper['name']} ({value:+.0f})" if keeper else "Nobody on your roster beats the pick he'd cost.")
    early = [p for p in rosters[me] if p.get("not_keepable")]
    if early:
        lines.append(f"Drafted before round {FIRST_KEEPABLE}, so not keepable: "
                     + ", ".join(p["name"] for p in early))
    lines += ["", "Better keepers on other teams (each team's own best is marked; they'll want to keep him):"]
    others = []
    for team, roster in rosters.items():
        if team == me:
            continue
        theirs, _ = best(roster)
        others += [(p, team, p is theirs) for p in roster if surplus(p) > value]
    for player, team, own in sorted(others, key=lambda o: -surplus(o[0]))[:SHOWN * 2]:
        lines.append(f"  {surplus(player):>+6.0f}  {label(player)}, {names[team]}{'  (their best)' if own else ''}")
    lines += ["", "Values: DynastyProcess (FantasyPros dynasty consensus, 1QB). They count several seasons, so "
                  "young players read a little high for a one-year keeper."]
    return "\n".join(lines)


def main():
    import trades
    espn = ESPN.from_env()
    ctx = trades.gather(espn)
    draft = espn.league("mDraftDetail").get("draftDetail", {})
    values, kept, drafted = load(), kept_rounds(draft), drafted_rounds(draft)
    print(f"Draft: {len(drafted)} picks read, {len(kept)} marked as keepers")
    for roster in ctx["rosters"].values():
        attach(roster, values, kept, len(ctx["rosters"]), drafted)
    print(describe(ctx["rosters"], ctx["names"], ctx["me"]))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except ESPNError as error:
        sys.exit(f"Error: {error}")
