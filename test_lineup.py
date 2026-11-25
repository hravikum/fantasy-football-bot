"""Tests for the lineup and IR planner. Run: python test_lineup.py"""
import os
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

import requests

import lineup_test
from espn import BENCH, ESPN, IR, ESPNError
from lineup import (MIN_GAIN, decide, describe, game_time_soon, later_in_flex, plan, season_kickoffs,
                    slack_layout, would_act)

SLOTS = {0: 1, 2: 2, 4: 2, 6: 1, 16: 1, 17: 1, 20: 6, 21: 1, 23: 2}
ELIGIBLE = {"QB": [0, 7, 20, 21], "RB": [2, 3, 23, 7, 20, 21], "WR": [4, 3, 5, 23, 7, 20, 21],
            "TE": [6, 5, 23, 7, 20, 21], "D/ST": [16, 20, 21], "K": [17, 20, 21]}
GAMES = {team: (99, None) for team in range(1, 35)}  # pro team 0 stands in for a bye
SUNDAY = datetime(2026, 10, 4)
TIMED = {team: (99, SUNDAY.replace(hour=13)) for team in range(1, 35)}
TIMED[2] = (99, SUNDAY.replace(hour=16, minute=25))     # pro team 2 plays the late game
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
    # Thin bench on purpose: the only replacement is worth 0.3, under the churn threshold.
    roster = {"qb": player("QB", 0, 20.0), "rb1": player("RB", 2, 15.0),
              "rb2": player("RB", 2, 14.0, status="OUT"), "wr1": player("WR", 4, 13.0),
              "wr2": player("WR", 4, 12.0), "te": player("TE", 6, 9.0),
              "flex1": player("RB", 23, 11.0), "flex2": player("WR", 23, 10.0),
              "dst": player("D/ST", 16, 7.0), "k": player("K", 17, 8.0),
              "bench_rb": player("RB", 20, 0.3)}
    target, moves, gain, broken, _ = decide(list(roster.values()), SLOTS, GAMES)
    assert slots_of(roster, target)["rb2"] == IR, "an OUT player belongs on IR, not in the lineup"
    assert round(gain, 1) == 0.3 and gain < MIN_GAIN
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
    roster = full_roster(bench_wr=player("WR", 20, 10.3))  # +0.3 over the weakest starter
    _, moves, gain, broken, _ = decide(list(roster.values()), SLOTS, GAMES)
    assert moves and round(gain, 1) == 0.3
    assert not would_act(moves, gain, broken), "under the threshold the bot holds"


def test_a_one_point_upgrade_is_worth_making():
    """A projected point has been worth about 0.9 real ones, so this is no longer noise to sit out."""
    roster = full_roster(bench_wr=player("WR", 20, 11.0))  # +1.0 over the weakest starter
    _, moves, gain, broken, _ = decide(list(roster.values()), SLOTS, GAMES)
    assert round(gain, 1) == 1.0 and would_act(moves, gain, broken)


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
    """A projected kicker point was worth +1.04 real ones in 2025 and -1.70 in 2026: a coin flip."""
    roster = full_roster(bench_k=player("K", BENCH, 14.0))      # your kicker projects 8.0
    target, moves, _, _, _ = decide(list(roster.values()), SLOTS, GAMES)
    slots = slots_of(roster, target)
    assert slots["k"] == 17 and slots["bench_k"] == BENCH and not moves


def test_a_defense_still_follows_its_projection():
    """A projected point was worth 0.65 real ones at D/ST in 2025 and 0.73 in 2026."""
    roster = full_roster(bench_dst=player("D/ST", BENCH, 12.0))   # your defense projects 7.0
    target, moves, gain, broken, _ = decide(list(roster.values()), SLOTS, GAMES)
    slots = slots_of(roster, target)
    assert slots["bench_dst"] == 16 and slots["dst"] == BENCH
    assert would_act(moves, gain, broken)


