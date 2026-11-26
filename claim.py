"""Places the waiver claim you submit from a Bid button in Slack, and says how it went in the report's thread.

The Cloudflare Worker (slack-worker/worker.js) starts this on GitHub with what you submitted, as JSON in
CLAIM_ARGS: {"player", "name", "bid", "drop", "drop_name", "ts"}, ts being the report's message. Hours
may have passed since the report, so it checks again on ESPN first: he is still on waivers (one who has
cleared would be added at once, and the Add button comes later), you have no claim on him already, the
drop is still yours with his game still to come, and the bid is within the FAAB you have left.

    python3 claim.py           checks and says what it would send, sends nothing
    python3 claim.py --live    places the claim (hosted_run.sh adds --live when BOT_LIVE is true)

ESPN.claim's request was confirmed in this league by claim_probe.py (Nov 26, 2026).
"""
import json
import os
import sys

import requests

import slack
from espn import ESPN, ESPNError, ESPNWriteRejected, parse_roster_entry


def availability(espn, week, player_id):
    """"WAIVERS", "FREEAGENT", or None once he is on a roster."""
    rules = {"players": {"filterStatus": {"value": ["FREEAGENT", "WAIVERS"]}, "filterIds": {"value": [player_id]}}}
    data = espn.league("kona_player_info", week=week, headers={"x-fantasy-filter": json.dumps(rules)})
    return next((entry.get("status") for entry in data.get("players", [])
                 if (entry.get("player") or {}).get("id") == player_id), None)


def _transactions(data):
    """Every transaction anywhere in an ESPN answer."""
    if isinstance(data, dict):
        if "items" in data and "status" in data:
            yield data
        for value in data.values():
            yield from _transactions(value)
    elif isinstance(data, list):
        for value in data:
            yield from _transactions(value)


def pending_bid(espn, team_id, player_id):
    """Your pending claim on him, if you have one. A claim cancelled in ESPN leaves this view, though
    it still reads PENDING in mTransactions2 (claim_probe.py, Nov 26)."""
    return next((t for t in _transactions(espn.league("mPendingTransactions"))
                 if t.get("teamId") == team_id and t.get("type") == "WAIVER" and t.get("status") == "PENDING"
                 and any(i.get("type") == "ADD" and i.get("playerId") == player_id for i in t["items"])), None)


def faab_left(league, team):
    acquisition = league["settings"]["acquisitionSettings"]
    return acquisition.get("acquisitionBudget", 0) - team.get("transactionCounter", {}).get("acquisitionBudgetSpent", 0)


def problem_with(espn, league, team, week, player, bid, drop):
    """Why the claim shouldn't be sent, or None. ESPN checks the budget and roster again itself."""
    status = availability(espn, week, player)
    if status is None:
        return "he's no longer available."
    if status != "WAIVERS":
        return "he has cleared waivers, so a claim would add him at once. Add him in ESPN (the Add button comes later)."
    already = pending_bid(espn, team["id"], player)
    if already:
        return f"you already have a ${already.get('bidAmount', '?')} claim on him. Change or cancel it in ESPN."
    if drop is not None:
        roster = {p["id"]: p for p in (parse_roster_entry(e, week, espn.season) for e in espn.roster_entries(team["id"], week))}
        if drop not in roster:
            return "the player to drop is no longer on your roster."
        if roster[drop]["locked"]:
            return f"{roster[drop]['name']}'s game has started, so he can't be dropped until next week."
    left = faab_left(league, team)
    if not 0 <= bid <= left:
        return f"${bid} is more than the ${left} of FAAB you have left."
    return None


def report(text, ts):
    """Print it, and post it in the report's thread unless this is a shadow run. True if it reached you."""
    print(text)
    if os.getenv("MAIL_TO_LOG") or not slack.configured():
        return True
    try:
        slack.post_first(text, [[slack.section(text)]], thread_ts=ts or None)
        return True
    except Exception as problem:  # broad on purpose: the claim, if placed, is already in ESPN
        print(f"(Slack post failed: {type(problem).__name__}: {problem})")
        print(f'(nothing delivered: "{text[:80]}" reached neither Slack nor email)')
        return False


def main(live, args):
    player, bid = int(args["player"]), int(args["bid"])
    drop = int(args["drop"]) if args.get("drop") not in (None, "", "none") else None
    esc = slack.escape
    what = f"${bid} on *{esc(args.get('name') or str(player))}*" + (
        f", dropping *{esc(args.get('drop_name') or str(drop))}*" if drop is not None else "")
    ts = args.get("ts")
    espn = ESPN.from_env()
    league = espn.league("mSettings", "mTeam")
    week = league["scoringPeriodId"]
    team = espn.my_team(league["teams"])

    problem = problem_with(espn, league, team, week, player, bid, drop)
    if problem:
        return 0 if report(f":warning: Bid {what} not placed: {problem}", ts) else 1
    if not live:
        print(f"(shadow run: would have bid {what})")
        return 0
    try:
        espn.claim(team["id"], week, player, drop, bid)
    except ESPNWriteRejected as rejection:
        return 0 if report(f":warning: ESPN refused the bid {what}: {rejection}", ts) else 1
    except requests.exceptions.RequestException as error:
        print(f"(connection problem while writing: {type(error).__name__}; reading your pending claims)")
    if pending_bid(espn, team["id"], player):
        text = (f":white_check_mark: Bid {what} placed. It goes through at the next waiver run unless someone "
                "outbids you; change or cancel it in ESPN until then.")
    else:
        text = f":warning: Bid {what}: it isn't in your pending claims. Check ESPN before trying again."
    return 0 if report(text, ts) else 1


def _args():
    try:
        return json.loads(os.environ.get("CLAIM_ARGS") or "{}")
    except ValueError:
        return {}


if __name__ == "__main__":
    submitted = _args()
    try:
        sys.exit(main("--live" in sys.argv, submitted))
    except (ESPNError, KeyError, ValueError) as error:   # unexpected: said in Slack, and the run fails too
        report(f":rotating_light: Bid not placed: {slack.escape(f'{type(error).__name__}: {error}')}", submitted.get("ts"))
        sys.exit(1)
