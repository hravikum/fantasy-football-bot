"""Tests for the trade analyzer. Run: python3 test_trades.py"""
import contextlib
import io
import json
import sys
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path

import trades
from espn import BENCH
from test_lineup import GAMES, SLOTS, SUNDAY, TIMED, full_roster, player
from test_waivers import free_agent, roster_at_capacity

WEEK = 5
# The proposal the probe found in your league, as ESPN sent it, minus the account id.
PROPOSAL = {"id": "8eeaf424-9696-483d-a8e1-2fe05e7bb451", "type": "TRADE_PROPOSAL", "status": "PENDING",
            "isPending": True, "teamId": 3, "scoringPeriodId": 5, "proposedDate": 1791342982981,
            "expirationDate": 1791515783011, "teamActions": {"3": "ACCEPTED"},
            "items": [{"type": "TRADE", "playerId": 4871023, "fromTeamId": 3, "toTeamId": 13},
                      {"type": "TRADE", "playerId": 4035538, "fromTeamId": 3, "toTeamId": 13},
                      {"type": "TRADE", "playerId": 4882093, "fromTeamId": 13, "toTeamId": 3}]}
BEFORE_IT_EXPIRES = 1791400000000


def context(mine, theirs, pool=(), games=GAMES, byes=None, process_at=None, return_weeks=None):
    """A league of two: you are team 1, they are team 2. Regular season to week 14, playoffs 15-17."""
    return {"week": WEEK, "me": 1, "names": {1: "You", 2: "Them"},
            "rosters": {1: list(mine.values()), 2: list(theirs.values())},
            "slot_counts": SLOTS, "games": games, "byes": byes or {}, "pool": list(pool),
            "regular_end": 14, "last_week": 17, "review_hours": 0,
            "process_at": process_at or SUNDAY, "return_weeks": return_weeks or {}}


def run(mine, theirs, give, get, **options):
    ctx = context(mine, theirs, **options)
    return trades.analyze(ctx, 2, {p["id"] for p in give}, {p["id"] for p in get}), ctx


def gains(result, key):
    return [round(after - before, 1) for _, before, after in result[key]["weeks"]]


def on_team(pro_team, *args, **kwargs):
    person = player(*args, **kwargs)
    person["pro_team"] = pro_team
    return person


def test_a_starter_for_a_bench_piece_is_worth_what_he_adds_to_each_lineup():
    mine, theirs = full_roster(), full_roster(wr1=player("WR", 4, 14.0))
    result, _ = run(mine, theirs, [mine["bench_rb"]], [theirs["wr1"]])
    assert gains(result, "you") == [4.0] * 13, "he replaces your 10-point flex; the back you send never started"
    assert gains(result, "them") == [-11.0] * 13, "they lose a 14-point receiver and start a 3-point one"


def test_the_windows_split_the_regular_season_from_the_playoffs():
    mine, theirs = full_roster(), full_roster(wr1=player("WR", 4, 14.0))
    result, ctx = run(mine, theirs, [mine["bench_rb"]], [theirs["wr1"]])
    w = trades.windows(result["you"]["weeks"], ctx)
    assert (w["this_week"], w["next"], w["regular"], w["playoffs"]) == (4.0, 20.0, 40.0, 12.0)


def test_taking_two_for_one_costs_your_cheapest_bench_player():
    mine = roster_at_capacity()                          # sixteen, so a second arrival forces a drop
    theirs = full_roster(rb1=player("RB", 2, 16.0), wr1=player("WR", 4, 9.0))
    result, _ = run(mine, theirs, [mine["wr1"]], [theirs["rb1"], theirs["wr1"]])
    dropped = result["you"]["dropped"]
    assert len(dropped) == 1 and dropped[0]["season_projection"] == 17.0, "a one-point filler goes"
    assert gains(result, "you")[0] == 3.0


def test_an_open_spot_is_worth_the_best_free_agent_from_next_week():
    mine = roster_at_capacity()
    theirs = full_roster(wr1=player("WR", 4, 14.0))
    help_wanted = free_agent("RB", 13.0, season=221.0)
    result, _ = run(mine, theirs, [mine["bench_wr"], mine["bench_rb"]], [theirs["wr1"]], pool=[help_wanted])
    assert [p["id"] for p in result["you"]["added"]] == [help_wanted["id"]]
    assert gains(result, "you")[:2] == [4.0, 6.0], "he can't play until waivers run, then he starts at flex"


