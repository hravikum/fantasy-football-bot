"""See the Slack layouts without waiting for the day each one comes up.

Builds the waiver report, a lineup update, a trade offer and a failure notice from sample data (the
waiver report is Tuesday Nov 24's), checks each against Slack's limits, and with --post posts them to
the channel in .env, each headed "Preview", with the thread replies under the waiver report and the
trade offer. Nothing goes to ESPN or by email.

    python3 slack_preview.py           check the layouts, post nothing
    python3 slack_preview.py --post    post them
    python3 slack_preview.py --bid-card PLAYER
                                       post one real card, with a Bid button, for a player on waivers (an
                                       id from claim_probe.py): a $0 test of the Bid button from end to end
"""
import sys
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

import bids
import claim
import claim_probe
import lineup
import slack
import trades
import waivers
from espn import BENCH, ESPN, parse_roster_entry
from test_lineup import GAMES, SLOTS, full_roster, player
from test_trades import BEFORE_IT_EXPIRES, PROPOSAL, run

CLE, NYG, NYJ, WSH, LAC = 5, 19, 20, 28, 24


def fa(name, position, team, owned, change, wk, level, gain, ahead, season, bid, call=None, drop=None, weeks=1):
    return {"id": hash(name) % 10**6, "name": name, "position": position, "pro_team": team, "percent_owned": owned,
            "percent_change": change, "snaps": {"by_week": [100, None]} if position == "QB" else None,
            "projection": wk, "level": level, "gain": gain, "ahead_gain": ahead, "season_gain": season,
            "weeks_helped": weeks, "bid": bid, "free_now": False, "drop": {"name": drop} if drop else None,
            "call": call, "eligible_slots": [], "injury_status": "ACTIVE"}


def waiver_report():
    advice = (
        "Watson is locked in as Cleveland's starter this week — he's confirmed for Week 12 vs. the Raiders after "
        "clearing concussion protocol, and the Raiders have allowed 12 passing touchdowns in six games. His 100% "
        "snap share and the 5.7-point gain this week make him worth a mid-range bid even though it's one week "
        "only. Bid $4, drop Emari Demercado.\n\n"
        "McCarthy is also a confirmed starter, but his gain this week (2.4) trails Watson's and he adds nothing "
        "after. One streaming QB is enough — skip.\n\n"
        "Giants D/ST and Commanders D/ST don't fix the real week 13 hole (Eagles or Lions, claimable on Tuesday) — "
        "skip both.\n\nSources:\n- Week 12 fantasy football QB start-sit: Darnold, Mahomes, Watson, Williams\n"
        "  https://www.example.com/week-12-qb-start-sit\n- Giants' J.J. McCarthy update - Yahoo Sports\n"
        "  https://sports.yahoo.com/example")
    candidates = [
        fa("Giants D/ST", "D/ST", NYG, 16, -0.1, 7.0, None, 0.5, 5.8, 2, (1, 4),
           {"call": "skip", "bid": None, "drop": None, "why": "Doesn't fix the week 13 hole"}, weeks=3),
        fa("Commanders D/ST", "D/ST", WSH, 16, 0.1, 8.9, None, 2.5, 2.7, 0, (1, 4),
           {"call": "skip", "bid": None, "drop": None, "why": "Jets D/ST is as good"}, weeks=2),
        fa("Deshaun Watson", "QB", CLE, 11, 0.0, 19.9, 13.2, 5.7, 5.7, 0, (3, 5),
           {"call": "bid", "bid": 4, "drop": "Emari Demercado", "why": "Confirmed starter after clearing concussion protocol"}),
        fa("J.J. McCarthy", "QB", NYG, 25, -0.1, 16.5, 9.9, 2.4, 2.4, 0, (3, 5),
           {"call": "skip", "bid": None, "drop": None, "why": "One streaming QB is enough"}),
    ]
    context = {"week": 12, "budget": 100, "budget_left": 55, "minimum_bid": 0, "rivals": [100, 95, 83, 60, 41],
               "weeks_left": 6, "last_runs": [datetime(2026, 11, 18, 3, 33), datetime(2026, 11, 19, 3, 42),
                                             datetime(2026, 11, 20, 3, 24)],
               "holes_now": [], "gaps": ["week 13: D/ST short by 1 — available and playing that week: Eagles D/ST "
                                         "(PHI), Lions D/ST (DET)",
                                         "week 13: K short by 1 — available and playing that week: Jason Myers "
                                         "(SEA), Cairo Santos (CHI)"],
               "candidates": candidates, "advice": advice,
               "past_claims": {bids.IN_DEMAND: [8] * 12 + [18] * 6 + [31] * 5, bids.OTHER: [2] * 40 + [5] * 20 + [12] * 14,
                               bids.STREAMED: [1] * 33 + [4] * 20 + [7] * 13}}
    subject = f"Waivers week 12: {waivers.headline(candidates)}"
    report = waivers.build_report(context)
    blocks, thread = waivers.slack_layout(context, subject, report)
    return subject, blocks, thread