def test_the_rest_of_the_lineup_still_optimises_around_a_steady_kicker():
    roster = full_roster(bench_k=player("K", BENCH, 14.0), bench_wr=player("WR", BENCH, 18.0))
    slots = slots_of(roster, target_for(roster))
    assert slots["k"] == 17 and slots["bench_wr"] == 4


def test_a_kicker_who_cannot_play_is_replaced():
    roster = full_roster(k=player("K", 17, 8.0, bye=True), bench_k=player("K", BENCH, 6.0))
    slots = slots_of(roster, target_for(roster))
    assert slots["bench_k"] == 17 and slots["k"] == BENCH


def test_a_defense_that_cannot_play_is_replaced():
    hurt = full_roster(dst=player("D/ST", 16, 7.0, status="OUT"), bench_dst=player("D/ST", BENCH, 4.0))
    assert slots_of(hurt, target_for(hurt))["bench_dst"] == 16


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


def late(position, slot, projection, **kwargs):
    return dict(player(position, slot, projection, **kwargs), pro_team=2)


def test_a_later_kickoff_moves_into_flex():
    roster = full_roster(rb2=late("RB", 2, 14.0))     # flex1, a running back, plays at 1:00
    players = list(roster.values())
    target, moves, gain, broken, _ = decide(players, SLOTS, TIMED)
    slots = slots_of(roster, target)
    assert slots["rb2"] == 23 and slots["flex1"] == 2, "the 4:25 back takes FLEX, the 1:00 one his RB slot"
    later = later_in_flex(players, target, TIMED)
    assert gain == 0 and later and would_act(moves, gain, broken, later), "it can't wait: 1:00 locks the swap"
    assert "late scratch" in describe(players, target, moves, gain, broken, [], TIMED, 5)


def test_a_reshuffle_that_gains_nothing_is_left_alone():
    roster = full_roster(rb2=player("RB", 23, 14.0), flex1=player("RB", 2, 11.0))   # your arrangement
    players = list(roster.values())
    target, moves, gain, broken, _ = decide(players, SLOTS, TIMED)
    later = later_in_flex(players, target, TIMED)
    assert moves and gain == 0 and not later and not would_act(moves, gain, broken, later)


def test_flex_swaps_only_happen_where_both_players_fit():
    roster = full_roster(te=late("TE", 6, 9.0), wr1=late("WR", 4, 13.0))
    slots = slots_of(roster, decide(list(roster.values()), SLOTS, TIMED)[0])
    assert slots["te"] == 6 and slots["flex1"] == 23, "a running back can't take the tight end's slot"
    assert slots["wr1"] == 23 and slots["flex2"] == 4, "but the 1:00 receiver can take the 4:25 one's"


def test_a_locked_player_is_not_moved_for_kickoff_order():
    roster = full_roster(rb2=late("RB", 2, 14.0), flex1=player("RB", 23, 11.0, locked=True))
    assert slots_of(roster, decide(list(roster.values()), SLOTS, TIMED)[0])["flex1"] == 23


def test_the_check_in_only_acts_near_a_kickoff():
    kickoffs = [SUNDAY.replace(hour=13), SUNDAY.replace(hour=16, minute=25)]
    def soon(hour, minute):
        return game_time_soon(SUNDAY.replace(hour=hour, minute=minute), kickoffs)
    assert not soon(11, 20), "inactives for the 1:00 games aren't out yet"
    assert soon(11, 25) and soon(12, 45)
    assert not soon(13, 0), "the 1:00 games have started, and 4:25 is more than 95 minutes off"
    assert soon(14, 50) and not soon(16, 25)


