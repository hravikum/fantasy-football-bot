"""Tests for the rest-of-season odds. Run: python3 test_odds.py"""
import sys

import odds
from espn import BENCH
from test_lineup import GAMES, SLOTS, full_roster, player
from test_trades import context


def game(period, home, home_points, away, away_points, winner=None):
    winner = winner or ("HOME" if home_points > away_points else "AWAY" if away_points > home_points else "TIE")
    return {"matchupPeriodId": period, "winner": winner,
            "home": {"teamId": home, "totalPoints": home_points}, "away": {"teamId": away, "totalPoints": away_points}}


SCHEDULE = [game(1, 1, 100, 2, 90), game(1, 3, 120, 4, 80),
            game(2, 1, 95, 3, 110), game(2, 2, 130, 4, 85),
            game(3, 1, 0, 4, 0, winner="UNDECIDED"), game(3, 2, 0, 3, 0, winner="UNDECIDED")]


def model(means, start, pairs, sd=0.0, with_median=True, rule="TOTAL_POINTS_SCORED", h2h=None, sims=50):
    teams = sorted(start)
    return odds.Model(teams, start, h2h or {}, pairs, means, sd, with_median, rule, spots=2, sims=sims)


def test_records_count_the_matchup_and_the_median_game():
    record, h2h, done = odds.results(SCHEDULE, regular=14, with_median=True)
    assert done == 2, "week 3 isn't finished"
    # Week 1 median 95: teams 1 (100) and 3 (120) above it. Week 2 median 102.5: teams 2 and 3 above.
    assert record[1][:3] == [2, 2, 0], "beat team 2, lost to team 3, above the median once"
    assert record[3][:3] == [4, 0, 0]
    assert record[4][:3] == [0, 4, 0]
    assert record[1][3] == 195
    assert h2h[(1, 2)] == 1 and h2h[(3, 1)] == 1 and (2, 1) not in h2h


def test_without_the_median_game_a_week_is_one_result():
    record, _, _ = odds.results(SCHEDULE, regular=14, with_median=False)
    assert record[1][:3] == [1, 1, 0] and record[3][:3] == [2, 0, 0]


def test_playoff_games_are_left_out():
    record, _, done = odds.results(SCHEDULE + [game(15, 1, 200, 3, 10)], regular=14, with_median=True)
    assert done == 2 and record[1][0] == 2


def test_espn_records_with_two_results_a_week_mean_the_median_counts():
    two = [{"record": {"overall": {"wins": 3, "losses": 1, "ties": 0}}}] * 4
    one = [{"record": {"overall": {"wins": 1, "losses": 1, "ties": 0}}}] * 4
    assert odds.espn_counts_median(two, done=2) is True
    assert odds.espn_counts_median(one, done=2) is False


def test_periods_left_start_with_this_week():
    settings = {"scheduleSettings": {"matchupPeriodCount": 14,
                                     "matchupPeriods": {str(p): [p] for p in range(1, 18)}}}
    assert odds.periods_left(settings, 12) == {12: [12], 13: [13], 14: [14]}
    assert odds.periods_left(settings, 15) == {}


def test_a_much_weaker_team_finishes_last():
    start = {1: (4, 400), 2: (4, 400), 3: (4, 400), 4: (4, 400)}
    means = {3: {1: (60, 1.0), 2: (120, 1.0), 3: (110, 1.0), 4: (100, 1.0)}}
    result = model(means, start, {3: [(1, 4), (2, 3)]}).run()
    assert result[1][0] == 1.0, "with no spread, the 60-point team loses both games and is last"
    assert result[2][1] == 1.0 and result[3][1] == 1.0, "the top two make the playoffs"


def test_the_record_so_far_counts():
    start = {1: (8, 800), 2: (1, 500), 3: (5, 600), 4: (5, 600)}
    means = {3: {1: (60, 1.0), 2: (120, 1.0), 3: (110, 1.0), 4: (100, 1.0)}}
    result = model(means, start, {3: [(1, 4), (2, 3)]}).run()
    assert result[2][0] == 1.0, "a 1-win team can't catch up in one week, even winning both games"
    assert result[1][0] == 0.0