def test_a_pickup_you_could_make_anyway_is_not_credited_to_the_trade():
    """Twelve players means room already: the free agent is yours with or without the trade."""
    mine, theirs = full_roster(), full_roster(wr1=player("WR", 4, 14.0))
    help_wanted = free_agent("RB", 13.0, season=221.0)
    result, _ = run(mine, theirs, [mine["bench_rb"]], [theirs["wr1"]], pool=[help_wanted])
    assert result["you"]["added"] == [], "not a pickup the trade made possible"
    assert gains(result, "you")[:2] == [4.0, 3.0], "next week he's in both lineups, so only the trade counts"


def test_a_player_whose_game_already_started_plays_it_for_his_old_team():
    mine = full_roster()
    for receiver, this_week in ((player("WR", 4, 14.0), 0.0),            # pro team 1 kicks off at 1:00
                                (on_team(2, "WR", 4, 14.0), 4.0)):       # pro team 2 at 4:25
        theirs = full_roster(wr1=receiver)
        result, _ = run(mine, theirs, [mine["bench_rb"]], [receiver], games=TIMED,
                        process_at=SUNDAY.replace(hour=14))
        assert gains(result, "you")[:2] == [this_week, 4.0], "a trade at 2:00 catches only the late game"


def test_a_locked_player_never_changes_teams_this_week():
    assert not trades.moves_this_week(player("WR", 4, 14.0, locked=True), GAMES, SUNDAY)


def test_an_injured_player_counts_from_the_week_he_is_due_back():
    mine = full_roster()
    hurt = player("RB", 2, 0.0, status="OUT", season=16.0 * 17)
    theirs = full_roster(rb1=hurt)
    result, _ = run(mine, theirs, [mine["bench_rb"]], [hurt])
    assert gains(result, "you")[:3] == [0.0, 6.0, 6.0], "OUT means back next week unless the news says otherwise"
    result, _ = run(mine, theirs, [mine["bench_rb"]], [hurt], return_weeks={hurt["id"]: WEEK + 3})
    assert gains(result, "you")[:4] == [0.0, 0.0, 0.0, 6.0]


def test_injured_reserve_defaults_to_the_four_game_minimum():
    mine = full_roster()
    hurt = player("RB", 2, 0.0, status="INJURY_RESERVE", season=16.0 * 17)
    theirs = full_roster(rb1=hurt)
    result, _ = run(mine, theirs, [mine["bench_rb"]], [hurt])
    assert gains(result, "you")[:6] == [0.0, 0.0, 0.0, 0.0, 6.0, 6.0]


def test_your_own_injured_starter_comes_back_in_both_versions():
    """Otherwise every running back offered while yours is hurt looks like a season-long upgrade."""
    mine = full_roster(rb1=player("RB", 2, 0.0, status="OUT", season=18.0 * 17))
    theirs = full_roster(rb1=player("RB", 2, 12.5))
    result, _ = run(mine, theirs, [mine["bench_wr"]], [theirs["rb1"]])
    assert gains(result, "you")[:3] == [9.5, 2.5, 2.5], "a big upgrade only while your back is out"


def test_his_bye_week_costs_only_that_week():
    mine = full_roster()
    receiver = on_team(3, "WR", 4, 14.0)
    theirs = full_roster(wr1=receiver)
    result, _ = run(mine, theirs, [mine["bench_rb"]], [receiver], byes={3: {7}})
    assert gains(result, "you")[:4] == [4.0, 4.0, 0.0, 4.0]


def test_this_weeks_locks_do_not_carry_into_later_weeks():
    """A receiver locked on your bench today can still start next week."""
    mine = full_roster(bench_wr=player("WR", BENCH, 18.0, locked=True))
    theirs = full_roster()
    result, _ = run(mine, theirs, [mine["bench_rb"]], [theirs["bench_rb"]])
    assert result["you"]["weeks"][1][1] == 127.0, "with him starting next week, the lineup projects 127"


