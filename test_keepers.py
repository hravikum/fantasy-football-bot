"""Tests for keeper value. Run: python3 test_keepers.py"""
import sys

import keepers
from test_lineup import player

TEAMS = 14


def values(count=200):
    """Ranks 1..count, value falling 50 a rank from 10,000, ESPN ids 1000 + rank."""
    return {1000 + rank: {"rank": rank, "value": 10000.0 - 50 * rank, "age": 24.0, "name": f"P{rank}"}
            for rank in range(1, count + 1)}


def on(player_id, position="WR"):
    person = player(position, 20, 5.0)
    person["id"] = player_id
    return person


def test_a_pick_is_worth_the_player_ranked_mid_round():
    by_rank = keepers.slot_values(values())
    assert keepers.pick_value(9, TEAMS, by_rank) == 10000 - 50 * (8 * 14 + 7), "pick 119 in round 9"
    assert keepers.pick_value(1, TEAMS, by_rank) == 10000 - 50 * 7


def test_someone_not_kept_costs_a_ninth():
    roster = [on(1010)]
    keepers.attach(roster, values(), kept={}, teams=TEAMS)
    k = roster[0]["keeper"]
    assert k["cost_round"] == 9 and k["worth_round"] == 1
    assert k["surplus"] == (10000 - 500) - (10000 - 50 * 119)


def test_the_cost_climbs_each_time_he_is_kept():
    roster = [on(1010), on(1011), on(1012)]
    keepers.attach(roster, values(), kept={1010: 9, 1011: 5, 1012: 2}, teams=TEAMS)
    assert roster[0]["keeper"]["cost_round"] == 5
    assert roster[1]["keeper"]["cost_round"] == 2
    assert roster[2]["keeper"] is None, "nobody has said what one kept in the 2nd costs"


def test_kickers_and_unknowns_have_no_keeper_value():
    roster = [on(99999, "K")]
    keepers.attach(roster, values(), kept={}, teams=TEAMS)
    assert roster[0]["keeper"] is None and keepers.surplus(roster[0]) == 0.0


def test_the_best_keeper_must_beat_his_pick():
    good, poor = on(1020), on(1190)
    keepers.attach([good, poor], values(), kept={}, teams=TEAMS)
    assert keepers.best([good, poor])[0] is good
    assert keepers.best([poor]) == (None, 0.0), "ranked 190th, he's worth less than a 9th-round pick gets"


def test_a_player_drafted_before_the_ninth_cant_be_kept():
    early, late, pickup = on(1001), on(1002), on(1003)
    keepers.attach([early, late, pickup], values(), kept={}, teams=TEAMS, drafted={1001: 1, 1002: 9})
    assert early["keeper"] is None and early["not_keepable"], "a first-round pick can't be kept"
    assert late["keeper"]["cost_round"] == 9, "a 9th-round pick can, for a 9th"
    assert pickup["keeper"]["cost_round"] == 9, "so can a player picked up during the season"


def test_a_keeper_from_this_season_stays_keepable():
    kept_in_fifth = on(1004)
    keepers.attach([kept_in_fifth], values(), kept={1004: 5}, teams=TEAMS, drafted={1004: 5})
    assert kept_in_fifth["keeper"]["cost_round"] == 2, "kept in the 5th, he costs a 2nd"


def test_drafted_rounds_come_from_the_draft():
    draft = {"picks": [{"playerId": 5, "roundId": 9, "keeper": True}, {"playerId": 6, "roundId": 1}]}
    assert keepers.drafted_rounds(draft) == {5: 9, 6: 1}


def test_the_report_names_your_players_who_cant_be_kept():
    mine = [on(1001), on(1100)]
    rosters = {1: mine, 2: [on(1150)]}
    for roster in rosters.values():
        keepers.attach(roster, values(), kept={}, teams=TEAMS, drafted={1001: 1})
    text = keepers.describe(rosters, {1: "You", 2: "Them"}, 1)
    assert f"not keepable: {mine[0]['name']}" in text and f"Best now: {mine[1]['name']}" in text, text


def test_kept_rounds_come_from_the_draft():
    draft = {"picks": [{"playerId": 5, "roundId": 9, "keeper": True}, {"playerId": 6, "roundId": 1, "keeper": False}]}
    assert keepers.kept_rounds(draft) == {5: 9}
    assert keepers.kept_rounds(None) == {}


def test_ordinals():
    assert [keepers.ordinal(n) for n in (1, 2, 3, 5, 9, 11, 12, 13, 21)] == \
        ["1st", "2nd", "3rd", "5th", "9th", "11th", "12th", "13th", "21st"]


def test_the_report_marks_each_teams_own_best():
    mine, theirs, other = [on(1100)], [on(1005), on(1030)], [on(1040)]
    rosters = {1: mine, 2: theirs, 3: other}
    for roster in rosters.values():
        keepers.attach(roster, values(), kept={}, teams=TEAMS)
    text = keepers.describe(rosters, {1: "You", 2: "Them", 3: "Three"}, 1)
    assert f"Best now: {mine[0]['name']}" in text, text
    assert text.count("(their best)") == 2, text
    assert text.index(theirs[0]["name"]) < text.index(theirs[1]["name"]), "best surplus first"


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
