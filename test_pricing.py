"""Tests for claim pricing and projection accuracy. Run: python3 test_pricing.py"""
import csv
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

import accuracy
import auctions
import bids
import claims
import waivers

CHEAP = [0, 0, 0, 0, 1, 1, 2, 3, 5, 12]        # ten claims, most of them free
RICH = [4, 6, 9, 12, 15, 18, 22, 28, 35, 47]


def test_percentile_reads_the_sample():
    assert waivers.percentile([0, 10], 0.5) == 5 and waivers.percentile([0, 10], 0.9) == 9
    assert waivers.percentile([], 0.5) == 0


STARTER = {"position": "WR", "gain": 4.0, "season_gain": 60}
STASH = {"position": "WR", "gain": 0.5, "season_gain": 0}
BREAKOUT = {"week": 5, "points": 21.0, "before": 4.0}


def priced(other=(), in_demand=(), streamed=()):
    return {bids.OTHER: list(other), bids.IN_DEMAND: list(in_demand), bids.STREAMED: list(streamed)}


def test_a_winning_bid_is_the_smallest_that_beat_that_share():
    paid = [0, 2, 3, 3, 5, 6, 6, 6, 7, 7, 8, 11, 11, 14, 17, 19, 28, 30, 33, 37]   # breakout claims here, weeks 1-10
    assert [waivers.winning_bid(paid, share) for share in (0.50, 0.75, 0.90)] == [8, 18, 31]
    assert sum(price < 18 for price in paid) == 15 and sum(price < 17 for price in paid) == 14, "15 of 20 is three in four"
    assert waivers.winning_bid([0, 0, 0, 0], 0.50) == 1, "matching a bid of nothing isn't beating it"
    assert waivers.winning_bid([5, 1, 3], 0.90) == 6, "order doesn't matter, and nine in ten of three is all three"


def test_budget_shares_until_the_league_has_priced_enough_claims_of_his_kind():
    assert waivers.suggest_bid(STARTER, 100, 0, priced(other=CHEAP[:5])) == (20, 35), "five claims isn't a market"
    assert waivers.suggest_bid(STARTER, 100, 0, priced(in_demand=RICH, streamed=RICH)) == (20, 35), \
        "what other kinds of claim cost says nothing about his"
    assert waivers.suggest_bid(STARTER, 100, 0) == (20, 35), "and with no history at all"
    assert len(CHEAP) == waivers.MIN_CLAIMS


def test_a_claim_is_priced_off_past_claims_of_its_own_kind():
    past = priced(other=CHEAP, in_demand=RICH, streamed=[0, 0, 0, 0, 1, 1, 2, 2, 3, 4])
    assert waivers.suggest_bid(STARTER, 100, 0, past) == (4, 6), "nobody here pays 20% of budget for him"
    assert waivers.suggest_bid(dict(STARTER, breakout=BREAKOUT), 100, 0, past) == (29, 36), "but they do after a big game"
    assert waivers.suggest_bid(dict(STARTER, role={"touches": 19}), 100, 0, past) == (29, 36), "or for a next man up"
    kicker = dict(STARTER, position="K")
    assert waivers.suggest_bid(kicker, 100, 0, past) == (2, 3), "a kicker costs what kickers cost, at stash odds"
    assert waivers.suggest_bid(dict(kicker, breakout=BREAKOUT), 100, 0, past) == (2, 3), "whatever else is on him"


def test_a_starter_is_bid_to_win_three_in_four_and_a_stash_one_in_two():
    past = priced(other=CHEAP, in_demand=RICH)
    for candidate, sample in ((STARTER, CHEAP), (dict(STARTER, breakout=BREAKOUT), RICH)):
        low, high = waivers.suggest_bid(candidate, 100, 0, past)
        assert sum(price < low for price in sample) >= 0.75 * len(sample) > sum(price < low - 1 for price in sample)
        assert sum(price < high for price in sample) >= 0.90 * len(sample)
    assert waivers.suggest_bid(STASH, 100, 0, past) == (2, 4)
    low, high = waivers.suggest_bid(dict(STASH, breakout=BREAKOUT), 100, 0, past)
    assert (low, high) == (16, 29) and sum(price < low for price in RICH) == 5, "half of ten"
    useful = {"position": "WR", "gain": 2.0, "season_gain": 10}
    assert waivers.suggest_bid(useful, 100, 0, past) == waivers.suggest_bid(STARTER, 100, 0, past), \
        "anyone who would start for you is bid at the same odds"


