"""Tests for player levels. Run: python3 test_values.py"""
import sys

import trades
import value_test
import values
import waivers
from espn import BENCH
from lineup import plan
from test_lineup import GAMES, SLOTS, full_roster, player
from test_waivers import free_agent, roster_at_capacity

SEASON = 2026
HERBERT = [13.3, 7.9, 12.7, 9.6, 10.2, 8.8, 7.8, 10.4]    # his first eight games of 2026


def line(week, points, source=0, season=SEASON, split=1, stats=None):
    """One of ESPN's stat lines: source 0 is what happened, 1 is a projection."""
    return {"seasonId": season, "scoringPeriodId": week, "statSourceId": source, "statSplitTypeId": split,
            "appliedTotal": points, "stats": {"53": 4.0} if stats is None else stats}


class FakeESPN:
    season = SEASON

    def __init__(self, cards):
        self.cards, self.asked = cards, None

    def player_cards(self, player_ids, final_week):
        self.asked = list(player_ids)
        return {pid: self.cards[pid] for pid in player_ids if pid in self.cards}


QB, RB = values.PRIOR_GAMES["QB"], values.PRIOR_GAMES["RB"]


def test_the_weights_are_the_ones_the_league_test_chose():
    assert values.PRIOR_GAMES == {"QB": 4, "RB": 16, "WR": 16} and values.FILTERED == {"QB", "RB", "WR"}


def test_with_no_games_the_level_is_the_preseason_number():
    assert values.level(12.0, [], QB) == values.level(12.0, [], RB) == 12.0


def test_one_game_moves_a_quarterback_a_fifth_of_the_way_and_a_back_much_less():
    assert 12.0 < values.level(10.0, [20.0], QB) < 12.3
    assert 10.6 < values.level(10.0, [20.0], RB) < 10.9, "ESPN's number is the better guide at running back"


def test_a_bad_season_overrides_a_good_projection():
    """The report listed Herbert on 30 of 32 mornings at 276 / 17 = 16.2 a game. He was scoring 10.1."""
    level = values.level(276 / 17, HERBERT, QB)
    assert 10.1 < level < 12.0, level


def test_it_closes_on_what_he_scores_and_never_passes_it():
    for weight in (QB, RB):
        levels = [values.level(2.0, [15.0] * games, weight) for games in range(9)]
        assert all(earlier < later < 15.0 for earlier, later in zip(levels, levels[1:]))
    assert values.level(2.0, [15.0] * 4, QB) > 9.0, "four starts and a backup quarterback has mostly caught up"
    assert values.level(2.0, [15.0] * 4, RB) < 7.0, "a back hasn't, which is what roles.py is for"


def test_one_big_game_is_not_a_new_level():
    assert values.level(3.0, [3.0, 3.0, 3.0, 25.0], QB) < 7.0 and values.level(3.0, [3.0, 3.0, 3.0, 25.0], RB) < 5.5


def test_a_game_late_in_the_season_still_counts():
    for weight in (QB, RB):
        assert values.level(10.0, [10.0] * 12 + [20.0] * 4, weight) > 14.0, "drift keeps the estimate from freezing"


def test_a_missed_game_is_not_a_zero():
    card = {"stats": [line(1, 14.0), line(2, 0.0, stats={}), line(3, 9.0)]}
    assert values.games_played(card, SEASON, 5) == [(1, 14.0), (3, 9.0)]


def test_a_game_he_played_and_did_nothing_in_is_a_zero():
    card = {"stats": [line(1, 0.0, stats={"210": 1.0})]}
    assert values.games_played(card, SEASON, 5) == [(1, 0.0)]


def test_only_finished_weeks_of_this_season_count():
    card = {"stats": [line(4, 30.0),                      # this week: could still be playing
                      line(3, 8.0), line(2, 6.0),
                      line(2, 99.0, source=1),            # a projection
                      line(0, 250.0),                     # the season total
                      line(1, 77.0, season=SEASON - 1),   # last year
                      line(1, 55.0, split=2),
                      {"seasonId": SEASON, "scoringPeriodId": 1, "statSourceId": 0, "stats": {"53": 1.0}}]}
    assert values.games_played(card, SEASON, 4) == [(2, 6.0), (3, 8.0)], "oldest first, nothing else"


def test_attach_rates_quarterbacks_backs_and_receivers_each_at_his_own_weight():
    back, passer = player("RB", BENCH, 5.0, season=170.0), player("QB", BENCH, 5.0, season=170.0)
    end, kicker, defense = player("TE", BENCH, 6.0), player("K", 17, 8.0), player("D/ST", 16, 7.0)
    unknown = player("WR", BENCH, 5.0)
    games = {"stats": [line(1, 20.0), line(2, 20.0), line(3, 40.0)]}
    espn = FakeESPN({back["id"]: games, passer["id"]: games, end["id"]: games, kicker["id"]: games})
    cards = values.attach(espn, [back, passer, end, kicker, defense, unknown], week=3)
    assert espn.asked == [back["id"], passer["id"], unknown["id"]], "tight ends, kickers and defenses aren't asked about"
    assert set(cards) == {back["id"], passer["id"]}
    assert back["games"] == 2 and round(back["level"], 2) == round(values.level(10.0, [20.0, 20.0], RB), 2)
    assert round(passer["level"], 2) == round(values.level(10.0, [20.0, 20.0], QB), 2) and passer["level"] > back["level"]
    assert all("level" not in p for p in (end, kicker, defense, unknown))


