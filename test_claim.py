"""Tests for placing a waiver claim (ESPN.claim) and the probe that tries it. Run: python3 test_claim.py
Nothing here reaches ESPN."""
import contextlib
import io
import sys
import tempfile
from pathlib import Path

import requests

import claim
import claim_probe
from espn import BENCH, ESPN, ESPNError, ESPNWriteRejected, claim_body


class Reply:
    def __init__(self, status_code, body):
        self.status_code, self._body, self.text = status_code, body, str(body)

    def json(self):
        return self._body


class Session:
    def __init__(self, reply):
        self.reply, self.sent = reply, []

    def post(self, url, json=None, timeout=None):
        self.sent.append((url, json))
        return self.reply


def espn_with(reply, owner=13):
    espn = ESPN(league_id=4242, espn_s2="s2", swid="{SWID}", season=2026)
    espn.session, espn.my_team_id = Session(reply), owner
    return espn


def test_a_claim_adds_to_your_team_and_drops_from_it_for_the_bid():
    assert claim_body(13, 12, 111, 222, 0) == {
        "isLeagueManager": False, "teamId": 13, "type": "WAIVER", "bidAmount": 0, "scoringPeriodId": 12,
        "executionType": "EXECUTE", "items": [{"playerId": 111, "type": "ADD", "toTeamId": 13},
                                              {"playerId": 222, "type": "DROP", "fromTeamId": 13}]}
    assert claim_body(13, 12, 111, bid=7)["items"] == [{"playerId": 111, "type": "ADD", "toTeamId": 13}], \
        "no drop with room on the roster"


def test_a_claim_goes_to_the_write_host_and_comes_back_pending():
    espn = espn_with(Reply(200, {"id": "abc", "status": "PENDING", "isPending": True}))
    assert espn.claim(13, 12, 111, 222, bid=4)["id"] == "abc"
    (url, body), = espn.session.sent
    assert url == "https://lm-api-writes.fantasy.espn.com/apis/v3/games/ffl/seasons/2026/segments/0/leagues/4242/transactions/"
    assert body == claim_body(13, 12, 111, 222, 4)


def test_a_claim_is_refused_for_any_team_but_yours_and_before_ownership_is_checked():
    for owner, team in ((13, 4), (None, 13)):
        espn = espn_with(Reply(200, {"status": "PENDING"}), owner=owner)
        try:
            espn.claim(team, 12, 111, 222)
            raise AssertionError("should have refused")
        except ESPNError:
            assert espn.session.sent == [], "refused before anything was sent"


def test_espns_refusal_and_an_unexpected_answer_both_raise():
    espn = espn_with(Reply(409, {"details": [{"type": "TRAN_PLAYER_NOT_ON_WAIVERS"}], "messages": ["no"]}))
    try:
        espn.claim(13, 12, 111, 222)
        raise AssertionError("should have raised")
    except ESPNWriteRejected as error:
        assert error.status == 409 and error.kind == "TRAN_PLAYER_NOT_ON_WAIVERS"
    espn = espn_with(Reply(200, {"id": "abc", "status": "EXECUTED"}))
    try:
        espn.claim(13, 12, 111, 222)
        raise AssertionError("should have raised")
    except ESPNError as error:
        assert "EXECUTED, not PENDING" in str(error) and "Check your roster" in str(error)


def test_the_probe_finds_a_claim_and_its_cancellation_anywhere_in_an_answer():
    answer = {"teams": [{"pendingTransactions": [{"id": "abc", "status": "PENDING"}]}],
              "transactions": [{"id": "zzz", "status": "CANCELED", "relatedTransactionId": "abc"},
                               {"id": "other", "status": "PENDING"}]}
    assert [hit["id"] for hit in claim_probe.find(answer, "abc")] == ["abc", "zzz"]