def test_players_are_found_by_name_and_mistakes_are_refused():
    mine, theirs, third = full_roster(), full_roster(), full_roster()
    mine["qb"]["name"], theirs["wr1"]["name"], third["wr1"]["name"] = "Jalen Hurts", "Amon-Ra St. Brown", "Ja'Marr Chase"
    rosters = {1: list(mine.values()), 2: list(theirs.values()), 3: list(third.values())}
    give, get, partner = trades.pick_players(["hurts"], ["st brown"], rosters, 1)
    assert give == {mine["qb"]["id"]} and get == {theirs["wr1"]["id"]} and partner == 2
    give, _, _ = trades.pick_players([mine["wr1"]["name"]], ["st brown"], rosters, 1)
    assert give == {mine["wr1"]["id"]}, "digits count: WR12 is not WR13"
    for give_names, get_names, why in ((["hurts"], ["st brown", "jamarr chase"], "players on two teams"),
                                       (["nobody"], ["st brown"], "no such player"),
                                       (["WR"], ["st brown"], "a name that matches several")):
        try:
            trades.pick_players(give_names, get_names, rosters, 1)
            raise AssertionError(f"should refuse {why}")
        except ValueError:
            pass


def test_the_report_shows_both_sides_and_never_reads_as_if_it_traded():
    mine, theirs = full_roster(), full_roster(wr1=player("WR", 4, 14.0))
    result, ctx = run(mine, theirs, [mine["bench_rb"]], [theirs["wr1"]])
    text = trades.describe(result, ctx)
    assert "You give" in text and "You get" in text and "Them" in text
    assert "+4.0" in text and "-11.0" in text
    assert "nothing was proposed or sent to ESPN" in text


def test_a_proposal_reads_the_same_from_either_side():
    assert trades.deal_from(PROPOSAL, 13) == ({4882093}, {4871023, 4035538}, 3)
    assert trades.deal_from(PROPOSAL, 3) == ({4871023, 4035538}, {4882093}, 13)


def test_only_your_open_proposals_are_picked_up_and_each_only_once():
    yours = dict(PROPOSAL, id="yours", teamId=13)
    strangers = dict(PROPOSAL, id="strangers", teamId=5,
                     items=[dict(item, fromTeamId=5, toTeamId=6) for item in PROPOSAL["items"]])
    answered = dict(PROPOSAL, id="answered", status="EXECUTED")
    expired = dict(PROPOSAL, id="expired", expirationDate=BEFORE_IT_EXPIRES - 1)
    found = trades.new_proposals([PROPOSAL, yours, strangers, answered, expired], 13, {}, BEFORE_IT_EXPIRES)
    assert [p["id"] for p in found] == [PROPOSAL["id"], "yours"], "sent to you and sent by you, nothing else"
    seen = {PROPOSAL["id"]: "2026-10-07T07:00"}
    assert trades.new_proposals([PROPOSAL], 13, seen, BEFORE_IT_EXPIRES) == []


def test_the_offer_line_says_who_offered_it_and_when_it_expires():
    expires = trades._when(PROPOSAL["expirationDate"])
    incoming = trades.offer_line(PROPOSAL, 13, "Please Respond to Jeff")
    assert incoming.startswith("Please Respond to Jeff offered this") and f"expires {expires}" in incoming
    assert "Accept or decline it in ESPN" in incoming, "the bot never answers for you"
    outgoing = trades.offer_line(dict(PROPOSAL, teamId=13), 13, "Please Respond to Jeff")
    assert outgoing.startswith("You offered this to Please Respond to Jeff") and "if they don't answer" in outgoing


def test_a_failure_is_emailed_once_a_day_and_logged_every_time():
    sent = []
    with tempfile.TemporaryDirectory() as folder, contextlib.redirect_stdout(io.StringIO()) as log:
        path = Path(folder) / "failed_on.txt"
        send = lambda subject, body: sent.append(subject) or True
        assert trades.report_failure("401", path, date(2026, 10, 8), send)
        assert not trades.report_failure("401", path, date(2026, 10, 8), send)
        assert trades.report_failure("401", path, date(2026, 10, 9), send)
    assert len(sent) == 2 and log.getvalue().count("Trade watch failed") == 3


def test_the_report_puts_each_players_pace_beside_the_season_figure():
    mine, theirs = full_roster(), full_roster(wr1=player("WR", 4, 14.0, season=5.0 * 17))
    theirs["wr1"].update(scored=60.0, games_played=4, level=11.0)
    result, ctx = run(mine, theirs, [mine["bench_rb"]], [theirs["wr1"]])
    row = next(line for line in trades.describe(result, ctx).splitlines() if line.strip().startswith(theirs["wr1"]["name"]))
    assert row.split()[-5:] == ["14.0", "5.0", "11.0", "15.0", "(4)"], "this week, ESPN's season per game, level, actual"