def test_season_pace_is_the_level_or_else_espns_number():
    assert values.season_pace({"level": 10.0, "season_projection": 300.0}) == 170.0
    assert values.season_pace({"season_projection": 300.0}) == 300.0
    assert values.season_pace({"season_projection": None}) == 0.0
    assert values.season_pace({"level": 0.0, "season_projection": 300.0}) == 0.0, "a level of zero is still a level"


def herbert_case(level=None):
    """Your quarterback projects 15 a week. The free agent projects 12 this week on a 276 season."""
    roster = list(full_roster(qb=player("QB", 0, 15.0)).values())
    stale = free_agent("QB", 12.0, season=276.0)
    if level is not None:
        stale["level"] = level
    return waivers.score_pool(roster, [stale], SLOTS, GAMES, byes={}, week=5)[0]


def test_a_quarterback_living_on_his_preseason_number_stops_being_an_upgrade():
    before, after = herbert_case(), herbert_case(level=11.4)
    assert before["ahead_gain"] > 4 and before["season_gain"] > 0, "what the report did every morning"
    assert after["ahead_gain"] == 0 and after["season_gain"] == 0
    assert waivers.bid_band(before) < 3 and waivers.bid_band(after) == 3


def test_a_breakout_counts_for_more_than_his_preseason_number():
    roster = list(full_roster().values())                     # weakest starter projects 10.0
    riser = dict(free_agent("WR", 9.0, season=51.0), level=12.5)
    ranked = waivers.score_pool(roster, [riser], SLOTS, GAMES, byes={}, week=5)[0]
    assert ranked["gain"] == 0 and ranked["ahead_gain"] == 10.0, "2.5 a week over your flex for four weeks"


def test_the_drop_goes_by_level_not_by_the_preseason_number():
    faded = dict(player("WR", BENCH, 6.0, season=200.0), level=4.0)
    risen = dict(player("WR", BENCH, 6.0, season=100.0), level=9.0)
    roster = list(roster_at_capacity(bench_wr=faded, bench_rb=risen).values())
    target, _ = plan(roster, SLOTS, GAMES)
    order = [p["id"] for p in waivers.drop_candidates(roster, target, SLOTS, [(5, GAMES)], 5, count=10)]
    assert order.index(faded["id"]) < order.index(risen["id"]), "the 200-point projection scoring 4 a game goes first"
    assert waivers.surplus(faded, {"WR": 51.0}) == 17.0 and waivers.replacement_level([risen])["WR"] == 153.0


def report_context():
    roster = list(full_roster().values())
    ranked = waivers.candidates(roster, [dict(free_agent("WR", 14.0, owned=41.0), level=11.26),
                                         free_agent("TE", 10.0)], SLOTS, GAMES)
    for candidate in ranked:
        candidate["bid"] = (0, 1)
    target, _ = plan(roster, SLOTS, GAMES)
    return {"week": 3, "budget": 100, "budget_left": 100, "minimum_bid": 0, "rivals": [], "last_runs": [],
            "gaps": [], "candidates": ranked, "drops": [], "advice": None, "roster": roster, "target": target}


def test_both_reports_show_the_level_and_a_dash_for_a_tight_end():
    context = report_context()
    text = waivers.build_report(context).splitlines()
    receiver, tight_end = (next(row for row in text if row.startswith(c["name"])) for c in context["candidates"])
    assert "LEVEL" in next(row for row in text if row.startswith("PLAYER"))
    assert receiver.split()[6:8] == ["14.0", "11.3"] and tight_end.split()[6:8] == ["10.0", "-"]
    html = waivers.build_html(context)
    assert ">Level<" in html and ">11.3<" in html and "&mdash;" in html


def test_claude_is_given_the_level_not_the_frozen_projection():
    context = report_context()
    context["roster"][0]["level"] = 18.24
    brief = waivers.brief_for_claude(context)
    assert brief["my_starters"][0]["level_per_game"] == 18.2
    assert brief["available"][0]["level_per_game"] == 11.3 and brief["available"][1]["level_per_game"] == 10.0
    assert "season_projection" not in str(brief) and "level_per_game" in brief["note_on_levels"]


def test_a_trade_values_later_weeks_at_the_level():
    from test_trades import run
    mine, theirs = full_roster(), full_roster(wr1=dict(player("WR", 4, 14.0), level=10.0))
    result, _ = run(mine, theirs, [mine["bench_rb"]], [theirs["wr1"]])
    gains = [round(after - before, 1) for _, before, after in result["you"]["weeks"]]
    assert gains[0] == 4.0 and set(gains[1:]) == {0.0}, "worth 14 this week, but only your flex's 10 from then on"