def test_bids_stay_inside_the_budget_and_above_the_minimum():
    past = priced(other=CHEAP, in_demand=RICH)
    assert waivers.suggest_bid(dict(STARTER, breakout=BREAKOUT), 20, 0, past) == (20, 20), "never bid what you lack"
    assert waivers.suggest_bid(STASH, 100, 5, past) == (5, 5)
    assert waivers.suggest_bid({"gain": 0.0, "season_gain": 0}, 100, 2, past)[0] >= 2


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


RUN = datetime(2026, 10, 14, 3, 0)     # a Wednesday claims run


def bid(team, status, amount, player=7, when=RUN, week=6):
    return {"week": week, "team_id": team, "status": status, "bid": amount, "player_id": player,
            "processed": when}


def test_one_player_on_one_night_is_one_auction():
    """Germie Bernard on Oct 14: six teams, $33 won, $18 was the best live bid that lost."""
    field = [bid(1, "EXECUTED", 33), bid(2, bids.OUTBID, 18), bid(3, bids.OUTBID, 16),
             bid(13, bids.OUTBID, 4), bid(4, "FAILED_PLAYERALREADYDROPPED", 40),
             bid(5, "FAILED_ROSTERLIMIT", 3), bid(6, "CANCELED", 0), bid(8, "PENDING", 9, when=None)]
    (auction,) = auctions.auctions(field, me=13)
    assert auction["price"] == 33 and auction["winner"] == 1 and auction["your_bid"] == 4
    assert auction["teams"] == 6, "every team that claimed him counts; cancelled and pending claims don't"
    assert auction["runner_up"] == 18, "a claim that failed on its own terms never competed, however big"


def test_the_same_player_on_two_nights_is_two_auctions():
    first_night = [bid(1, "FAILED_ROSTERLIMIT", 5), bid(2, "FAILED_ROSTERLIMIT", 2)]
    second_night = [bid(3, "EXECUTED", 1, when=RUN + timedelta(days=1))]
    first, second = auctions.auctions(first_night + second_night, me=13)
    assert first["price"] is None and first["winner"] is None and first["teams"] == 2
    assert second["price"] == 1 and second["teams"] == 1 and second["your_bid"] is None


def look(hours_before, change="5.0", bid_range=""):
    return {"logged_at": RUN - timedelta(hours=hours_before), "percent_owned": "20.0",
            "percent_change": change, "projection": "9.1", "band": "2", "bid": bid_range}


def test_an_auction_meets_the_reports_last_look_before_that_run():
    (auction,) = auctions.auctions([bid(1, "EXECUTED", 12)], me=13)
    two_days_old, night_before, that_morning, afterwards = look(45), look(27), look(21, "18.4", "1-3"), look(-3)
    rows = [two_days_old, night_before, that_morning, afterwards]
    assert auctions.snapshot(auction, {7: rows}) is that_morning, "the last look before the run"
    assert auctions.snapshot(auction, {7: [two_days_old, afterwards]}) is None, "an older report isn't this run's"
    assert auctions.snapshot(auction, {}) is None


def test_prices_are_grouped_by_how_many_teams_bid():
    three = [bid(1, "EXECUTED", 11, player=5), bid(2, bids.OUTBID, 0, player=5), bid(3, bids.OUTBID, 0, player=5)]
    six = [bid(t, bids.OUTBID, 10 + t, player=4) for t in range(2, 7)] + [bid(1, "EXECUTED", 37, player=4)]
    found = auctions.auctions([bid(1, "EXECUTED", 0, player=1), bid(1, "EXECUTED", 2, player=2),
                               bid(1, "EXECUTED", 9, player=3), bid(2, bids.OUTBID, 5, player=3)] + three + six,
                              me=13)
    assert auctions.by_teams(found) == [("1", 2, 1.0, 2), ("2", 1, 9, 9), ("3-4", 1, 11, 11), ("5 or more", 1, 37, 37)]


def test_auction_log_is_written_with_a_header():
    path = Path(tempfile.mkdtemp()) / "auctions.csv"
    (auction,) = auctions.auctions([bid(1, "EXECUTED", 12)], me=13)
    auction.update(player="Someone", position="WR", winner="Rival", snapshot=None)
    assert auctions.write_log([auction], path) == 1
    row = next(csv.DictReader(open(path)))
    assert row["price"] == "12" and row["winner"] == "Rival" and row["snapshot_at"] == "" and row["your_bid"] == ""