class FakeESPN:
    def __init__(self, proposal):
        self.proposal = proposal

    def league(self, *views, week=None, headers=None):
        if "mTransactions2" in views:
            return {"transactions": [self.proposal]}
        return {"scoringPeriodId": WEEK, "settings": {"tradeSettings": {}}, "teams": []}

    def my_team(self, teams):
        return {"id": 1}


def test_the_watch_emails_a_new_offer_once_and_retries_a_failed_email():
    mine, theirs = full_roster(), full_roster(wr1=player("WR", 4, 14.0))
    now = datetime(2026, 10, 7, 12)
    proposal = {"id": "offer-1", "type": "TRADE_PROPOSAL", "status": "PENDING", "teamId": 2,
                "proposedDate": (now - timedelta(hours=3)).timestamp() * 1000,
                "expirationDate": (now + timedelta(hours=45)).timestamp() * 1000,
                "items": [{"type": "TRADE", "playerId": theirs["wr1"]["id"], "fromTeamId": 2, "toTeamId": 1},
                          {"type": "TRADE", "playerId": mine["bench_rb"]["id"], "fromTeamId": 1, "toTeamId": 2}]}
    emails, delivered = [], [False]
    real_gather, real_send = trades.gather, trades.mailer.send
    trades.gather = lambda espn: context(mine, theirs)
    trades.mailer.send = lambda subject, body, **slack_parts: emails.append((subject, body, slack_parts)) or delivered[0]
    try:
        with tempfile.TemporaryDirectory() as folder, contextlib.redirect_stdout(io.StringIO()):
            seen = Path(folder) / "seen.json"
            trades.watch(FakeESPN(proposal), seen, now)
            assert len(emails) == 1 and not seen.exists(), "an email that didn't go out is tried again"
            delivered[0] = True
            trades.watch(FakeESPN(proposal), seen, now)
            trades.watch(FakeESPN(proposal), seen, now)
            assert len(emails) == 2 and "offer-1" in json.loads(seen.read_text()), "then never again"
    finally:
        trades.gather, trades.mailer.send = real_gather, real_send
    subject, body, slack_parts = emails[-1]
    assert subject == "Trade offer from Them: you +52, them -143 pts rest of season"
    assert body.startswith("Them offered this") and "You get:  WR" in body
    assert slack_parts["blocks"][0]["text"]["text"] == subject
    (analysis,) = slack_parts["thread"]
    assert analysis[0]["text"]["text"].startswith("```\nTrade check, week"), "the full analysis goes in the thread"


def test_slack_puts_the_two_sides_next_to_each_other_with_both_teams_totals():
    mine, theirs = full_roster(), full_roster(wr1=player("WR", 4, 14.0))
    result, ctx = run(mine, theirs, [mine["bench_rb"]], [theirs["wr1"]])
    ctx["process_at"] = datetime(2026, 10, 9, 3)
    proposal = dict(PROPOSAL, teamId=2, proposedDate=BEFORE_IT_EXPIRES - 86400000, expirationDate=BEFORE_IT_EXPIRES)
    blocks = trades.slack_layout("Trade offer from Them & co", proposal, result, ctx, 1, "Them & co")
    assert blocks[0]["text"]["text"] == "Trade offer from Them & co", "a header is plain text: nothing escaped"
    assert blocks[1]["text"]["text"].startswith("Them &amp; co offered this")
    give, get = (field["text"] for field in blocks[2]["fields"])
    assert give == "*You give*\n" + trades.label(mine["bench_rb"]) and get == "*You get*\n" + trades.label(theirs["wr1"])
    totals = blocks[3]["text"]["text"].split("\n")
    assert totals[0] == "*With the trade, minus without*" and totals[1].startswith("You: ")
    assert totals[2].startswith("Them &amp; co: ") and "in the playoffs" in totals[2]
    assert "Read-only: nothing was proposed or sent to ESPN." in blocks[4]["elements"][0]["text"]


if __name__ == "__main__":
    tests = [(name, fn) for name, fn in sorted(vars().items()) if name.startswith("test_")]
    failures = 0
    for name, fn in tests:
        try:
            fn()
            print(f"pass  {name}")
        except AssertionError as e:
            failures += 1
            print(f"FAIL  {name}: {e}")
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    sys.exit(1 if failures else 0)