def lineup_message():
    roster = full_roster(bench_wr=player("WR", BENCH, 18.0, status="QUESTIONABLE"))
    names = {"qb": "Bryce Young", "rb1": "Omarion Hampton", "rb2": "Kyle Monangai", "wr1": "Marvin Harrison Jr.",
             "wr2": "Treylon Burks", "te": "Cade Otton", "flex1": "Emari Demercado", "flex2": "Rashid Shaheed",
             "dst": "Jets D/ST", "k": "Jason Myers", "bench_wr": "KC Concepcion", "bench_rb": "Keaton Mitchell"}
    for key, name in names.items():
        roster[key]["name"] = name
    players = list(roster.values())
    target, moves, gain, broken, blocked = lineup.decide(players, SLOTS, GAMES)
    subject = "Week 12: lineup updated"
    return subject, lineup.slack_layout(subject, players, target, moves, gain, broken, blocked, GAMES,
                                        [f"Applied and confirmed on ESPN: {len(moves)} move(s)."]), None


def trade_offer():
    mine, theirs = full_roster(), full_roster(wr1=player("WR", 4, 14.0))
    mine["bench_rb"].update(name="Keaton Mitchell", pro_team=LAC)
    theirs["wr1"].update(name="Garrett Wilson", pro_team=NYJ)
    result, ctx = run(mine, theirs, [mine["bench_rb"]], [theirs["wr1"]])
    ctx["process_at"] = datetime(2026, 11, 27, 3)
    proposal = dict(PROPOSAL, teamId=2, proposedDate=BEFORE_IT_EXPIRES - 86400000, expirationDate=BEFORE_IT_EXPIRES)
    title = trades.subject(proposal, result, 1, "Crack Baby Rival")
    return (title, trades.slack_layout(title, proposal, result, ctx, 1, "Crack Baby Rival"),
            [slack.monospace(trades.describe(result, ctx))])


def failure():
    subject = "lineup run failed"
    return subject, slack.alert(subject, "Error: ESPN rejected the cookies (401). Re-copy ESPN_S2 and SWID "
                                         "from your browser."), None


def check(blocks):
    texts = []
    for b in blocks:
        if b["type"] in ("section", "header"):
            texts += [b["text"]["text"]] if "text" in b else [f["text"] for f in b["fields"]]
        elif b["type"] == "context":
            texts += [e["text"] for e in b["elements"]]
    assert len(blocks) <= 50 and all(len(t) <= 3000 for t in texts), "over Slack's limits"
    return f"{len(blocks)} blocks, longest text {max(map(len, texts), default=0)}"


def bid_card(player_id):
    """One real card with a Bid button, built the way the waiver report builds it, for a player on
    waivers and your real roster. Tap Bid, submit $0, and cancel the claim in ESPN afterwards."""
    espn = ESPN.from_env()
    league = espn.league("mSettings", "mTeam")
    week = league["scoringPeriodId"]
    team = espn.my_team(league["teams"])
    found = claim_probe.on_waivers(espn, week, ids=[player_id])
    if not found:
        sys.exit(f"Player {player_id} isn't on waivers: pick one from python3 claim_probe.py")
    bench = claim_probe.your_bench(espn, team["id"], week)
    candidate = dict(found[0], bid=(0, 0), gain=0.0, ahead_gain=0.0, season_gain=0, weeks_helped=1, free_now=False,
                     drop=bench[0] if bench else None,
                     call={"call": "bid", "bid": 0, "drop": None, "why": "A test: cancel it in ESPN afterwards"})
    context = {"roster": [parse_roster_entry(e, week, espn.season) for e in espn.roster_entries(team["id"], week)],
               "budget_left": claim.faab_left(league, team), "week": week}
    card = slack.section(waivers._card(candidate))
    card["accessory"] = slack.button("Bid", "bid", waivers.bid_button(candidate, context))
    blocks = [slack.header("Preview: a test bid"),
              slack.context("Tap Bid, submit $0, and watch for the answer in this message's thread. Then cancel "
                            "the claim in ESPN, and run python3 claim_probe.py --check with its id if you like."),
              card]
    slack.post_first("Preview: a test bid", [blocks])
    print(f"posted a card for {found[0]['name']}, suggesting {bench[0]['name'] if bench else 'no drop'} to drop")


if __name__ == "__main__":
    load_dotenv(Path(__file__).with_name(".env"))
    if ("--post" in sys.argv or "--bid-card" in sys.argv) and not slack.configured():
        sys.exit("Set SLACK_BOT_TOKEN and SLACK_CHANNEL in .env first.")
    if "--bid-card" in sys.argv:
        bid_card(int(sys.argv[sys.argv.index("--bid-card") + 1]))
        sys.exit(0)
    for build in (waiver_report, lineup_message, trade_offer, failure):
        subject, blocks, thread = build()
        print(f"{build.__name__}: {check(blocks)}" + (f"; thread: {', '.join(check(t) for t in thread)}" if thread else ""))
        if "--post" in sys.argv:
            if build is not failure:
                blocks = [slack.header("Preview: " + subject)] + blocks[1:]
            else:
                blocks = [slack.section(blocks[0]["text"]["text"].replace("*lineup", "*Preview: lineup"))] + blocks[1:]
            ts = slack.post_first("Preview: " + subject, [blocks])
            if thread:
                slack.post_first("Preview: " + subject, thread, thread_ts=ts)
            print("  posted")
