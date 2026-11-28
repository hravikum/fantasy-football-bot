"""Trade finder: deals a contender should want that leave you with a better keeper.

Out of the playoff race, the weeks after the regular season are worth nothing to you, and to a
contender they are the season. So for each team with a real chance at the playoffs (odds.py) it
tries every one-for-one deal where you get a player who'd be a better keeper than your best one now
(keepers.py) and who isn't that team's own best keeper, giving anyone of yours; then two-for-one
deals from the players of yours they'd gain most from. trades.analyze values both lineups through
the playoffs, the same way it values an offer that arrives.

A deal is listed when the other team gains in the playoff weeks and loses little before them, and
your chance of finishing last, re-simulated with both teams' points as the deal changes them,
rises by no more than MAX_RISE. For each player you could get, the deal that costs you least is
listed, with the other offers that would work as fallbacks; best keeper first.

Read-only, like the rest of the trade code: it never proposes a trade. Send the ones you like in ESPN.

Usage:
  python3 finder.py           print the deals
  python3 finder.py --post    and post them to Slack
"""
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from itertools import combinations

import keepers
import mailer
import odds
import slack
import trades
from espn import ESPN, ESPNError

CONTENDER = 0.20       # chance of a playoff spot that makes a team a buyer
GETS = 4               # per team: its best keepers you could get, its own best left out
TWO_FOR_ONE = 4        # your players a team gains most from, paired into two-for-one deals
THEIR_MIN_GAIN = 5.0   # points a team must gain over the playoff weeks: nobody trades for one
THEIR_MAX_LOSS = 3.0   # points it may lose before them
MAX_RISE = 0.02        # how much a deal may raise your chance of finishing last
CHECKED = 3            # per player you'd get: deals re-simulated in full, cheapest to you first
ALSO = 3               # other offers for the same player, listed as fallbacks
SHOWN = 6
SIMS = 10000


def gains_by_period(weeks, left):
    """{matchup period: points the deal adds} over the regular season still to play."""
    gain = {number: after - before for number, before, after in weeks}
    return {period: sum(gain.get(w, 0.0) for w in ws) for period, ws in left.items()}


def evaluate(ctx, partner, give, get, left, my_value):
    """The deal as the finder sees it, or None when a player isn't where the rosters say."""
    result = trades.analyze(ctx, partner, {p["id"] for p in give}, {p["id"] for p in get})
    if len(result["sent"]) != len(give) or len(result["received"]) != len(get):
        return None
    w_you, w_them = trades.windows(result["you"]["weeks"], ctx), trades.windows(result["them"]["weeks"], ctx)
    gone = {p["id"] for p in give} | {p["id"] for p in result["you"]["dropped"]}
    after = [p for p in ctx["rosters"][ctx["me"]] if p["id"] not in gone] + list(get) + result["you"]["added"]
    keeper, value = keepers.best(after)
    return {"partner": partner, "give": list(give), "get": list(get), "result": result,
            "keeper": keeper, "keeper_gain": value - my_value,
            "you_regular": w_you["regular"], "them_regular": w_them["regular"], "them_playoffs": w_them["playoffs"],
            "you_by_period": gains_by_period(result["you"]["weeks"], left),
            "them_by_period": gains_by_period(result["them"]["weeks"], left)}


def acceptable(deal):
    """Whether a contender has reason to say yes: more in the playoffs, not much less before."""
    return deal["them_playoffs"] >= THEIR_MIN_GAIN and deal["them_regular"] >= -THEIR_MAX_LOSS and deal["keeper_gain"] > 0


def find(ctx, model, left):
    """(deals worth sending, best first; your chance of last now; your best keeper now, his surplus)."""
    me = ctx["me"]
    now = model.run()
    base = now[me][0]
    per_point = {period: (after - base) / odds.BUMP for period, after in odds.point_values(model, me).items()}
    mine = ctx["rosters"][me]
    my_keeper, my_value = keepers.best(mine)

    found, seen = [], set()
    def consider(deal):
        if deal is None:
            return
        key = (deal["partner"], frozenset(p["id"] for p in deal["give"]), frozenset(p["id"] for p in deal["get"]))
        if key not in seen:
            seen.add(key)
            found.append(deal)

    for partner, roster in ctx["rosters"].items():
        if partner == me or now.get(partner, (0, 0))[1] < CONTENDER:
            continue
        their_keeper, _ = keepers.best(roster)
        gets = sorted((p for p in roster if p is not their_keeper and keepers.surplus(p) > my_value),
                      key=lambda p: -keepers.surplus(p))[:GETS]
        for get in gets:
            singles = [evaluate(ctx, partner, [give], [get], left, my_value) for give in mine]
            singles = [d for d in singles if d is not None]
            for deal in singles:
                consider(deal)
            helpful = sorted(singles, key=lambda d: -d["them_playoffs"])[:TWO_FOR_ONE]
            for one, two in combinations(helpful, 2):
                consider(evaluate(ctx, partner, one["give"] + two["give"], [get], left, my_value))

    # One deal per player you'd get: the one that costs you least, checked in full; the others that
    # would also work are kept as fallbacks in case the first offer is turned down.
    groups = defaultdict(list)
    for deal in found:
        if acceptable(deal):
            deal["screened_rise"] = sum(per_point.get(p, 0.0) * g for p, g in deal["you_by_period"].items())
            groups[(deal["partner"], frozenset(p["id"] for p in deal["get"]))].append(deal)
    chosen = []
    for group in groups.values():
        group.sort(key=lambda d: (d["screened_rise"], -d["you_regular"]))
        for deal in group[:CHECKED]:
            extra = {period: {me: deal["you_by_period"].get(period, 0.0),
                              deal["partner"]: deal["them_by_period"].get(period, 0.0)} for period in model.periods}
            deal["last_after"] = model.run(extra)[me][0]
            if deal["last_after"] - base <= MAX_RISE:
                deal["their_playoff_odds"] = now[deal["partner"]][1]
                deal["also"] = [d for d in group if d is not deal and d["screened_rise"] <= MAX_RISE][:ALSO]
                chosen.append(deal)
                break
    chosen.sort(key=lambda d: (-d["keeper_gain"], -d["you_regular"]))
    return chosen, base, (my_keeper, my_value)


