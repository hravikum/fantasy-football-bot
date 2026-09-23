"""Tests for the lineup and IR planner. Run: python test_lineup.py"""
import sys

from espn import BENCH, IR
from lineup import MIN_GAIN, decide, plan, would_act

SLOTS = {0: 1, 2: 2, 4: 2, 6: 1, 16: 1, 17: 1, 20: 6, 21: 1, 23: 2}
ELIGIBLE = {"QB": [0, 7, 20, 21], "RB": [2, 3, 23, 7, 20, 21], "WR": [4, 3, 5, 23, 7, 20, 21],
            "TE": [6, 5, 23, 7, 20, 21], "D/ST": [16, 20, 21], "K": [17, 20, 21]}
GAMES = {team: (99, None) for team in range(1, 35)}  # pro team 0 stands in for a bye
_next_id = [0]


def player(position, slot, projection, status="ACTIVE", locked=False, bye=False, season=None):
    _next_id[0] += 1
    return {"id": _next_id[0], "name": f"{position}{_next_id[0]}", "position": position,
            "pro_team": 0 if bye else 1, "slot": slot, "eligible_slots": ELIGIBLE[position],
            "injury_status": status, "locked": locked, "projection": projection,
            "season_projection": projection * 17 if season is None else season}


def full_roster(**overrides):
    """A legal starting lineup plus a quiet bench, so each test changes one thing."""
    roster = {
        "qb": player("QB", 0, 20.0), "rb1": player("RB", 2, 15.0), "rb2": player("RB", 2, 14.0),
        "wr1": player("WR", 4, 13.0), "wr2": player("WR", 4, 12.0), "te": player("TE", 6, 9.0),
        "flex1": player("RB", 23, 11.0), "flex2": player("WR", 23, 10.0),
        "dst": player("D/ST", 16, 7.0), "k": player("K", 17, 8.0),
        "bench_wr": player("WR", 20, 3.0), "bench_rb": player("RB", 20, 2.0),
    }
    roster.update(overrides)
    return roster


def slots_of(roster, target):
    return {name: target[p["id"]] for name, p in roster.items()}


def target_for(roster):
    return plan(list(roster.values()), SLOTS, GAMES)[0]


def test_better_bench_player_takes_the_slot():
    roster = full_roster(bench_wr=player("WR", 20, 18.0))
    target, moves, gain, broken, _ = decide(list(roster.values()), SLOTS, GAMES)
    slots = slots_of(roster, target)
    assert slots["bench_wr"] == 4, "an 18-point WR should start"
    assert slots["wr2"] == 23, "the displaced starter drops to flex, he is still worth starting"
    assert slots["flex2"] == BENCH, "the weakest starter is the one who sits"
    assert round(gain, 1) == 8.0 and would_act(moves, gain, broken)


def test_locked_starters_push_the_upgrade_to_flex():
    roster = full_roster(wr1=player("WR", 4, 13.0, locked=True), wr2=player("WR", 4, 12.0, locked=True),
                         bench_wr=player("WR", 20, 18.0))
    slots = slots_of(roster, target_for(roster))
    assert slots["wr1"] == 4 and slots["wr2"] == 4, "locked starters keep their slots"
    assert slots["bench_wr"] == 23, "with both WR slots locked the upgrade goes to flex"


def test_locked_bench_player_is_never_promoted():
    roster = full_roster(bench_wr=player("WR", 20, 18.0, locked=True))
    slots = slots_of(roster, target_for(roster))
    assert slots["bench_wr"] == BENCH and slots["wr2"] == 4


def test_out_starter_is_replaced_even_below_the_threshold():
    # Thin bench on purpose: the only replacement is worth 1.0, under the churn threshold.
    roster = {"qb": player("QB", 0, 20.0), "rb1": player("RB", 2, 15.0),
              "rb2": player("RB", 2, 14.0, status="OUT"), "wr1": player("WR", 4, 13.0),
              "wr2": player("WR", 4, 12.0), "te": player("TE", 6, 9.0),
              "flex1": player("RB", 23, 11.0), "flex2": player("WR", 23, 10.0),
              "dst": player("D/ST", 16, 7.0), "k": player("K", 17, 8.0),
              "bench_rb": player("RB", 20, 1.0)}
    target, moves, gain, broken, _ = decide(list(roster.values()), SLOTS, GAMES)
    assert slots_of(roster, target)["rb2"] == IR, "an OUT player belongs on IR, not in the lineup"
    assert round(gain, 1) == 1.0 and gain < MIN_GAIN
    assert would_act(moves, gain, broken), "a dead starter is fixed regardless of the gain"


def test_bye_week_starter_is_replaced():
    roster = full_roster(wr2=player("WR", 4, 12.0, bye=True))
    target, moves, gain, broken, _ = decide(list(roster.values()), SLOTS, GAMES)
    assert slots_of(roster, target)["wr2"] == BENCH
    assert broken and would_act(moves, gain, broken)


def test_optimal_lineup_produces_no_moves():
    _, moves, gain, broken, _ = decide(list(full_roster().values()), SLOTS, GAMES)
    assert moves == [] and gain == 0.0 and not would_act(moves, gain, broken)