class FakeLeague:
    """Just enough of ESPN for --send: one player on waivers, one bench player, the claim pending."""
    season = 2026

    def __init__(self):
        self.claimed, self.my_team_id = [], None

    def league(self, *views, week=None, headers=None):
        if "kona_player_info" in views:
            entry = {"status": "WAIVERS", "waiverProcessDate": 1795000000000,
                     "player": {"id": 111, "fullName": "Nobody Wanted", "eligibleSlots": [4], "proTeamId": 5}}
            return {"players": [entry] if "111" in headers["x-fantasy-filter"] else []}
        if "mPendingTransactions" in views:
            return {"transactions": [{"id": "abc", "status": "PENDING"}]}
        if "mTransactions2" in views:
            return {"transactions": [{"id": "abc", "status": "PENDING", "bidAmount": 0}]}
        return {"scoringPeriodId": 12, "teams": [{"id": 13, "owners": ["{SWID}"]}],
                "settings": {"acquisitionSettings": {"acquisitionBudget": 100}}}

    def my_team(self, teams):
        self.my_team_id = 13
        return teams[0]

    def roster_entries(self, team_id, week):
        return [{"lineupSlotId": BENCH, "playerPoolEntry": {"lineupLocked": False, "player": {
            "id": 222, "fullName": "Bench Guy", "eligibleSlots": [2], "proTeamId": 3}}}]

    def claim(self, team_id, week, add_id, drop_id=None, bid=0):
        self.claimed.append((team_id, week, add_id, drop_id, bid))
        return {"id": "abc", "status": "PENDING"}


def probe(argv, league):
    saved = claim_probe.ESPN.from_env, claim_probe.LOG
    claim_probe.ESPN.from_env = lambda: league
    claim_probe.LOG = Path(tempfile.mkdtemp()) / "claim_probe.log"
    try:
        with contextlib.redirect_stdout(io.StringIO()) as out:
            code = claim_probe.main(argv)
        return code, out.getvalue(), claim_probe.LOG.read_text()
    finally:
        claim_probe.ESPN.from_env, claim_probe.LOG = saved


def test_the_probe_sends_only_a_zero_dollar_claim_on_a_waiver_player_for_a_bench_drop():
    league = FakeLeague()
    code, printed, logged = probe(["--send", "111", "222"], league)
    assert code == 0 and league.claimed == [(13, 12, 111, 222, 0)]
    assert "Pending: yes. Claim id abc" in printed and "--check abc" in printed
    assert printed == logged, "the log has everything printed, so it can be read back later"


def test_the_probe_refuses_a_player_not_on_waivers_or_a_drop_not_on_your_bench():
    for argv, why in ((["--send", "999", "222"], "isn't on waivers"), (["--send", "111", "999"], "isn't on your bench")):
        league = FakeLeague()
        code, printed, _ = probe(argv, league)
        assert code == 1 and why in printed and league.claimed == [], argv


def test_the_check_says_plainly_whether_the_claim_is_still_pending():
    code, printed, _ = probe(["--check", "abc"], FakeLeague())
    assert code == 1 and "STILL PENDING" in printed
    league = FakeLeague()
    league.league = lambda *views, week=None, headers=None: (
        {"transactions": [{"id": "zzz", "status": "CANCELED", "relatedTransactionId": "abc"}]}
        if "mTransactions2" in views else {"transactions": []} if "mPendingTransactions" in views
        else FakeLeague.league(league, *views, week=week, headers=headers))
    code, printed, _ = probe(["--check", "abc"], league)
    assert code == 0 and "Not in your pending claims any more: cancelled." in printed


class Wire:
    """ESPN as claim.py sees it: one player and his status, your roster, your pending claims, the claim."""
    season = 2026

    def __init__(self, status="WAIVERS", pending=(), locked=False, spent=45, answer=None):
        self.status, self.pending, self.locked, self.spent, self.answer = status, list(pending), locked, spent, answer
        self.claimed, self.my_team_id = [], None

    def league(self, *views, week=None, headers=None):
        if "kona_player_info" in views:
            entry = {"status": self.status, "player": {"id": 111, "fullName": "Nobody Wanted"}}
            return {"players": [entry] if self.status else []}
        if "mPendingTransactions" in views:
            return {"transactions": self.pending}
        return {"scoringPeriodId": 12, "teams": [{"id": 13, "transactionCounter": {"acquisitionBudgetSpent": self.spent}}],
                "settings": {"acquisitionSettings": {"acquisitionBudget": 100}}}

    def my_team(self, teams):
        self.my_team_id = 13
        return teams[0]

    def roster_entries(self, team_id, week):
        return [{"lineupSlotId": BENCH, "playerPoolEntry": {"lineupLocked": self.locked, "player": {
            "id": 222, "fullName": "Bench Guy", "eligibleSlots": [2], "proTeamId": 3}}}]

    def claim(self, team_id, week, add_id, drop_id=None, bid=0):
        self.claimed.append((team_id, week, add_id, drop_id, bid))
        if isinstance(self.answer, Exception):
            if isinstance(self.answer, requests.exceptions.RequestException):
                self.pending.append(_pending_claim(bid))   # it landed, though the answer never came
            raise self.answer
        self.pending.append(_pending_claim(bid))
        return {"id": "abc", "status": "PENDING"}


