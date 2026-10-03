"""Tests for claim pricing and projection accuracy. Run: python3 test_pricing.py"""
import sys
import tempfile
from pathlib import Path

import accuracy
import claims
import waivers

CHEAP = [0, 0, 0, 0, 1, 1, 2, 3, 5, 12]        # ten claims, most of them free
RICH = [4, 6, 9, 12, 15, 18, 22, 28, 35, 47]


def test_percentile_reads_the_sample():
    assert waivers.percentile([0, 10], 0.5) == 5 and waivers.percentile([0, 10], 0.9) == 9
    assert waivers.percentile([], 0.5) == 0


def test_budget_shares_until_the_league_has_priced_enough_claims():
    starter = {"gain": 4.0, "season_gain": 60}
    assert waivers.suggest_bid(starter, 100, 0, CHEAP[:5]) == (20, 35), "five claims isn't a market"
    assert len(CHEAP) == waivers.MIN_CLAIMS


def test_a_cheap_league_stops_the_report_overbidding():
    starter = {"gain": 4.0, "season_gain": 60}
    low, high = waivers.suggest_bid(starter, 100, 0, CHEAP)
    assert (low, high) < (20, 35), "nobody here pays 20% of budget; don't suggest it"
    assert high <= max(CHEAP) + 1


def test_an_expensive_league_pushes_bids_up():
    starter = {"gain": 4.0, "season_gain": 60}
    assert waivers.suggest_bid(starter, 100, 0, RICH)[0] >= 28, "claims here cost real money"


def test_the_tiers_stay_in_order_and_inside_the_budget():
    tiers = [waivers.suggest_bid({"gain": 4.0, "season_gain": 60}, 100, 0, RICH),
             waivers.suggest_bid({"gain": 2.0, "season_gain": 10}, 100, 0, RICH),
             waivers.suggest_bid({"gain": 0.5, "season_gain": 0}, 100, 0, RICH)]
    assert tiers[0][0] >= tiers[1][0] >= tiers[2][0]
    assert all(low <= high for low, high in tiers)
    assert waivers.suggest_bid({"gain": 4.0, "season_gain": 60}, 6, 0, RICH)[1] <= 6, "never bid what you lack"


def test_minimum_bid_is_respected():
    assert waivers.suggest_bid({"gain": 0.0, "season_gain": 0}, 100, 2, CHEAP)[0] >= 2


def test_correlation_spots_a_projection_that_tracks_nothing():
    tracking = [(10.0, 11.0), (14.0, 13.0), (6.0, 7.0), (20.0, 18.0), (8.0, 9.5)]
    noise = [(10.0, 3.0), (10.0, 18.0), (10.0, 7.0), (10.0, 21.0), (10.0, 1.0)]
    assert accuracy.correlation(tracking) > 0.9
    assert abs(accuracy.correlation(noise) or 0) < 0.3
    assert accuracy.correlation([(1.0, 2.0)]) is None, "too small a sample to say"


def test_by_position_separates_bias_from_scatter():
    rows = [{"position": "RB", "projected": 10.0, "actual": 14.0, "error": 4.0, "week": 1, "player": "a"},
            {"position": "RB", "projected": 10.0, "actual": 6.0, "error": -4.0, "week": 2, "player": "a"},
            {"position": "K", "projected": 8.0, "actual": 9.0, "error": 1.0, "week": 1, "player": "b"}]
    summary = accuracy.by_position(rows)
    assert summary["RB"]["n"] == 2 and summary["RB"]["bias"] == 0.0, "misses both ways, no bias"
    assert summary["RB"]["average_miss"] == 4.0, "but they're four points out either way"
    assert summary["K"]["bias"] == 1.0