def test_ties_on_record_go_to_total_points():
    start = {1: (4, 390), 2: (4, 450), 3: (6, 500), 4: (6, 500)}
    means = {3: {1: (100, 1.0), 2: (100, 1.0), 3: (100, 1.0), 4: (100, 1.0)}}
    result = model(means, start, {3: [(1, 3), (2, 4)]}, with_median=False).run()
    assert result[1][0] == 1.0, "level scores tie every game; team 1 has fewer points"


def test_head_to_head_breaks_ties_when_espn_says_so():
    start = {1: (4, 390), 2: (4, 450), 3: (6, 500), 4: (6, 500)}
    means = {3: {1: (100, 1.0), 2: (100, 1.0), 3: (100, 1.0), 4: (100, 1.0)}}
    result = model(means, start, {3: [(1, 3), (2, 4)]}, with_median=False, rule="H2H_RECORD",
                   h2h={(1, 2): 1}).run()
    assert result[2][0] == 1.0, "team 1 beat team 2, so team 2 is last despite more points"


def test_more_points_in_a_week_lower_the_chance_of_last():
    start = {1: (4, 400), 2: (4, 400), 3: (4, 400), 4: (4, 400)}
    means = {3: {1: (95, 1.0), 2: (100, 1.0), 3: (100, 1.0), 4: (100, 1.0)},
             4: {1: (95, 1.0), 2: (100, 1.0), 3: (100, 1.0), 4: (100, 1.0)}}
    pairs = {3: [(1, 2), (3, 4)], 4: [(1, 3), (2, 4)]}
    m = model(means, start, pairs, sd=25.0, sims=4000)
    base = m.run()[1][0]
    bumped = odds.point_values(m, 1)
    assert base > 0.25, f"the weakest of four is the likeliest last ({base:.2f})"
    assert all(after < base for after in bumped.values()), (base, bumped)


def test_the_same_draws_every_run():
    start = {1: (4, 400), 2: (4, 400), 3: (4, 400), 4: (4, 400)}
    means = {3: {t: (100, 1.0) for t in start}}
    one = model(means, start, {3: [(1, 2), (3, 4)]}, sd=25.0, sims=500).run()
    two = model(means, start, {3: [(1, 2), (3, 4)]}, sd=25.0, sims=500).run()
    assert one == two


def test_matchup_chance_for_the_watched_team():
    start = {1: (4, 400), 2: (4, 400), 3: (4, 400), 4: (4, 400)}
    means = {3: {1: (130, 1.0), 2: (90, 1.0), 3: (100, 1.0), 4: (100, 1.0)}}
    _, matchup = model(means, start, {3: [(1, 2), (3, 4)]}).run(watch=1)
    assert matchup == {3: 1.0}


def test_this_week_counts_what_locked_starters_scored():
    roster = full_roster(qb=player("QB", 0, 20.0, locked=True), rb1=player("RB", 2, 15.0, locked=True))
    roster["qb"]["actual"], roster["rb1"]["actual"] = 31.0, 2.0
    points, share = odds.this_week_mean(list(roster.values()), SLOTS, GAMES)
    projected_rest = 14 + 13 + 12 + 9 + 11 + 10 + 7 + 8
    assert round(points, 1) == 31 + 2 + projected_rest
    assert round(share, 3) == round(projected_rest / (projected_rest + 35), 3)


def test_nothing_left_to_play_has_no_spread():
    roster = {k: dict(p, locked=True, actual=p["projection"]) for k, p in full_roster().items()}
    _, share = odds.this_week_mean(list(roster.values()), SLOTS, GAMES)
    assert share == 0.0