def test_kickoffs_come_from_a_copy_refreshed_daily():
    class Schedule:
        calls, down = 0, False
        def kickoffs(self):
            self.calls += 1
            if self.down:
                raise ESPNError("ESPN is down")
            return [SUNDAY.replace(hour=13)]
    espn, path = Schedule(), Path(tempfile.mkdtemp()) / "kickoffs.json"
    assert season_kickoffs(espn, path) == season_kickoffs(espn, path) == [SUNDAY.replace(hour=13)]
    assert espn.calls == 1, "the second check-in reads the copy"
    old = time.time() - 2 * 86400
    os.utime(path, (old, old))
    espn.down = True
    assert season_kickoffs(espn, path) == [SUNDAY.replace(hour=13)], "a stale copy beats none"
    path.unlink()
    try:
        season_kickoffs(espn, path)
        raise AssertionError("with no copy at all, the error should surface")
    except ESPNError:
        pass


def test_espn_retries_a_read_once_when_the_connection_was_dropped():
    class Response:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return {"ok": True}

    class Session:
        def __init__(self, failures):
            self.failures, self.calls = failures, 0

        def get(self, url, params=None, headers=None, timeout=None):
            self.calls += 1
            if self.calls <= self.failures:
                raise requests.exceptions.ConnectionError("Connection aborted.")
            return Response()

    espn = ESPN(1, "s2", "{swid}", season=2026)
    espn.session = Session(failures=1)
    assert espn.league("mSettings") == {"ok": True} and espn.session.calls == 2, "an idle connection ESPN closed"
    espn.session = Session(failures=2)
    try:
        espn.league("mSettings")
        raise AssertionError("a second failure in a row is a real outage and should surface")
    except requests.exceptions.ConnectionError:
        assert espn.session.calls == 2


def test_espn_lists_each_kickoff_once():
    espn = ESPN(1, "s2", "{swid}", season=2026)
    game = {"date": 1791133200000, "homeProTeamId": 1, "awayProTeamId": 2}
    espn._schedule = {"settings": {"proTeams": [{"id": 1, "proGamesByScoringPeriod": {"5": [game]}},
                                                {"id": 2, "proGamesByScoringPeriod": {"5": [game]}},
                                                {"id": 3, "proGamesByScoringPeriod": {"5": []}}]}}
    assert espn.kickoffs() == [datetime.fromtimestamp(1791133200)], "both teams list the same game"


def replayed(position, slot, projection, actual, level=None, week=3, team=1, mine=False):
    """A roster row as lineup_test.collect builds it: who he was, where he was put, and how the week went."""
    return dict(player(position, slot, projection or 0.0), projection=projection, actual=actual, level=level,
                week=week, team_id=team, mine=mine)


def replay_roster(**overrides):
    """Everyone scores his projection, so a lineup's points say exactly who was started."""
    roster = {name: dict(p, actual=p["projection"], level=None, week=3, team_id=1, mine=False)
              for name, p in full_roster().items()}
    roster.update(overrides)
    return roster


def test_a_replayed_lineup_is_scored_on_what_its_starters_did():
    roster = replay_roster(wr2=replayed("WR", 4, 12.0, actual=2.0), bench_wr=replayed("WR", 20, 3.0, actual=25.0))
    points, starters = lineup_test.best_lineup(list(roster.values()), SLOTS, "espn")
    assert roster["wr2"]["id"] in starters and roster["bench_wr"]["id"] not in starters, "ESPN had no way to know"
    assert points == 119.0 - 12.0 + 2.0, "and the lineup is charged the two he scored, not the twelve projected"
    points, starters = lineup_test.best_lineup(list(roster.values()), SLOTS, "hindsight")
    assert roster["bench_wr"]["id"] in starters and roster["wr2"]["id"] not in starters and points == 119.0 - 12.0 + 25.0, \
        "hindsight starts the 25 and sits the 2"