def test_logs_are_written_with_a_header():
    import csv as csv_module
    folder = Path(tempfile.mkdtemp())
    accuracy.write_log([{"week": 1, "player": "Someone", "position": "WR",
                         "projected": 9.0, "actual": 12.0, "error": 3.0}], folder / "accuracy.csv")
    claims.write_log([{"week": 1, "team": "Rival", "bid": 7, "player": "Someone", "processed": None}],
                     folder / "claims.csv")
    assert [row["error"] for row in csv_module.DictReader(open(folder / "accuracy.csv"))] == ["3.0"]
    assert [row["bid"] for row in csv_module.DictReader(open(folder / "claims.csv"))] == ["7"]


class FakeLeague:
    """Two finished weeks of rosters, shaped the way the roster view returns them."""
    season = 2026

    def __init__(self, rows):
        self.rows = rows
        self.asked = []

    def league(self, *views, week=None, headers=None):
        self.asked.append((views, week))
        if week is None:
            return {"scoringPeriodId": 3}
        entries = []
        for name, position, pro_team, projected, actual in self.rows[week]:
            stats = [{"seasonId": 2026, "scoringPeriodId": week, "statSourceId": source,
                      "statSplitTypeId": 1, "appliedTotal": points}
                     for source, points in ((1, projected), (0, actual))]
            entries.append({"lineupSlotId": 20, "playerPoolEntry": {"player": {
                "id": abs(hash(name)) % 9999, "fullName": name, "proTeamId": pro_team,
                "eligibleSlots": {"RB": [2, 3, 23, 20], "WR": [4, 3, 23, 20], "K": [17, 20]}[position],
                "injuryStatus": "ACTIVE", "stats": stats}}})
        return {"teams": [{"id": 1, "roster": {"entries": entries}}]}

    def bye_weeks(self):
        return {9: {2}}      # pro team 9 is off in week 2


def test_accuracy_reads_projections_from_the_weekly_roster_view():
    """Player cards keep old results but not the projections made beforehand."""
    fake = FakeLeague({1: [("Starter", "RB", 5, 14.0, 9.0), ("Scrub", "WR", 5, 2.0, 1.0)],
                       2: [("Starter", "RB", 5, 12.0, 20.0), ("Bye Guy", "WR", 9, 11.0, 0.0)]})
    week, rows = accuracy.collect(fake)
    assert week == 3 and [(views, w) for views, w in fake.asked][1:] == [(("mRoster",), 1), (("mRoster",), 2)]
    assert [row["player"] for row in rows] == ["Starter", "Starter"], "scrub too small, bye week skipped"
    assert [row["error"] for row in rows] == [-5.0, 8.0]


def test_claims_report_says_when_it_has_nothing():
    assert "No claims have processed yet" in claims.report("A League", 2, [])


def test_slope_says_what_a_projected_point_is_worth():
    calibrated = [(6.0, 6.5), (10.0, 9.0), (14.0, 14.5), (8.0, 8.0), (12.0, 12.0)]
    assert accuracy.slope(calibrated) == 1.0, "a calibrated projection is worth a point per point"
    assert accuracy.slope([(10.0, 3.0), (10.0, 18.0), (10.0, 7.0)]) is None, "no spread, nothing to measure"
    assert accuracy.slope([(1.0, 2.0)]) is None


def test_report_prices_an_edge_instead_of_guessing_a_threshold():
    rows = [{"position": "RB", "projected": p, "actual": a, "error": a - p, "week": 1, "player": "x"}
            for p, a in [(6.0, 6.5), (10.0, 9.0), (14.0, 14.5), (8.0, 8.0), (12.0, 12.0)]]
    text = accuracy.report(3, rows, accuracy.by_position(rows))
    assert "PER PROJ PT" in text and " 1.00" in text and "SMALLEST EDGE" not in text


def test_rows_carry_the_espn_id_so_snaps_can_join_them():
    fake = FakeLeague({1: [("Starter", "RB", 5, 14.0, 9.0)], 2: [("Starter", "RB", 5, 12.0, 20.0)]})
    _, rows = accuracy.collect(fake)
    assert [row["player_id"] for row in rows] == [abs(hash("Starter")) % 9999] * 2


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