def test_small_upgrade_is_found_but_not_applied():
    roster = full_roster(bench_wr=player("WR", 20, 10.5))  # +0.5 over the weakest starter
    _, moves, gain, broken, _ = decide(list(roster.values()), SLOTS, GAMES)
    assert moves and round(gain, 1) == 0.5
    assert not would_act(moves, gain, broken), "under the threshold the bot holds"


def test_flex_takes_the_best_remaining_and_not_a_kicker():
    roster = full_roster(bench_rb=player("RB", 20, 10.5), k=player("K", 17, 30.0))
    slots = slots_of(roster, target_for(roster))
    assert slots["bench_rb"] == 23 and slots["flex2"] == BENCH
    assert slots["k"] == 17, "a kicker can never fill flex"


def test_thin_roster_leaves_a_slot_empty_rather_than_breaking():
    roster = {"qb": player("QB", 0, 20.0), "rb1": player("RB", 2, 15.0), "k": player("K", 17, 8.0)}
    assert slots_of(roster, target_for(roster)) == {"qb": 0, "rb1": 2, "k": 17}


def test_out_bench_player_is_stashed_on_ir():
    roster = full_roster(bench_rb=player("RB", 20, 0.0, status="OUT"))
    _, moves, gain, broken, _ = decide(list(roster.values()), SLOTS, GAMES)
    assert slots_of(roster, target_for(roster))["bench_rb"] == IR
    assert would_act(moves, gain, broken), "an IR move happens whatever the point gain"


def test_suspended_player_is_not_stashed():
    roster = full_roster(bench_rb=player("RB", 20, 0.0, status="SUSPENSION"))
    assert slots_of(roster, target_for(roster))["bench_rb"] == BENCH, "ESPN does not allow SSPD on IR"


def test_upgrade_to_questionable_does_not_pull_him_off_ir():
    roster = full_roster(hurt=player("RB", IR, 6.0, status="QUESTIONABLE"))
    assert slots_of(roster, target_for(roster))["hurt"] == IR


def test_a_kicker_who_plays_is_never_swapped_for_a_better_projection():
    """Measured in this league: ESPN's kicker projections don't track results, so a swap is a coin flip."""
    roster = full_roster(bench_k=player("K", BENCH, 14.0))      # your kicker projects 8.0
    slots = slots_of(roster, target_for(roster))
    assert slots["k"] == 17 and slots["bench_k"] == BENCH


def test_but_a_kicker_who_cannot_play_is_replaced():
    roster = full_roster(k=player("K", 17, 8.0, bye=True), bench_k=player("K", BENCH, 6.0))
    slots = slots_of(roster, target_for(roster))
    assert slots["bench_k"] == 17 and slots["k"] == BENCH


def test_the_same_holds_for_defenses():
    steady = full_roster(bench_dst=player("D/ST", BENCH, 12.0))
    hurt = full_roster(dst=player("D/ST", 16, 7.0, status="OUT"), bench_dst=player("D/ST", BENCH, 4.0))
    assert slots_of(steady, target_for(steady))["dst"] == 16
    assert slots_of(hurt, target_for(hurt))["bench_dst"] == 16


def test_the_rest_of_the_lineup_still_optimises_around_them():
    roster = full_roster(bench_k=player("K", BENCH, 14.0), bench_wr=player("WR", BENCH, 18.0))
    slots = slots_of(roster, target_for(roster))
    assert slots["bench_k"] == BENCH and slots["bench_wr"] == 4


def test_healed_player_comes_off_ir():
    roster = full_roster(hurt=player("RB", IR, 16.0, status="ACTIVE"))
    target, moves, gain, broken, blocked = decide(list(roster.values()), SLOTS, GAMES)
    assert slots_of(roster, target)["hurt"] in (2, 23), "a cleared player is worth starting here"
    assert not blocked and would_act(moves, gain, broken)


def test_healed_player_stays_put_when_there_is_no_roster_spot():
    roster = full_roster(hurt=player("RB", IR, 1.0, status="ACTIVE"))
    for i in range(4):  # fill the bench: 10 starters + 6 bench = 16 active spots
        roster[f"filler{i}"] = player("WR", 20, 1.0)
    target, _, _, _, blocked = decide(list(roster.values()), SLOTS, GAMES)
    assert slots_of(roster, target)["hurt"] == IR
    assert [p["name"] for p in blocked] == [roster["hurt"]["name"]], "flagged for a human to resolve"


def test_the_more_valuable_injury_gets_the_only_ir_slot():
    roster = full_roster(cheap=player("RB", 20, 0.0, status="OUT", season=40.0),
                         star=player("WR", 20, 0.0, status="OUT", season=220.0))
    slots = slots_of(roster, target_for(roster))
    assert slots["star"] == IR and slots["cheap"] == BENCH


def test_season_ending_injury_outranks_a_one_week_out():
    roster = full_roster(week_to_week=player("RB", 20, 0.0, status="OUT", season=300.0),
                         done=player("WR", 20, 0.0, status="INJURY_RESERVE", season=50.0))
    slots = slots_of(roster, target_for(roster))
    assert slots["done"] == IR and slots["week_to_week"] == BENCH


if __name__ == "__main__":
    tests = [(name, fn) for name, fn in sorted(globals().items()) if name.startswith("test_")]
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