def test_level_and_half_rank_a_receiver_by_his_level_and_a_tight_end_by_espn():
    risen = replayed("WR", 20, 9.0, actual=9.0, level=16.0)        # ESPN has him below the flex; his level doesn't
    faded = replayed("WR", 23, 10.0, actual=10.0, level=4.0)
    tight_end = replayed("TE", 20, 9.5, actual=9.5)                # no level: ESPN's number whatever the method
    roster = replay_roster(flex2=faded, bench_wr=risen, bench_rb=tight_end)
    for name, row in roster.items():
        if row["position"] in ("QB", "RB", "WR") and row["level"] is None:
            row["level"] = row["projection"]
    started = {method: lineup_test.best_lineup(list(roster.values()), SLOTS, method)[1] for method in lineup_test.METHODS}
    assert faded["id"] in started["espn"] and risen["id"] not in started["espn"]
    assert risen["id"] in started["level"] and faded["id"] not in started["level"]
    assert risen["id"] in started["half"], "12.5 against 7"
    assert roster["te"]["id"] not in started["level"] and tight_end["id"] in started["level"], \
        "the 9.5 tight end starts over the 9.0 one in every method"
    assert lineup_test.worth(risen, "half") == 12.5 and lineup_test.worth(tight_end, "level") == 9.5


def test_a_player_espn_projected_for_nothing_is_unavailable_to_every_method_but_hindsight():
    out = replayed("RB", 20, 0.0, actual=0.0, level=22.0)          # hurt: a level doesn't know, ESPN's zero does
    no_number = replayed("RB", 20, None, actual=None, level=19.0)
    roster = replay_roster(bench_rb=out, bench_wr=no_number)
    for method in ("espn", "level", "half"):
        starters = lineup_test.best_lineup(list(roster.values()), SLOTS, method)[1]
        assert out["id"] not in starters and no_number["id"] not in starters, method
    assert lineup_test.worth(out, "level") == 0.0 and not lineup_test.available(no_number)
    surprise = replayed("RB", 20, 0.0, actual=30.0, level=5.0)
    assert surprise["id"] in lineup_test.best_lineup(list(replay_roster(bench_rb=surprise).values()), SLOTS, "hindsight")[1]


def test_as_set_is_whoever_was_in_a_starting_slot():
    roster = replay_roster(bench_wr=replayed("WR", 20, 18.0, actual=18.0), bench_rb=replayed("RB", 21, 30.0, actual=30.0))
    (result,) = lineup_test.score(list(roster.values()), SLOTS)
    assert result["as_set"][0] == 119.0, "the 18 on the bench and the 30 on IR don't count"
    assert result["espn"][0] == 119.0 + 18.0 + 30.0 - 11.0 - 10.0, "ESPN's lineup starts both, over the two weakest"
    assert result["week"] == 3 and result["team_id"] == 1 and not result["mine"]


def test_teams_and_weeks_are_scored_apart():
    rows = []
    for week, team, mine in ((3, 1, True), (3, 2, False), (4, 1, True)):
        rows += [dict(row, week=week, team_id=team, mine=mine) for row in replay_roster().values()]
    results = lineup_test.score(rows, SLOTS)
    assert [(r["week"], r["team_id"], r["mine"]) for r in results] == [(3, 1, True), (3, 2, False), (4, 1, True)]
    assert all(r["espn"][0] == r["as_set"][0] == 119.0 for r in results)


def test_the_gap_to_espn_comes_with_its_standard_error():
    def result(espn, level, same_starters=False):
        return {"espn": (espn, frozenset({1})), "level": (level, frozenset({1} if same_starters else {2}))}
    results = [result(100.0, 104.0), result(100.0, 96.0), result(100.0, 106.0), result(100.0, 100.0, same_starters=True)]
    points, gap, error, differed = lineup_test.against_espn(results, "level")
    assert points == 101.5 and gap == 1.5 and differed == 3
    assert lineup_test.against_espn(results + [result(90.0, 90.0)], "level")[3] == 4, \
        "a different lineup that happened to score the same still differed"
    assert round(error, 2) == 2.22, "four weeks of +4, -4, +6, 0: nothing to see"
    assert lineup_test.against_espn(results[:1], "level")[2] is None, "one week has no spread"