def _pending_claim(bid, player=111):
    return {"id": "abc", "teamId": 13, "type": "WAIVER", "status": "PENDING", "bidAmount": bid,
            "items": [{"type": "ADD", "playerId": player, "toTeamId": 13}, {"type": "DROP", "playerId": 222}]}


BID = {"player": 111, "name": "Nobody Wanted", "bid": 3, "drop": 222, "drop_name": "Bench Guy", "ts": "1.2"}


def bid(wire, live=True, args=BID, slack_works=True):
    """Run claim.main against the wire; returns (exit code, what was posted to Slack, what was printed)."""
    posted = []
    def post_first(text, layouts, thread_ts=None):
        if not slack_works:
            raise RuntimeError("slack is down")
        posted.append((text, thread_ts))
    saved = claim.ESPN.from_env, claim.slack.post_first, claim.slack.configured
    claim.ESPN.from_env, claim.slack.post_first, claim.slack.configured = (lambda: wire), post_first, (lambda: True)
    try:
        with contextlib.redirect_stdout(io.StringIO()) as out:
            code = claim.main(live, dict(args))
    finally:
        claim.ESPN.from_env, claim.slack.post_first, claim.slack.configured = saved
    return code, posted, out.getvalue()


def test_a_bid_from_slack_is_placed_and_said_so_in_the_reports_thread():
    wire = Wire()
    code, posted, _ = bid(wire)
    assert code == 0 and wire.claimed == [(13, 12, 111, 222, 3)]
    assert posted == [(":white_check_mark: Bid $3 on *Nobody Wanted*, dropping *Bench Guy* placed. It goes through at "
                       "the next waiver run unless someone outbids you; change or cancel it in ESPN until then.", "1.2")]


def test_a_bid_is_checked_again_before_it_is_sent():
    cases = [(Wire(status="FREEAGENT"), "he has cleared waivers"), (Wire(status=None), "no longer available"),
             (Wire(pending=[_pending_claim(5)]), "you already have a $5 claim on him"),
             (Wire(locked=True), "Bench Guy's game has started"), (Wire(spent=98), "$3 is more than the $2 of FAAB")]
    for wire, why in cases:
        code, posted, _ = bid(wire)
        assert code == 0 and wire.claimed == [] and why in posted[0][0] and posted[0][0].startswith(":warning:"), why
    wire = Wire()
    code, posted, _ = bid(wire, args=dict(BID, drop=999))
    assert wire.claimed == [] and "no longer on your roster" in posted[0][0]


def test_a_bid_with_room_on_the_roster_drops_nobody():
    wire = Wire()
    code, posted, _ = bid(wire, args=dict(BID, drop="none"))
    assert wire.claimed == [(13, 12, 111, None, 3)] and "dropping" not in posted[0][0]


def test_a_shadow_run_checks_and_sends_nothing():
    wire = Wire()
    code, posted, printed = bid(wire, live=False)
    assert code == 0 and wire.claimed == [] and posted == []
    assert "(shadow run: would have bid $3 on *Nobody Wanted*, dropping *Bench Guy*)" in printed


def test_espns_refusal_is_said_in_the_thread():
    wire = Wire(answer=ESPNWriteRejected(409, "TRAN_INSUFFICIENT_FUNDS", "not enough"))
    code, posted, _ = bid(wire)
    assert code == 0 and posted[0][0].startswith(":warning: ESPN refused the bid $3 on *Nobody Wanted*")
    assert "TRAN_INSUFFICIENT_FUNDS" in posted[0][0]


def test_a_lost_answer_is_settled_by_reading_the_pending_claims():
    wire = Wire(answer=requests.exceptions.ReadTimeout("read timed out"))
    code, posted, printed = bid(wire)
    assert code == 0 and len(wire.claimed) == 1, "never sent twice"
    assert "connection problem while writing" in printed and posted[0][0].startswith(":white_check_mark:")


def test_when_slack_cannot_be_told_the_run_fails_so_github_says_so():
    code, _, printed = bid(Wire(), slack_works=False)
    assert code == 1 and "(nothing delivered:" in printed, "hosted_run.sh fails a run that prints this"


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