class FakeAuctions:
    """mTransactions2 and player cards, shaped the way ESPN returned them to bid_probe.py."""
    season = 2026

    def __init__(self, transactions):
        self.transactions = transactions

    def league(self, *views, week=None, headers=None):
        if "mTransactions2" in views:
            return {"transactions": self.transactions.get(week, [])}
        return {"scoringPeriodId": 6, "settings": {"name": "Test League"},
                "teams": [{"id": 13, "name": "Champion Of Obamacare"}, {"id": 4, "name": "Rival"}]}

    def my_team(self, teams):
        return teams[0]

    def player_cards(self, ids, week):
        return {99: {"id": 99, "fullName": "Germie Bernard", "eligibleSlots": [4, 3, 23, 20]}}


def test_auctions_end_to_end_with_the_reports_snapshot():
    stamp = int(RUN.timestamp() * 1000)
    def claim(tid, team, status, amount):
        return {"id": tid, "teamId": team, "status": status, "bidAmount": amount, "processDate": stamp,
                "items": [{"type": "ADD", "playerId": 99}, {"type": "DROP", "playerId": 1}]}
    won, mine = claim("a", 4, "EXECUTED", 33), claim("b", 13, bids.OUTBID, 4)
    espn = FakeAuctions({5: [won], 6: [won, mine, claim("c", 4, "CANCELED", 0)]})   # ESPN repeats some
    pool_path = Path(tempfile.mkdtemp()) / "pool.csv"
    with open(pool_path, "w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=waivers.POOL_LOG_FIELDS)
        writer.writeheader()
        writer.writerow({"logged_at": (RUN - timedelta(hours=21)).isoformat(timespec="minutes"), "week": 6,
                         "player_id": 99, "name": "Germie Bernard", "position": "WR", "percent_owned": 9.5,
                         "percent_change": 18.4, "projection": 6.2, "band": 2, "bid": "1-3"})
    name, week, found = auctions.collect(espn, pool_path)
    assert len(found) == 1 and found[0]["teams"] == 2 and found[0]["winner"] == "Rival"
    text = auctions.report(name, week, found, RUN + timedelta(days=1))
    for piece in ("Germie Bernard", "WR", "$33", "$4", "+18.4", "$1-3", "Auctions someone won: 1",
                  "of the 1 claims it had priced the morning", "would have won 0 and the high end 0"):
        assert piece in text, piece
    assert found[0]["to_beat"] == 33
    log = Path(tempfile.mkdtemp()) / "auctions.csv"
    auctions.write_log(found, log)
    row = next(csv.DictReader(open(log)))
    assert row["report_bid"] == "1-3" and row["percent_change"] == "18.4" and row["position"] == "WR"
    assert row["to_beat"] == "33"


def test_the_reports_bids_are_checked_against_what_it_took_to_win():
    def priced(beat, report_bid, days_ago=2):
        return {"to_beat": beat, "processed": RUN - timedelta(days=days_ago), "snapshot": {"bid": report_bid}}
    won = [priced(14, "18-31"), priced(19, "18-31"), priced(33, "18-31"), priced(0, "3-5"), priced(5, "3-5"),
           priced(3, "3-3"),                                    # matching the price to beat isn't beating it
           priced(50, "0-1", days_ago=40),                      # too old to say anything about today's pricing
           {"to_beat": 9, "processed": RUN, "snapshot": {"bid": ""}},      # seen, not listed
           {"to_beat": 9, "processed": RUN, "snapshot": None}]             # never seen
    assert auctions.bids_checked(won, RUN) == (6, 2, 3)
    assert auctions.bids_checked([], RUN) == (0, 0, 0)


def test_what_a_new_bid_had_to_beat():
    lost_to_rival = {"price": 33, "winner": 4, "runner_up": 18, "your_bid": 4}
    assert bids.to_beat(lost_to_rival, me=13) == 33, "someone else's winning bid is the one to top"
    assert bids.to_beat({"price": 10, "winner": 13, "runner_up": 1, "your_bid": 10}, me=13) == 1, \
        "your own $10 says nothing about what it took: the next bid down does (Bryce Young, week 3)"
    assert bids.to_beat({"price": 3, "winner": 13, "runner_up": None, "your_bid": 3}, me=13) == 0, "nobody else bid"


def test_a_breakout_game_is_ten_points_and_five_more_than_before():
    assert bids.breakout([(1, 3.0), (2, 4.0), (4, 11.5)], 4) == {"week": 4, "points": 11.5, "before": 3.5}
    assert bids.breakout([(1, 16.4)], 1) == {"week": 1, "points": 16.4, "before": 0.0}, "a first game counts from nothing"
    assert bids.breakout([(1, 3.0), (2, 4.0), (4, 9.9)], 4) is None, "under ten is a decent game, not a breakout"
    assert bids.breakout([(1, 5.0), (2, 10.0)], 2) == {"week": 2, "points": 10.0, "before": 5.0}, "on both lines counts"
    assert bids.breakout([(1, 12.0), (2, 14.0), (4, 17.0)], 4) is None, "seventeen from a man who scores thirteen"
    assert bids.breakout([(1, 3.0), (2, 21.0)], 4) is None, "he didn't play that week"
    assert bids.breakout([(1, 3.0), (2, 4.0), (4, 11.5), (5, 2.0)], 4)["before"] == 3.5, "later games don't count"


def test_kinds_put_kickers_first_and_breakouts_and_next_men_up_together():
    assert bids.kind("K", True) == bids.kind("D/ST", False) == bids.STREAMED
    assert bids.kind_of({"position": "RB", "role": {"touches": 19}}) == bids.IN_DEMAND
    assert bids.kind_of({"position": "WR", "breakout": {"week": 4}}) == bids.IN_DEMAND
    assert bids.kind_of({"position": "WR"}) == bids.kind_of({"position": "TE", "role": None}) == bids.OTHER
    assert set(bids.KINDS) == {bids.STREAMED, bids.IN_DEMAND, bids.OTHER}, "every kind has a line in the report"


def game(week, points):
    return {"seasonId": 2026, "scoringPeriodId": week, "statSourceId": 0, "statSplitTypeId": 1,
            "appliedTotal": points, "stats": {"53": 4.0}}


class FakeCards:
    season = 2026
    CARDS = {1: {"id": 1, "eligibleSlots": [4, 23], "stats": [game(1, 2.0), game(2, 4.0), game(3, 19.0), game(4, 6.0)]},
             2: {"id": 2, "eligibleSlots": [0, 20], "stats": [game(1, 9.0), game(2, 8.0), game(3, 26.0)]},
             3: {"id": 3, "eligibleSlots": [17], "stats": [game(3, 14.0)]},
             4: {"id": 4, "eligibleSlots": [2, 23], "stats": [game(3, 1.0)]},
             -16001: {"id": -16001, "eligibleSlots": [16], "stats": [game(3, 20.0)]}}

    def __init__(self):
        self.asked = []

    def player_cards(self, ids, final_week):
        self.asked.append(list(ids))
        return {i: self.CARDS[i] for i in ids if i in self.CARDS}


def test_history_sorts_past_claims_by_kind_with_what_each_took_to_win():
    later = RUN + timedelta(days=14)
    field = [bid(4, "EXECUTED", 28, player=1, week=4), bid(13, bids.OUTBID, 9, player=1, week=4),   # a receiver, after 19
             bid(4, "EXECUTED", 2, player=1, when=later, week=6),                 # the same man two weeks on
             bid(4, "EXECUTED", 11, player=2, week=4),                            # a quarterback's 26 is no breakout
             bid(13, "EXECUTED", 6, player=3, week=4), bid(4, bids.OUTBID, 3, player=3, week=4),    # yours: the next bid down
             bid(4, "EXECUTED", 1, player=-16001, week=4),
             bid(4, "EXECUTED", 5, player=4, week=4),                             # a back after a quiet game
             bid(4, "FAILED_ROSTERLIMIT", 40, player=5, week=4),                  # nobody won him
             bid(4, "EXECUTED", 7, player=77, week=4)]                            # ESPN sends no card for him
    espn = FakeCards()
    past = bids.history(espn, field, me=13, cards={4: FakeCards.CARDS[4]})
    assert past == {bids.IN_DEMAND: [28], bids.OTHER: [11, 5, 2], bids.STREAMED: [3, 1]}
    assert espn.asked == [[-16001, 1, 2, 3, 77]], "a card already in hand isn't fetched again"


def test_a_free_agent_coming_off_a_breakout_game_is_tagged():
    def agent(player_id, position="WR", team=5):
        return {"id": player_id, "name": f"P{player_id}", "position": position, "pro_team": team}
    cards = {1: {"stats": [game(1, 2.0), game(2, 4.0), game(3, 19.0)]},
             2: {"stats": [game(1, 2.0), game(2, 4.0), game(3, 19.0)]},
             3: {"stats": [game(1, 2.0), game(2, 19.0)]},
             4: {"stats": [game(1, 2.0), game(2, 4.0), game(3, 19.0), game(4, 5.0)]}}
    risen, tight_end, before_bye, cooled, no_card = agent(1), agent(2, "TE"), agent(3, team=7), agent(4), agent(9)
    finished = {5: [1, 2, 3], 7: [1, 2]}                       # team 7 was off in week 3
    found = bids.mark([risen, tight_end, before_bye, cooled, no_card], cards, 2026, 4, finished)
    assert found == [risen, cooled] and risen["breakout"] == {"week": 3, "points": 19.0, "before": 3.0}
    assert "breakout" not in tight_end, "no tight end's claim has drawn a crowd here"
    assert "breakout" not in before_bye, "the league has already had a run to bid on that game"
    assert bids.describe(risen) == "P1 (19.0 points in week 3, 3.0 a game before)"
    assert bids.describe(dict(risen, role={"touches": 19})) == "P1 (next man up)"
    monday = bids.mark([agent(4)], cards, 2026, 4, {5: [1, 2, 3, 4]})    # week 4's game is over, the week isn't
    assert monday == [], "Sunday's five points replace last week's nineteen as his latest game"
    sunday_star = agent(1)
    cards[1]["stats"].append(game(4, 22.0))
    assert bids.mark([sunday_star], cards, 2026, 4, {5: [1, 2, 3, 4]})[0]["breakout"]["week"] == 4, \
        "a Monday morning sees Sunday"


ME = 13
MORNING = RUN + timedelta(hours=3, minutes=7)     # the 6:07 report after the 3am run
NAMES = {7: "Germie Bernard", 8: "Chargers D/ST", 9: "Tyler Shipley", 10: "Rashee Rice", 1: "Jets D/ST"}
TEAMS = {1: "One Week of Puka", 4: "Rival"}


def test_last_night_says_how_each_of_your_bids_went():
    field = [bid(ME, "EXECUTED", 0, player=8), bid(4, bids.OUTBID, 0, player=8),
             bid(1, "EXECUTED", 14, player=9), bid(ME, bids.OUTBID, 4, player=9), bid(4, bids.OUTBID, 6, player=9),
             bid(4, "EXECUTED", 5, player=10), bid(ME, bids.OUTBID, 5, player=10),
             bid(ME, "FAILED_ROSTERLIMIT", 2, player=7), bid(1, "EXECUTED", 3, player=7)]
    field[0]["drop_id"] = 1
    found = bids.last_night(field, ME, MORNING)
    assert [r["player_id"] for r in found] == [8, 10, 9, 7], "wins first, then the biggest bids"
    lines = [bids.result_line(r, NAMES, TEAMS) for r in found]
    assert lines[0] == ("won", "Won Chargers D/ST for $0 (next best bid $0); dropped Jets D/ST")
    assert lines[1] == ("lost", "Lost Rashee Rice on a tie at $5: Rival was ahead in waiver order")
    assert lines[2] == ("lost", "Lost Tyler Shipley to One Week of Puka: $14 against your $4")
    assert lines[3] == ("failed", "Germie Bernard: your $2 claim didn't run: your roster was full")


def test_a_win_nobody_contested():
    found = bids.last_night([bid(ME, "EXECUTED", 3, player=8)], ME, MORNING)
    assert bids.result_line(found[0], NAMES, TEAMS) == ("won", "Won Chargers D/ST for $3 (nobody else claimed him)")


def test_only_the_latest_run_and_only_within_a_day():
    older = bid(ME, "EXECUTED", 9, player=9, when=RUN - timedelta(days=1))
    latest = bid(ME, "EXECUTED", 0, player=8)
    assert [r["player_id"] for r in bids.last_night([older, latest], ME, MORNING)] == [8]
    assert bids.last_night([older, latest], ME, MORNING + timedelta(days=1)) == [], "Tuesday has no run to report"
    assert bids.last_night([bid(4, "EXECUTED", 9)], ME, MORNING) == [], "no bid of yours, nothing to say"
    assert bids.last_night([bid(ME, "PENDING", 9, when=None)], ME, MORNING) == []


def test_season_record_counts_bids_decided_on_price():
    field = [bid(ME, "EXECUTED", 3), bid(ME, bids.OUTBID, 4, player=9), bid(ME, "FAILED_ROSTERLIMIT", 2, player=10),
             bid(ME, "CANCELED", 0, player=11), bid(4, "EXECUTED", 5, player=12)]
    assert bids.season_record(field, ME) == (1, 2)


def test_all_bids_keeps_the_drop():
    stamp = int(RUN.timestamp() * 1000)
    claim = {"id": "a", "teamId": ME, "status": "EXECUTED", "bidAmount": 0, "processDate": stamp,
             "items": [{"type": "ADD", "playerId": 8}, {"type": "DROP", "playerId": 1}]}
    found = bids.all_bids(FakeAuctions({1: [claim]}), 1)
    assert found[0]["drop_id"] == 1 and found[0]["player_id"] == 8


def test_auctions_report_says_when_nothing_has_run():
    assert "No claims have processed yet" in auctions.report("League", 1, [], RUN)


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