def describe(deals, base, mine, ctx, deadline=None):
    names = ctx["names"]
    lines = [f"Trade ideas, {datetime.now():%a %b %d %I:%M %p}"
             + (f" (deadline {deadline:%a %b %d, %I:%M %p})" if deadline else ""), ""]
    keeper, value = mine
    lines.append(f"Your chance of finishing last now: {base:.0%}. Your best keeper now: "
                 + (f"{keepers.label(keeper)} ({value:+.0f})" if keeper else "nobody beats the pick he'd cost") + ".")
    lines.append(f"Deals a contender should want, that leave you a better keeper and raise your chance of "
                 f"last by {MAX_RISE:.0%} at most:")
    if not deals:
        lines += ["", "None today."]
    for number, deal in enumerate(deals[:SHOWN], start=1):
        partner = names.get(deal["partner"], f"team {deal['partner']}")
        lines += ["", f"{number}. With {partner} ({deal['their_playoff_odds']:.0%} to make the playoffs)",
                  "   You give: " + ", ".join(trades.label(p) for p in deal["give"]),
                  "   You get:  " + ", ".join(trades.label(p) for p in deal["get"]),
                  f"   Keeper: {keepers.label(deal['keeper'])} ({deal['keeper_gain']:+.0f} over your best now)",
                  f"   You: {deal['you_regular']:+.1f} points to week {ctx['regular_end']}; chance of last "
                  f"{base:.0%} -> {deal['last_after']:.0%}",
                  f"   Them: {deal['them_playoffs']:+.1f} points in the playoffs, {deal['them_regular']:+.1f} before"]
        for who, key in (("You", "you"), ("They", "them")):
            part = deal["result"][key]
            if part["dropped"]:
                lines.append(f"   {who} would drop {', '.join(p['name'] for p in part['dropped'])}")
            if part["added"]:
                lines.append(f"   {who} would pick up {', '.join(p['name'] for p in part['added'])}")
        if deal["also"]:
            lines.append("   If not, they'd also gain from: " + "; ".join(
                f"{' + '.join(p['name'] for p in d['give'])} ({d['them_playoffs']:+.0f} for them, "
                f"{d['you_regular']:+.0f} for you)" for d in deal["also"]))
    lines += ["", "Keeper numbers are dynasty value less what the pick he'd cost tends to get (keepers.py). "
                  "Points are both lineups with the deal minus without (trades.py); your chance of last is "
                  "re-simulated with both (odds.py).",
              "Read-only: nothing was proposed or sent to ESPN. To check one in full: "
              "python3 trades.py --give \"...\" --get \"...\""]
    return "\n".join(lines)


def main(argv):
    espn = ESPN.from_env()
    ctx = trades.gather(espn)
    ctx["process_at"] = datetime.now() + timedelta(hours=ctx["review_hours"])
    values = keepers.load()
    draft = espn.league("mDraftDetail").get("draftDetail", {})
    kept, drafted = keepers.kept_rounds(draft), keepers.drafted_rounds(draft)
    for roster in list(ctx["rosters"].values()) + [ctx["pool"]]:
        keepers.attach(roster, values, kept, len(ctx["rosters"]), drafted)
    model, facts = odds.build(espn, ctx=ctx, sims=SIMS)
    if not facts["left"]:
        print("The regular season is over.")
        return 0
    if facts["deadline"] and datetime.now() > facts["deadline"]:
        print(f"The trade deadline passed {facts['deadline']:%a %b %d}.")
        return 0
    deals, base, mine = find(ctx, model, facts["left"])
    body = describe(deals, base, mine, ctx, facts["deadline"])
    print(body)
    if "--post" in argv:
        mailer.send(f"Trade ideas: {min(len(deals), SHOWN)} for a better keeper", body, blocks=slack.monospace(body))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except ESPNError as error:
        sys.exit(f"Error: {error}")