class FakeSeason:
    """Two finished weeks of one two-man roster, shaped like ESPN's roster view and player cards."""
    season = 2026

    def __init__(self, scoring_period=3, final=17, owner=True, roster_has_season=True):
        self.scoring_period, self.final, self.owner, self.roster_has_season = scoring_period, final, owner, roster_has_season
        self.asked = []

    def _stat(self, week, source, points):
        return {"seasonId": 2026, "scoringPeriodId": week, "statSourceId": source, "statSplitTypeId": 1,
                "appliedTotal": points, "stats": {"53": 1.0}}

    def league(self, *views, week=None, headers=None):
        if "mRoster" in views:
            self.asked.append(week)
            season = [self._stat(0, 1, 170.0)] if self.roster_has_season else []
            def entry(pid, slots, slot, projection, actual):
                return {"lineupSlotId": slot, "playerPoolEntry": {"player": {
                    "id": pid, "fullName": f"P{pid}", "eligibleSlots": slots, "proTeamId": 1,
                    "stats": season + [self._stat(week, 1, projection), self._stat(week, 0, actual)]}}}
            return {"teams": [{"id": 7, "roster": {"entries": [entry(1, ELIGIBLE["WR"], 4, 11.0, 20.0),
                                                               entry(2, ELIGIBLE["TE"], 20, 6.0, 3.0)]}}]}
        return {"scoringPeriodId": self.scoring_period, "status": {"finalScoringPeriod": self.final},
                "settings": {"rosterSettings": {"lineupSlotCounts": {str(k): v for k, v in SLOTS.items()}}},
                "teams": [{"id": 7}]}

    def my_team(self, teams):
        if not self.owner:
            raise ESPNError("Expected exactly one team owned by your SWID, found 0.")
        return teams[0]

    def player_cards(self, ids, final_week):
        self.cards_asked = list(ids)
        return {1: {"id": 1, "stats": [self._stat(0, 1, 170.0), self._stat(1, 0, 20.0), self._stat(2, 0, 20.0)]}}


def test_the_replay_gives_each_week_the_level_he_had_going_into_it():
    espn = FakeSeason()
    last, slot_counts, rows = lineup_test.collect(espn)
    assert last == 2 and espn.asked == [1, 2] and slot_counts == SLOTS
    assert espn.cards_asked == [1], "only positions that have a level need a card"
    receiver = {row["week"]: row for row in rows if row["id"] == 1}
    assert receiver[1]["level"] == 10.0, "before his first game it is ESPN's 170 over 17"
    assert receiver[2]["level"] == lineup_test.values.level(10.0, [20.0], 16) < 11.0, "one game in, and not the second"
    assert all(row["level"] is None for row in rows if row["id"] == 2), "a tight end has none"
    assert all(row["mine"] and row["team_id"] == 7 for row in rows)
    assert receiver[1]["slot"] == 4 and receiver[1]["projection"] == 11.0 and receiver[1]["actual"] == 20.0


def test_the_replay_stops_at_the_last_week_of_the_fantasy_season_and_runs_without_your_team():
    last, _, rows = lineup_test.collect(FakeSeason(scoring_period=19, final=2, owner=False))
    assert last == 2 and {row["week"] for row in rows} == {1, 2}, "week 18's abandoned lineups aren't decisions"
    assert not any(row["mine"] for row in rows), "a season you weren't in still compares the methods"
    _, _, rows = lineup_test.collect(FakeSeason(roster_has_season=False))
    assert [row["level"] for row in rows if row["id"] == 1][0] == 10.0, "the card has the season projection too"


