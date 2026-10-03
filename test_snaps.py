"""Tests for snap share. Run: python3 test_snaps.py"""
import os
import sys
import tempfile
import time
from pathlib import Path

import requests

import jev
import snap_test
import snaps

PLAYERS = "display_name,espn_id,pfr_id\nNico Collins,4258173,CollNi00\nFloat Id,4047650.0,FlotId00\nNo Espn,,NoEsNo00\n"
SNAPS = ("season,game_type,week,player,pfr_player_id,offense_pct\n"
         "2026,REG,1,Nico Collins,CollNi00,0.72\n"
         "2026,REG,1,Float Id,FlotId00,0.40\n"
         "2026,REG,2,Float Id,FlotId00,0.78\n"
         "2026,REG,2,No Espn,NoEsNo00,0.90\n"
         "2026,PRE,3,Float Id,FlotId00,0.10\n")


def test_join_maps_nflverse_players_to_espn_ids():
    shares, latest = snaps.join(PLAYERS, SNAPS)
    assert latest == 2, "preseason rows don't count"
    assert shares[4258173] == {1: 72}
    assert shares[4047650] == {1: 40, 2: 78}, "an id written as 4047650.0 still matches"
    assert len(shares) == 2, "a player with no ESPN id can't be matched, so he's left out"


def test_a_missed_game_shows_but_does_not_count_as_a_shrinking_role():
    """Nico Collins, week 2: no snap row at all, because he didn't play."""
    nico = snaps.summary({1: 72}, 2)
    assert nico["by_week"] == [72, None] and nico["missed_last_game"]
    assert nico["trend"] == "one game so far" and snaps.label(nico) == "72→out"


def test_trend_needs_a_real_change():
    assert snaps.summary({1: 40, 2: 78}, 2)["trend"] == "rising"
    assert snaps.summary({1: 80, 2: 55}, 2)["trend"] == "falling"
    assert snaps.summary({1: 70, 2: 76}, 2)["trend"] == "steady"
    assert snaps.summary({1: 30, 2: 35, 3: 90}, 3)["trend"] == "rising", "latest against the average before it"


def test_labels_fit_a_table_cell():
    assert snaps.label(snaps.summary({1: 40, 2: 78}, 2)) == "40→78"
    assert snaps.label(snaps.summary({2: 65}, 2)) == "out→65"
    assert snaps.label(snaps.summary({1: 65}, 1)) == "65"
    assert snaps.label(None) == "-" and snaps.summary({}, 2) is None


def test_a_fresh_cache_means_no_download():
    folder = Path(tempfile.mkdtemp())
    (folder / "file.csv").write_text("cached")
    saved = requests.get
    requests.get = lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not hit the network"))
    try:
        assert snaps._fetch("https://example.com/file.csv", folder / "file.csv", 3600) == "cached"
    finally:
        requests.get = saved


def test_a_failed_download_falls_back_to_the_stale_copy():
    folder = Path(tempfile.mkdtemp())
    stale = folder / "file.csv"
    stale.write_text("yesterday")
    old = time.time() - 10 * 86400
    os.utime(stale, (old, old))
    saved = requests.get
    def offline(*args, **kwargs):
        raise requests.exceptions.ConnectionError("no network")
    requests.get = offline
    try:
        assert snaps._fetch("https://example.com/file.csv", stale, 3600) == "yesterday"
        try:
            snaps._fetch("https://example.com/other.csv", folder / "other.csv", 3600)
            raise AssertionError("with nothing cached, the failure should surface")
        except requests.exceptions.ConnectionError:
            pass
    finally:
        requests.get = saved


def test_jev_sees_snap_share_when_there_is_some():
    candidate = {"name": "X", "position": "WR", "pro_team": 12, "injury_status": "ACTIVE",
                 "percent_owned": 20.0, "percent_change": 3.0, "projection": 9.0, "season_projection": 140.0,
                 "snaps": snaps.summary({1: 40, 2: 78}, 2)}
    state = jev.player_state(candidate, {}, {})
    assert state["player"]["snap_share"] == {"percent_by_week": [40, 78], "trend": "rising"}
    del candidate["snaps"]
    assert "snap_share" not in jev.player_state(candidate, {}, {})["player"]


def test_backtest_labels_each_week_with_the_trend_before_it():
    shares = {7: {1: 40, 2: 80, 3: 20}}
    rows = [{"week": week, "player_id": 7, "position": "WR", "error": 0.0} for week in (2, 3, 4)]
    rows.append({"week": 3, "player_id": 8, "position": "QB", "error": 0.0})
    labelled = snap_test.label(rows, shares)
    assert [row["trend"] for row in labelled] == [None, "rising", "falling"], \
        "week 3 sees 40 then 80 and not its own 20; week 2 has only one game behind it"
    assert all(row["position"] != "QB" for row in labelled), "a quarterback's snap share says nothing"


def test_backtest_compares_rising_with_steady_and_says_how_sure_it_is():
    rows = ([{"position": "WR", "trend": "rising", "error": e} for e in (3.0, 5.0, 4.0, 6.0)] +
            [{"position": "WR", "trend": "steady", "error": e} for e in (-1.0, 1.0, 0.0, 0.0)] +
            [{"position": "RB", "trend": None, "error": 9.0}])
    stats = snap_test.by_trend(rows)
    n, mean, error = stats[("WR", "rising")]
    assert n == 4 and mean == 4.5 and round(error, 2) == 0.65
    assert stats[("ALL", "steady")][0] == 4 and ("RB", None) not in stats, "no trend, no group"
    text = snap_test.report(2025, rows, stats)
    assert "8 of 9 player-weeks" in text and "+4.5 ±0.8" in text and "signal rather than luck" in text


def test_a_small_noisy_gap_is_called_luck():
    rows = ([{"position": "RB", "trend": "rising", "error": e} for e in (8.0, -6.0)] +
            [{"position": "RB", "trend": "steady", "error": e} for e in (1.0, -1.0)])
    assert "could be luck" in snap_test.report(2025, rows, snap_test.by_trend(rows))
    assert "Nothing to compare yet" in snap_test.report(2026, [], {})


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
