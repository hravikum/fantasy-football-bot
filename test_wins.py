"""Tests for turning points into wins. Run: python3 test_wins.py"""
import sys

import wins


def matchup(period, home, away, home_points, away_points, winner="HOME"):
    return {"matchupPeriodId": period, "winner": winner,
            "home": {"teamId": home, "totalPoints": home_points}, "away": {"teamId": away, "totalPoints": away_points}}


def test_the_spread_waits_for_enough_weeks_then_uses_the_league_s_own():
    assert wins.spread([matchup(1, 1, 2, 100, 90)]) == wins.SPREAD
    schedule = [matchup(week, 1, 2, 100 + (10 if week % 2 else -10), 90 + (10 if week % 2 else -10))
                for week in range(1, 17)]
    assert abs(wins.spread(schedule) - 10.3) < 0.1, wins.spread(schedule)
    unplayed = schedule + [matchup(17, 1, 2, 0, 0, winner="UNDECIDED")]
    assert wins.spread(unplayed) == wins.spread(schedule), "a game not yet played isn't a score"


def test_opponents_by_period():
    schedule = [matchup(1, 13, 4, 0, 0), matchup(2, 7, 13, 0, 0), matchup(2, 1, 2, 0, 0)]
    assert wins.opponents(schedule, 13) == {1: 4, 2: 7}


def test_an_even_week_is_half_a_win_in_each_game():
    assert abs(wins.week_wins(100, 100, [90, 100, 110], 25) - 1.0) < 1e-9
    assert abs(wins.week_wins(100, 100, [90, 100, 110], 25, playoff=True) - 0.5) < 1e-9


def test_a_point_is_worth_more_in_a_close_week_than_a_blowout():
    others = [100.0] * 13
    close = wins.wins_added([5.0], [(100.0, 100.0, others, False)], 25)
    lopsided = wins.wins_added([5.0], [(150.0, 90.0, others, False)], 25)
    assert close > 3 * lopsided > 0
    assert wins.wins_added([0.0, 0.0], [(100.0, 100.0, others, False)] * 2, 25) == 0


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