def test_later_weeks_use_the_best_lineup():
    roster = full_roster(bench_wr=player("WR", BENCH, 18.0))
    ctx = context(roster, full_roster())
    found = odds.means_for(list(roster.values()), ctx, [5, 6])
    assert found[6][1] == 1.0
    assert round(found[6][0], 1) == round(20 + 15 + 14 + 18 + 13 + 9 + 12 + 11 + 7 + 8, 1), found


def league_of_four(fourth):
    teams = [{"id": t, "record": {"overall": {"wins": w, "losses": 4 - w, "ties": 0, "pointsFor": pf}}}
             for t, w, pf in ((1, 2, 195), (2, 2, 220), (3, 4, 230), (4, 0, 165))]
    settings = {"scheduleSettings": {"matchupPeriodCount": 4, "matchupPeriods": {str(p): [p] for p in range(1, 7)},
                                     "playoffSeedingRule": "TOTAL_POINTS_SCORED", "playoffTeamCount": 2},
                "tradeSettings": {"deadlineDate": 1796500000000}}
    schedule = SCHEDULE + [game(4, 1, 0, 2, 0, winner="UNDECIDED"), game(4, 3, 0, 4, 0, winner="UNDECIDED")]
    ctx = context(full_roster(), full_roster())
    ctx.update(week=3, names={1: "You", 2: "Them", 3: "Three", 4: "Four"},
               rosters={1: list(full_roster().values()), 2: list(full_roster().values()),
                        3: list(full_roster().values()), 4: fourth})
    return odds.build(FakeESPN({"settings": settings, "teams": teams}, schedule), ctx=ctx, sims=300)


def test_a_team_projecting_far_below_its_scoring_is_flagged():
    m, facts = league_of_four([player("QB", 0, 20.0)])
    assert facts["holes"][4] == 9, "a quarterback alone leaves nine starting slots empty"
    text = odds.describe(m, facts)
    assert "Projection check" in text and "Four: 20 a week against 82 so far; 9 starting slots nobody can fill" in text, text
    assert "  You:" not in text, "a full roster isn't flagged"


class FakeESPN:
    def __init__(self, league, schedule):
        self._league, self._schedule = league, schedule

    def league(self, *views, week=None):
        return {"schedule": self._schedule} if views == ("mMatchupScore",) else self._league


def test_the_report_from_end_to_end():
    teams = [{"id": t, "record": {"overall": {"wins": w, "losses": 4 - w, "ties": 0, "pointsFor": pf}}}
             for t, w, pf in ((1, 2, 195), (2, 2, 220), (3, 4, 230), (4, 0, 165))]
    settings = {"scheduleSettings": {"matchupPeriodCount": 4, "matchupPeriods": {str(p): [p] for p in range(1, 7)},
                                     "playoffSeedingRule": "TOTAL_POINTS_SCORED", "playoffTeamCount": 2},
                "tradeSettings": {"deadlineDate": 1796500000000}}
    schedule = SCHEDULE + [game(4, 1, 0, 2, 0, winner="UNDECIDED"), game(4, 3, 0, 4, 0, winner="UNDECIDED")]
    ctx = context(full_roster(), full_roster())
    ctx.update(week=3, names={1: "You", 2: "Them", 3: "Three", 4: "Four"},
               rosters={1: list(full_roster().values()), 2: list(full_roster().values()),
                        3: list(full_roster().values()), 4: list(full_roster(qb=player("QB", 0, 1.0)).values())})
    m, facts = odds.build(FakeESPN({"settings": settings, "teams": teams}, schedule), ctx=ctx, sims=300)
    assert m.with_median and not facts["mismatched"], facts
    assert sorted(m.periods) == [3, 4]
    text = odds.describe(m, facts)
    assert "Your chance of finishing last" in text and "week 3: v Four" in text and "Trade deadline" in text, text
    result = m.run()
    assert result[4][0] > result[1][0], "the 0-4 team with a 1-point quarterback is the likeliest last"


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
