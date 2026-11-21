"""Tests for the work-and-efficiency rate. Run: python3 test_rates.py"""
import sys
from datetime import datetime

import rates
import values
from test_values import SEASON, line

PLAYERS = "gsis_id,espn_id,display_name\n00-1,101,Back One\n00-2,102.0,Receiver Two\n00-3,,Nobody\n"
HEADER = "season,posteam,week,player_id,position," + ",".join(
    [stat for stat in rates.SCORING] + [f"{stat}_exp" for stat in rates.SCORING] + list(rates.FUMBLES))


def weekly_row(week, player_id, team, expected=None, **scored):
    """One row of ffopportunity's file: rushing yards stand in for everything."""
    values_by_column = {stat: scored.get(stat, 0) for stat in rates.SCORING}
    values_by_column.update({f"{stat}_exp": 0 for stat in rates.SCORING})
    values_by_column.update({stat: scored.get(stat, 0) for stat in rates.FUMBLES})
    if expected is not None:
        values_by_column["rush_yards_gained_exp"] = expected * 10
    return f"{SEASON},{team},{week},{player_id},RB," + ",".join(str(v) for v in values_by_column.values())


def test_points_are_half_ppr_with_lost_fumbles_counted_only_in_what_he_scored():
    row = dict.fromkeys([*rates.SCORING, *[f"{s}_exp" for s in rates.SCORING], *rates.FUMBLES], "0")
    row.update(receptions="4", rec_yards_gained="50", rec_touchdown="1", rush_fumble_lost="1",
               receptions_exp="3.5", rec_yards_gained_exp="40.0", rec_touchdown_exp="0.3", pass_yards_gained_exp="NA")
    expected, scored = rates.game_points(row)
    assert round(scored, 2) == 4 * 0.5 + 5.0 + 6 - 2
    assert round(expected, 2) == 1.75 + 4.0 + 1.8


def test_the_file_is_keyed_by_espn_id_and_knows_which_games_it_has():
    weekly = "\n".join([HEADER, weekly_row(1, "00-1", "BUF", expected=8, rush_yards_gained=100),
                        weekly_row(2, "00-1", "BUF", expected=6), weekly_row(1, "00-2", "KC", expected=5),
                        weekly_row(2, "00-9", "KC", expected=5), weekly_row(3, "00-9", "MIA")])
    found, played, latest = rates.join(PLAYERS, weekly)
    assert set(found) == {101, 102} and round(found[101][1][0], 1) == 8.0 and round(found[101][1][1], 1) == 10.0
    assert played[101] == {1, 2}, "his team's games are in for weeks 1 and 2"
    assert played[102] == {1, 2}, "KC's week 2 game is in, even though he has no row for it"
    assert latest == 3


def test_a_new_workload_moves_the_rate_faster_than_the_level():
    """A backup who takes over: three games of starter's work and starter's points."""
    games, weeks = [(1, 15.0), (2, 15.0), (3, 15.0)], {w: (15.0, 15.0) for w in (1, 2, 3)}
    total, work, beyond = rates.rate(3.0, games, weeks, set(weeks), "RB")
    level = values.level(3.0, [15.0] * 3, values.PRIOR_GAMES["RB"])
    assert total > level + 4, (total, level)
    assert round(total, 6) == round(work + beyond, 6)


def test_one_long_touchdown_on_the_same_work_barely_moves_it():
    games = [(1, 6.0), (2, 6.0), (3, 26.0)]
    weeks = {1: (6.0, 6.0), 2: (6.0, 6.0), 3: (6.0, 26.0)}
    total, _, beyond = rates.rate(6.0, games, weeks, set(weeks), "WR")
    level = values.level(6.0, [p for _, p in games], values.PRIOR_GAMES["WR"])
    assert total - 6.0 < level - 6.0, (total, level)
    assert 0 < beyond - rates.EFFICIENCY_BASE["WR"] < 2, "efficiency takes under a tenth of the extra 20"


def test_a_game_the_file_does_not_have_yet_is_left_out():
    weeks = {1: (10.0, 12.0)}
    with_it = rates.rate(5.0, [(1, 12.0), (2, 30.0)], weeks, {1}, "RB")
    without = rates.rate(5.0, [(1, 12.0)], weeks, {1}, "RB")
    assert with_it == without, "Monday night's game waits for the file"


def test_a_game_the_file_has_without_him_is_zero_work():
    weeks = {1: (10.0, 10.0)}
    counted = rates.rate(8.0, [(1, 10.0), (2, 0.4)], weeks, {1, 2}, "RB")[1]
    skipped = rates.rate(8.0, [(1, 10.0)], weeks, {1, 2}, "RB")[1]
    assert counted < skipped


