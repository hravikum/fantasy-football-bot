"""Tests for the Jev scorer. Run: python3 test_jev.py

Uses the SDK's own client against a mock transport, so the request and response shapes
are the real ones rather than a stand-in for them.
"""
import json
import sys

import jev

SKIP = None
try:
    import httpx2
    from typesafe_sdk import TypeSafeClient
except ImportError as missing:
    SKIP = f"typesafe-sdk not installed ({missing})"

LEAGUE = {"teams": 14, "points_per_reception": 0.5, "keepers_next_season": 1, "my_faab_left": 100}
ROSTER = {"starters": ["Someone (WR, season projection 180)"], "bench": ["Other (RB, season projection 90)"]}


def player(name="Test Guy", position="WR", **overrides):
    person = {"name": name, "position": position, "pro_team": 12, "injury_status": "ACTIVE",
              "percent_owned": 22.0, "percent_change": 4.0, "projection": 9.1,
              "season_projection": 140.0, "gain": 0.0, "season_gain": 0, "math_rank": 1}
    person.update(overrides)
    return person


def answers(role=4.0, ros=4.0, growing=1.0, keeper=1.0, confidence=1.0):
    """A response body shaped the way the API returns one."""
    levels = {str(i): 0.2 for i in range(5)}
    return {"model": "jev-1.13.0", "usage": {"input_tokens": 100, "output_tokens": 0}, "answers": {
        "role": {"type": "score", "score": role, "confidence": confidence,
                 "probabilities": levels, "legend": {str(i): "x" for i in range(5)}},
        "rest_of_season": {"type": "score", "score": ros, "confidence": confidence,
                           "probabilities": levels, "legend": {str(i): "x" for i in range(5)}},
        "usage_growing": {"type": "noul", "noul": growing},
        "keeper": {"type": "noul", "noul": keeper}}}


def client_returning(by_player, capture=None):
    """A real SDK client on a mock transport, answering per player name.

    Keyed by player rather than by call order because the SDK retries 5xx responses,
    so one failing player can mean several requests.
    """
    def handle(request):
        body = json.loads(request.content)
        if capture is not None:
            capture.append(body)
        reply = by_player.get(body["state"]["player"]["name"], answers())
        if isinstance(reply, int):
            return httpx2.Response(reply, json={"error": {"message": "nope"}})
        return httpx2.Response(200, json=reply)

    from typesafe_sdk import RetryPolicy
    return TypeSafeClient(api_key="sk-test", transport=httpx2.MockTransport(handle),
                          retry=RetryPolicy(max_retries=0))


def test_top_marks_score_near_one_and_bottom_marks_near_zero():
    best, confidence = jev.composite(_parsed(answers(role=4.0, ros=4.0, growing=1.0, keeper=1.0)))
    worst, _ = jev.composite(_parsed(answers(role=0.0, ros=0.0, growing=0.0, keeper=0.0)))
    assert best == 1.0 and worst == 0.0 and confidence == 1.0


def test_weights_favour_role_and_rest_of_season_over_the_yes_no_questions():
    strong_role, _ = jev.composite(_parsed(answers(role=4.0, ros=4.0, growing=0.0, keeper=0.0)))
    strong_flags, _ = jev.composite(_parsed(answers(role=0.0, ros=0.0, growing=1.0, keeper=1.0)))
    assert strong_role == 0.75 and strong_flags == 0.25
    assert round(sum(jev.WEIGHTS.values()), 6) == 1.0


def test_an_unsure_judgment_is_pulled_toward_neutral():
    sure, _ = jev.composite(_parsed(answers(role=4.0, ros=4.0, growing=1.0, keeper=1.0, confidence=1.0)))
    unsure, confidence = jev.composite(_parsed(answers(role=4.0, ros=4.0, growing=1.0, keeper=1.0, confidence=0.2)))
    assert sure == 1.0 and confidence == 0.2
    assert unsure == 0.6, "0.2 * 1.0 + 0.8 * 0.5 — a coin-flip judgment barely moves the ranking"


def test_state_carries_only_what_the_questions_need():
    candidate = player(secret="should not be sent", season_gain=42)
    state = jev.player_state(candidate, ROSTER, LEAGUE)
    assert set(state) == {"player", "my_roster", "league"}
    assert "secret" not in json.dumps(state) and "season_gain" not in json.dumps(state)
    assert state["player"]["nfl_team"] == "KC", "pro team id 12 should be sent as a name"


