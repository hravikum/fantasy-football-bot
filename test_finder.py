"""Tests for the trade finder. Run: python3 test_finder.py"""
import sys

import finder
import odds
from test_lineup import full_roster, player
from test_trades import context

LEFT = {p: [p] for p in range(5, 15)}     # test_trades' league: week 5 now, regular season to 14


def keeper(person, surplus):
    person["keeper"] = {"rank": 20, "value": 3000.0, "age": 23.0, "worth_round": 2, "cost_round": 9,
                        "surplus": surplus}
    return person


def league(mine, theirs, my_wins=0.0, their_wins=20.0, my_mean=100.0):
    """You (1) and one contender (2); one playoff spot."""
    ctx = context(mine, theirs)
    means = {p: {1: (my_mean, 1.0), 2: (100.0, 1.0)} for p in LEFT}
    model = odds.Model([1, 2], {1: (my_wins, 1000.0), 2: (their_wins, 1000.0)}, {},
                       {p: [(1, 2)] for p in LEFT}, means, 25.0, False, "TOTAL_POINTS_SCORED", spots=1, sims=400)
    return ctx, model


def rosters():
    mine = full_roster(bench_rb=keeper(player("RB", 20, 2.0), 100.0))
    theirs = full_roster(wr1=keeper(player("WR", 4, 13.0), 3000.0), bench_wr=keeper(player("WR", 20, 3.0), 2000.0))
    return mine, theirs


def test_a_starter_for_their_spare_keeper():
    mine, theirs = rosters()
    ctx, model = league(mine, theirs)
    deals, base, (best, value) = finder.find(ctx, model, LEFT)
    assert best is mine["bench_rb"] and value == 100.0
    assert base == 1.0, "a team twenty wins back is last whatever happens"
    top = deals[0]
    assert [p["id"] for p in top["get"]] == [theirs["bench_wr"]["id"]]
    assert top["keeper_gain"] == 1900.0
    assert top["them_playoffs"] > 0 and top["last_after"] == 1.0
    assert all(d["them_playoffs"] >= finder.THEIR_MIN_GAIN for d in deals), "every deal helps them in the playoffs"


def test_their_own_best_keeper_is_never_asked_for():
    mine, theirs = rosters()
    ctx, model = league(mine, theirs)
    deals, _, _ = finder.find(ctx, model, LEFT)
    assert deals and all(theirs["wr1"]["id"] not in {p["id"] for p in d["get"]} for d in deals)


def test_a_player_who_wouldnt_start_for_them_buys_nothing():
    mine, theirs = rosters()
    ctx, model = league(mine, theirs)
    deals, _, _ = finder.find(ctx, model, LEFT)
    assert all(d["give"] != [mine["bench_wr"]] for d in deals), "a 3-point bench receiver doesn't help them"


def test_a_deal_that_makes_last_likelier_is_dropped():
    mine, theirs = rosters()
    ctx, model = league(mine, theirs, my_wins=10.0, their_wins=10.0)
    deals, base, _ = finder.find(ctx, model, LEFT)
    assert 0.3 < base < 0.7, base
    assert all(d["last_after"] - base <= finder.MAX_RISE for d in deals)
    assert all(d["give"] != [mine["wr1"]] for d in deals), "giving a 13-point starter for a 3-point one costs too much"


def test_no_contender_no_deals():
    mine, theirs = rosters()
    ctx, model = league(mine, theirs, my_wins=20.0, their_wins=0.0)
    deals, _, _ = finder.find(ctx, model, LEFT)
    assert deals == [], "a team out of the race isn't buying"


def test_nobody_better_than_your_keeper_means_no_deals():
    mine, theirs = rosters()
    keeper(mine["bench_rb"], 5000.0)
    ctx, model = league(mine, theirs)
    assert finder.find(ctx, model, LEFT)[0] == []


def test_the_report_reads():
    mine, theirs = rosters()
    ctx, model = league(mine, theirs)
    deals, base, best = finder.find(ctx, model, LEFT)
    text = finder.describe(deals, base, best, ctx)
    assert "1. With Them" in text and "Keeper:" in text and "Read-only" in text, text
    assert "costs a 9th" in text


def test_gains_by_period_sum_the_weeks_of_each():
    weeks = [(5, 100, 104), (6, 100, 98), (15, 100, 120)]
    assert finder.gains_by_period(weeks, {5: [5], 6: [6]}) == {5: 4, 6: -2}


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