def tested(position, projection, games, counted=None, played=None, mine=False):
    return {"name": position, "position": position, "season_projection": projection * 17, "games": games,
            "counted": len(games) if counted is None else counted, "espn_games": played, "mine": mine}


def test_the_comparison_scores_each_week_against_the_weeks_that_followed():
    fading = tested("QB", 16.0, [(1, 10.0), (2, 10.0), (3, 10.0), (5, 10.0)])
    summary = value_test.compare([fading])
    weeks, espn, ours = summary["QB"]
    assert weeks == 3, "the last game has nothing after it to be scored against"
    assert espn == 6.0 and ours < espn, "ESPN's number never moves; the level closes the gap"
    assert value_test.overall(summary) == (espn, ours)


def test_the_comparison_never_lets_a_level_see_later_games():
    then_a_big_one = tested("WR", 10.0, [(1, 10.0), (2, 30.0)])
    weeks, espn, ours = value_test.compare([then_a_big_one])["WR"]
    assert weeks == 1 and espn == ours == 20.0, "after one game at his projection, his level is still 10"


def test_the_comparison_can_keep_only_the_weeks_where_the_two_disagree():
    fading = tested("QB", 16.0, [(1, 10.0), (2, 10.0), (3, 10.0), (4, 10.0), (5, 10.0), (6, 10.0)])
    steady = tested("QB", 16.0, [(1, 16.0), (2, 16.0), (3, 16.0)])
    assert value_test.compare([fading, steady])["QB"][0] == 7
    weeks, espn, ours = value_test.compare([fading, steady], apart=3.0)["QB"]
    assert weeks == 2 and espn == 6.0 and ours < 3.0, "only the fade's weeks 4 and 5, once his level had moved 3"


def test_each_position_is_scored_at_its_own_weight_and_unrated_ones_are_left_out():
    games = [(1, 2.0), (2, 14.0), (3, 8.0)]
    summary = value_test.compare([tested("QB", 8.0, games), tested("RB", 8.0, games), tested("TE", 8.0, games)])
    assert set(summary) == {"QB", "RB"}, "tight ends keep ESPN's number, so there is no level to score"
    assert summary["QB"][1] == summary["RB"][1] and summary["QB"][2] != summary["RB"][2]
    assert set(value_test.compare([tested("TE", 8.0, games)], 8)) == {"TE"}, "but any weight can be tried on any position"


def test_the_report_shows_every_weight_for_every_position_and_marks_the_one_in_use():
    players = [tested("QB", 16.0, [(1, 10.0), (2, 10.0), (3, 10.0)]), tested("RB", 8.0, [(1, 2.0), (2, 14.0), (3, 8.0)]),
               tested("TE", 8.0, [(1, 2.0), (2, 14.0), (3, 8.0)]), tested("K", 8.0, [(1, 8.0), (2, 9.0)])]
    text = value_test.report(5, players).splitlines()
    top = next(i for i, row in enumerate(text) if row.startswith("POS") and "ESPN SEASON/17" in row)
    assert [row.split()[0] for row in text[top + 1:top + 4]] == ["QB", "RB", "ALL"], "only what values.py rates"
    header = next(i for i, row in enumerate(text) if row.startswith("POS") and "ESPN ONLY" in row)
    assert text[header].split()[3:] == ["2", "games", "4", "games", "8", "games", "16", "games", "32", "games"]
    rows = {row.split()[0]: row.split()[1:] for row in text[header + 1:header + 5]}
    assert list(rows) == ["QB", "RB", "TE", "K"]
    assert [cell.endswith("*") for cell in rows["QB"]] == [False, False, True, False, False, False]
    assert [cell.endswith("*") for cell in rows["RB"]] == [False, False, False, False, True, False]
    assert not any(cell.endswith("*") for cell in rows["TE"] + rows["K"]), "no star: they keep ESPN's number"
    quarterback = [float(cell.rstrip("*")) for cell in rows["QB"]]
    assert quarterback[0] == 6.0 and quarterback[1:] == sorted(quarterback[1:]), "a fade: less weight on ESPN is better"


def test_the_comparison_looks_no_further_than_the_reports_window():
    lone = tested("RB", 10.0, [(1, 10.0), (9, 30.0)])
    assert value_test.compare([lone]) == {}, "week 9 is outside week 1's four-week window"


def test_a_miscount_either_way_is_reported():
    players = [tested("RB", 10.0, [(1, 5.0)], counted=3, played=2), tested("WR", 10.0, [(1, 5.0)], counted=2, played=2),
               tested("TE", 10.0, [(1, 5.0)], counted=1, played=2), tested("QB", 10.0, [(1, 5.0)])]
    more, fewer, known = value_test.miscounted(players)
    assert [p["position"] for p in more] == ["RB"] and [p["position"] for p in fewer] == ["TE"] and known == 3
    text = value_test.report(6, players)
    assert "1 of 3 players" in text and "reads as a zero" in text


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
