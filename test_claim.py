"""Tests for placing a waiver claim (ESPN.claim) and the probe that tries it. Run: python3 test_claim.py
Nothing here reaches ESPN."""
import contextlib
import io
import sys
import tempfile
from pathlib import Path

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