def test_the_replay_report_shows_the_league_then_your_team_and_logs_every_row():
    import csv
    espn = FakeSeason()
    last, slot_counts, rows = lineup_test.collect(espn)
    text = lineup_test.report(last, lineup_test.score(rows, slot_counts))
    assert "Every team (1 teams, 2 team-weeks):" in text and "Your team (2 weeks):" in text
    league = text[text.index("Every team"):text.index("Your team")]
    for label in ("As set", "By ESPN's weekly projection", "By level (QB, RB, WR)", "Half ESPN, half level", "With hindsight"):
        assert label in league, label
    by_week = [line.split() for line in text.splitlines() if line[:1] in "12" and len(line.split()) == 6]
    assert by_week == [["1", "20.0", "23.0", "23.0", "23.0", "23.0"], ["2", "20.0", "23.0", "23.0", "23.0", "23.0"]], \
        "the manager left the tight end on the bench both weeks; every method starts him"
    assert "Your team" not in lineup_test.report(last, [dict(r, mine=False) for r in lineup_test.score(rows, slot_counts)])
    assert "Nothing has finished yet" in lineup_test.report(0, [])
    path = Path(tempfile.mkdtemp()) / "logs" / "lineups.csv"
    assert lineup_test.write_log(rows, path) == 4
    logged = list(csv.DictReader(open(path)))
    assert logged[0] == {"week": "1", "team_id": "7", "mine": "1", "player_id": "1", "player": "P1", "position": "WR",
                         "slot": "4", "projected": "11.0", "actual": "20.0", "level": "10.0"}
    assert logged[1]["position"] == "TE" and logged[1]["level"] == ""
    assert logged[2]["week"] == "2" and logged[2]["level"] == str(round(lineup_test.values.level(10.0, [20.0], 16), 2)), \
        "two decimals is plenty for a level"


def _slack_texts(blocks):
    return [b["text"]["text"] if "text" in b else b["elements"][0]["text"] for b in blocks if b["type"] != "divider"]


def test_slack_shows_each_move_on_a_line_and_that_espn_confirmed_them():
    roster = full_roster(bench_wr=player("WR", 20, 18.0, status="QUESTIONABLE"))
    players = list(roster.values())
    target, moves, gain, broken, blocked = decide(players, SLOTS, GAMES)
    texts = _slack_texts(slack_layout("Week 5: lineup updated", players, target, moves, gain, broken, blocked, GAMES,
                                      ["Applied and confirmed on ESPN: 3 move(s)."]))
    assert texts[0] == "Week 5: lineup updated"
    assert texts[1].startswith("Best legal lineup projects ") and f"({gain:+.1f} on the current" in texts[1]
    moved = texts[2].split("\n")
    assert len(moved) == len(moves) and f"*{roster['bench_wr']['name']}*   BE → WR   18.0   _questionable_" in moved
    assert texts[-1] == ":white_check_mark: Applied and confirmed on ESPN: 3 move(s)."
    assert any(t.startswith("Watch: " + roster["bench_wr"]["name"]) for t in texts)


def test_slack_puts_what_needs_you_up_front():
    roster = full_roster(hurt=player("RB", IR, 1.0, status="ACTIVE"))
    for i in range(4):
        roster[f"filler{i}"] = player("WR", 20, 1.0)
    players = list(roster.values())
    target, moves, gain, broken, blocked = decide(players, SLOTS, GAMES)
    texts = _slack_texts(slack_layout("Week 5: lineup needs you", players, target, moves, gain, broken, blocked,
                                      GAMES, ["ESPN refused the change (409 X): no", "Nothing was applied."], True))
    assert any(t.startswith(f":warning: *{roster['hurt']['name']}* is off IR") for t in texts)
    assert texts[-1] == ":warning: ESPN refused the change (409 X): no Nothing was applied."


def test_slack_says_so_when_there_is_nothing_to_move():
    players = list(full_roster().values())
    target, moves, gain, broken, blocked = decide(players, SLOTS, GAMES)
    assert "Already the best legal lineup. No moves." in _slack_texts(
        slack_layout("Week 5: roster needs you", players, target, moves, gain, broken, blocked, GAMES))


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