def test_only_backs_and_receivers_with_a_card_and_a_row_get_a_rate():
    card = {"stats": [line(1, 12.0), line(2, 14.0)]}
    back = {"id": 101, "position": "RB", "season_projection": 85.0}
    quarterback = {"id": 103, "position": "QB", "season_projection": 300.0}
    stranger = {"id": 104, "position": "WR", "season_projection": 50.0}
    data = ({101: {1: (11.0, 12.0), 2: (13.0, 14.0)}, 103: {1: (15.0, 20.0)}}, {101: {1, 2}, 103: {1}}, 2)
    rated = rates.attach([back, quarterback, stranger], {101: card, 103: card, 104: card}, SEASON, 3, data)
    assert rated == 1 and back["rate"] > 5.0 and "rate" not in quarterback and "rate" not in stranger
    assert back["work_recent"] == 12.0 and back["work_average"] == 12.0 and back["active"] is True


def test_the_rate_stands_in_for_the_level_and_for_this_week_unless_he_is_not_playing():
    rated = {"id": 1, "position": "RB", "rate": 11.0, "level": 6.0, "projection": 7.5, "locked": False}
    copies = rates.by_rate([rated, dict(rated, projection=0.0), dict(rated, locked=True),
                            {"id": 2, "position": "QB", "level": 17.0, "projection": 18.0}])
    assert (copies[0]["level"], copies[0]["projection"]) == (11.0, 11.0)
    assert copies[1]["projection"] == 0.0, "ESPN has him at zero: a bye, out or inactive"
    assert copies[2]["projection"] == 7.5, "his game has started"
    assert (copies[3]["level"], copies[3]["projection"]) == (17.0, 18.0), "no rate, nothing changes"
    assert rated["level"] == 6.0 and rated["projection"] == 7.5, "the originals are left as they were"


def test_playing_lately_means_one_of_his_team_s_last_two_games():
    team = {5, 6, 8, 9}                                   # his team's games in the file; week 7 a bye
    assert rates.recently_played([(5, 9.0), (8, 3.0)], {}, team, 10)
    assert not rates.recently_played([(5, 9.0), (6, 8.0)], {5: (9, 9), 6: (8, 8)}, team, 10), "out weeks 8 and 9"
    assert rates.recently_played([(5, 9.0)], {9: (2.0, 1.0)}, team, 10), "a touch in the file counts"
    assert rates.recently_played([(6, 9.0)], {}, team, 9), "this week's game isn't over: weeks 6 and 8 count"
    assert rates.recently_played([], {}, set(), 2), "nothing to judge by yet"


def test_a_player_who_has_stopped_playing_keeps_his_level():
    stale = {"id": 1, "position": "RB", "rate": 9.0, "level": 4.0, "projection": 0.0, "active": False}
    copy = rates.by_rate([stale])[0]
    assert copy["level"] == 4.0, "his rate is old work"


def test_kickoffs_come_from_the_schedule():
    schedule = "game_id,gameday,gametime\n2026_01_BUF_MIA,2026-09-13,13:00\n2026_02_X_Y,,\n"
    weekly = "\n".join([HEADER.replace("season,", "season,game_id,"),
                        weekly_row(1, "00-1", "BUF", expected=8).replace(f"{SEASON},", f"{SEASON},2026_01_BUF_MIA,", 1)])
    found, _, _ = rates.join(PLAYERS, weekly, schedule)
    assert found[101][1][2] == datetime(2026, 9, 13, 13, 0)
    assert rates.join(PLAYERS, weekly)[0][101][1][2] is None, "no schedule, no kickoff"


def test_scored_after_counts_games_that_kicked_off_after_the_run():
    weeks = {4: (5.0, 7.0, datetime(2026, 10, 4, 13)), 5: (5.0, 8.0, datetime(2026, 10, 8, 20, 15)),
             6: (5.0, 9.0, datetime(2026, 10, 18, 13)), 9: (5.0, 99.0, datetime(2026, 11, 8, 13))}
    assert rates.scored_after(weeks, 5, "2026-10-07T06:07:00", 4) == [8.0, 9.0], "Wednesday: all of week 5 is ahead"
    assert rates.scored_after(weeks, 5, "2026-10-09T06:07:00", 4) == [9.0], "Friday: Thursday night's game is over"
    assert 99.0 not in rates.scored_after(weeks, 5, "2026-10-07T06:07:00", 4), "week 9 is past the window"


def test_without_kickoffs_a_late_run_skips_its_own_week():
    weeks = {5: (5.0, 8.0), 6: (5.0, 9.0)}
    for stamp, expected in (("2026-10-06T06:07:00", [8.0, 9.0]), ("2026-10-08T06:07:00", [8.0, 9.0]),
                            ("2026-10-08T21:30:00", [9.0]), ("2026-10-10T14:51:05", [9.0]),
                            ("2026-10-12T06:07:00", [9.0])):
        assert rates.scored_after(weeks, 5, stamp, 4) == expected, stamp


def test_the_weights_are_the_ones_rate_test_chose():
    assert rates.OPPORTUNITY_GAMES == {"RB": 2, "WR": 2} and rates.EFFICIENCY_GAMES == {"RB": 16, "WR": 32}


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