def test_questions_are_well_formed():
    asked = jev.questions()
    assert set(asked) == set(jev.WEIGHTS), "every question must have a weight, and vice versa"
    for name, question in asked.items():
        assert question.instructions and not question.instructions.endswith("?")
        if hasattr(question, "criteria") and isinstance(question.criteria, (list, tuple)):
            assert 2 <= len(question.criteria) <= 10, f"{name}: Jev takes 2 to 10 score levels"


def test_scoring_ranks_the_better_player_higher():
    good, weak = player("Good Guy"), player("Weak Guy")
    sent = []
    note = jev.score([good, weak], ROSTER, LEAGUE,
                     client=client_returning({"Good Guy": answers(role=4.0, ros=4.0),
                                              "Weak Guy": answers(role=1.0, ros=0.0)}, sent))
    assert good["jev"] > weak["jev"] and note == "Ranked by Jev (jev-1.13.0)"
    assert len(sent) == 2 and sent[0]["state"]["player"]["name"] == "Good Guy"
    assert set(sent[0]["questions"]) == set(jev.WEIGHTS), "all four questions ride in one call"


def test_depth_limits_how_many_players_are_scored():
    pool = [player(f"Guy {i}") for i in range(5)]
    jev.score(pool, ROSTER, LEAGUE, client=client_returning({}), depth=2)
    assert all("jev" in p for p in pool[:2]) and all("jev" not in p for p in pool[2:])


def test_one_failed_call_does_not_lose_the_rest():
    first, second = player("First"), player("Second")
    note = jev.score([first, second], ROSTER, LEAGUE, client=client_returning({"First": 500}))
    assert "jev" not in first and "jev" in second
    assert "1 of 2 calls failed" in note


def test_total_failure_falls_back_instead_of_raising():
    people = [player("One"), player("Two")]
    note = jev.score(people, ROSTER, LEAGUE, client=client_returning({"One": 500, "Two": 500}))
    assert all("jev" not in p for p in people)
    assert note == "Jev ranking failed; falling back to the projection ranking"


def test_missing_key_is_reported_not_raised(monkeypatch=None):
    import os
    saved = os.environ.pop("TYPESAFE_API_KEY", None)
    os.environ["TYPESAFE_API_KEY"] = ""
    try:
        note = jev.score([player()], ROSTER, LEAGUE)
        assert note.startswith("Jev ranking skipped"), note
    finally:
        if saved:
            os.environ["TYPESAFE_API_KEY"] = saved


def test_usage_trend_is_computed_in_code_not_asked_of_jev():
    week = lambda carries, targets, attempts=0: {"carries": carries, "targets": targets,
                                                 "receptions": 0, "pass_attempts": attempts}
    rising = jev.usage_summary([week(2, 1), week(3, 1), week(9, 4)], "RB")
    assert rising["by_week"] == [3, 4, 13] and rising["trend"] == "rising" and rising["per_game"] == 6.7
    assert jev.usage_summary([week(6, 2), week(7, 1)], "RB")["trend"] == "steady"
    assert jev.usage_summary([week(8, 4), week(2, 0)], "WR")["trend"] == "falling"
    assert jev.usage_summary([week(5, 5)], "WR")["trend"] == "one game so far"
    qb = jev.usage_summary([week(3, 0, 34), week(2, 0, 38)], "QB")
    assert qb["measure"] == "pass attempts" and qb["by_week"] == [34, 38]
    assert jev.usage_summary([], "RB") is None


def test_usage_goes_into_the_state_only_when_we_have_it():
    with_usage = jev.player_state(player(usage={"by_week": [5, 9], "trend": "rising"}), ROSTER, LEAGUE)
    without = jev.player_state(player(), ROSTER, LEAGUE)
    assert with_usage["player"]["actual_usage_this_season"]["trend"] == "rising"
    assert "actual_usage_this_season" not in without["player"]


def test_kickers_and_defenses_are_not_sent_to_jev():
    assert jev.SCORED_POSITIONS == {"QB", "RB", "WR", "TE"}


def _parsed(body):
    """Turn a response body into the SDK's answer objects, so tests use real types."""
    client = client_returning({"probe": body})
    from typesafe_sdk import Noul
    return client.system_one(state={"player": {"name": "probe"}}, questions={
        name: Noul(instructions="placeholder") for name in body["answers"]}).answers


if __name__ == "__main__":
    if SKIP:
        sys.exit(f"skipped: {SKIP}")
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
