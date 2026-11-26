"""Tests for the shadow comparison. Run: python3 test_shadow.py"""
import contextlib
import io
import sys
import tempfile
from datetime import datetime
from pathlib import Path

import rates
import shadow
import waivers
from espn import BENCH
from test_lineup import GAMES, SLOTS, full_roster, player
from test_values import SEASON, line
from test_waivers import _sample_context, free_agent

WEEK = 5
SETTINGS = {"settings": {"scheduleSettings": {"matchupPeriodCount": 14,
                                              "matchupPeriods": {str(w): [w] for w in range(1, 18)}}}}


def entry(person):
    """A roster entry the way ESPN sends one, for parse_roster_entry."""
    stats = [{"seasonId": SEASON, "scoringPeriodId": WEEK, "statSourceId": 1, "appliedTotal": person["projection"]},
             {"seasonId": SEASON, "scoringPeriodId": 0, "statSourceId": 1, "appliedTotal": person["season_projection"]}]
    return {"lineupSlotId": person["slot"], "playerPoolEntry": {"lineupLocked": False, "player": {
        "id": person["id"], "fullName": person["name"], "eligibleSlots": person["eligible_slots"],
        "proTeamId": person["pro_team"], "injuryStatus": person["injury_status"], "stats": stats}}}


class FakeESPN:
    season = SEASON

    def __init__(self, rivals, schedule):
        self.rivals, self.schedule = rivals, schedule

    def league(self, *views, week=None, headers=None):
        if "mRoster" in views:
            return {"teams": [{"id": team, "roster": {"entries": [entry(p) for p in people]}}
                              for team, people in self.rivals.items()]}
        return {"schedule": self.schedule}

    def player_cards(self, player_ids, final_week):
        return {}


def setting_up():
    roster = list(full_roster().values())
    back = free_agent("RB", 9.0, season=60.0)            # preseason 3.5 a game; now doing starter's work
    receiver = free_agent("WR", 9.5, season=170.0)       # preseason 10 a game; nothing new
    kicker = free_agent("K", 6.0)
    pool = [back, receiver, kicker]
    rivals = {2: list(full_roster().values()), 3: list(full_roster().values())}
    schedule = [{"matchupPeriodId": w, "home": {"teamId": 1}, "away": {"teamId": 2 if w % 2 else 3}}
                for w in range(1, 15)]
    cards = {back["id"]: {"stats": [line(w, 16.0) for w in range(1, WEEK)]}}
    data = ({back["id"]: {w: (16.0, 16.0) for w in range(1, WEEK)}}, {back["id"]: set(range(1, WEEK))}, WEEK - 1)
    return roster, pool, rivals, schedule, cards, data


def test_record_logs_every_free_agent_four_ways_and_leaves_the_report_alone():
    roster, pool, rivals, schedule, cards, data = setting_up()
    current = waivers.score_pool(roster, pool, SLOTS, GAMES, {}, WEEK)
    order = [c["id"] for c in current]
    saved = rates.load
    rates.load = lambda season: data
    try:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "shadow.csv"
            note = shadow.record(FakeESPN(rivals, schedule), SETTINGS, 1, roster, pool, cards, SLOTS, GAMES, {},
                                 WEEK, current, current[:1], path=path, now=datetime(2026, 10, 6, 6, 7))
            rows = shadow.read(path)
    finally:
        rates.load = saved
    assert "1 backs and receivers rated" in note and "through week 4" in note
    assert [c["id"] for c in current] == order, "the report's order is untouched"
    assert len(rows) == 3 and {r["logged_at"] for r in rows} == {"2026-10-06T06:07:00"}
    back = next(r for r in rows if r["position"] == "RB")
    assert float(back["rate"]) > float(back["level"]) + 3, "four games of starter's work show in the rate"
    assert float(back["points_new"]) > float(back["points_now"]), "this week counts at his rate too"
    assert back["espn"] == str(round(60.0 / 17, 2)) and back["espn_week"] == "9.0"
    assert back["work_recent"] == back["work_average"] == "16.0"
    assert back["active"] == "1" and back["bench_floor"] != "", "he played last week; your bench has backs"
    kicker = next(r for r in rows if r["position"] == "K")
    assert kicker["active"] == "" and kicker["bench_floor"] == ""
    assert all(r[f"rank_{key}"] for r in rows for key in shadow.LISTS)
    assert [r["listed"] for r in rows].count("1") == 1
    assert all(float(r["wins_now"]) >= 0 for r in rows)


def run_rows(stamp, week, ranks_by_list, names):
    """One logged run: ranks_by_list maps each list to the player ids in its order."""
    rows = []
    for pid, name in names.items():
        row = {"logged_at": stamp, "week": str(week), "player_id": str(pid), "name": name, "position": "WR",
               "percent_owned": "10", "listed": "0", "espn": "7.0", "espn_week": "6.5", "level": "6.0", "rate": "8.0",
               "rate_work": "7.5", "rate_beyond": "0.5", "work_recent": "8.0", "work_average": "7.5",
               "active": "1", "bench_floor": "5.0",
               "points_now": "1", "points_new": "1", "wins_now": "0.1", "wins_new": "0.1",
               "xfp_through": str(week - 1)}
        for key in shadow.LISTS:
            row[f"rank_{key}"] = str(ranks_by_list[key].index(pid) + 1)
        rows.append(row)
    return rows


def test_the_summary_lines_up_today_s_lists_and_scores_past_picks():
    names = {pid: f"Player {pid}" for pid in range(1, 8)}
    level_first, rate_first = [1, 2, 3, 4, 5, 6, 7], [6, 1, 2, 3, 4, 7, 5]
    lists = {"points_now": level_first, "wins_now": level_first, "points_new": rate_first, "wins_new": rate_first}
    rows = run_rows("2026-10-06T06:07", 5, lists, names) + run_rows("2026-10-07T06:07", 5, lists, names)
    by_player = {6: {6: (9.0, 12.0), 7: (9.0, 10.0)}, 5: {6: (5.0, 2.0)}, 1: {5: (9.0, 30.0)}}
    subject, text = shadow.summary(rows, by_player)
    assert subject == "Rate shadow week 5: 0 hidden-value free agents; the report and the rate share 7 of their top 8"
    assert "Player 6" in text and "= 7.5 + 0.5" in text
    chosen = shadow.picks(rows)
    assert chosen["points_new"] == {(5, p): "2026-10-06T06:07" for p in (6, 1, 2, 3, 4)}, \
        "two runs in one week count once, from the first"
    only_rate = {k: v for k, v in chosen["points_new"].items() if k not in chosen["points_now"]}
    only_level = {k: v for k, v in chosen["points_now"].items() if k not in chosen["points_new"]}
    assert shadow.average(only_rate, by_player) == (1, 11.0)
    assert shadow.average(only_level, by_player) == (1, 2.0)


def test_only_games_after_the_pick_count():
    """A Saturday run in week 5: Thursday night's game came before it, Sunday's after."""
    weeks = {5: (9.0, 30.0, datetime(2026, 10, 8, 20, 15)), 6: (9.0, 10.0, datetime(2026, 10, 18, 13, 0))}
    assert shadow.scored_after(5, "2026-10-10T06:07:00", 1, {1: weeks}) == 10.0
    weeks[5] = (9.0, 30.0, datetime(2026, 10, 11, 13, 0))
    assert shadow.scored_after(5, "2026-10-10T06:07:00", 1, {1: weeks}) == 20.0


def test_hidden_value_lists_today_s_flags_and_scores_the_earlier_ones():
    names = {1: "Mack Hollins", 2: "Darius Cooper", 3: "Steady Eddie"}
    order = {key: [1, 2, 3] for key in shadow.LISTS}
    earlier = run_rows("2026-11-10T06:07", 10, order, names)
    today = run_rows("2026-11-23T06:07", 12, order, names)   # a Monday: week 12's Sunday is over
    for rows, unprojected in ((earlier, (1, 2)), (today, (1,))):
        for row in rows:
            if int(row["player_id"]) in unprojected:
                row.update(espn="0.0", rate="6.2")
    by_player = {1: {11: (7.0, 7.3), 12: (7.0, 7.3)}, 2: {11: (5.0, 1.0)}}
    subject, text = shadow.summary(earlier + today, by_player)
    assert subject.startswith("Rate shadow week 12: 1 hidden-value free agent;"), subject
    section = text[text.index("HIDDEN VALUE"):text.index("How the flags")]
    assert "Mack Hollins" in section and "Darius Cooper" not in section and "Steady Eddie" not in section
    assert "8.0 / 7.5" in section, "the workload trend is shown"
    assert "3 flagged so far (once a week per player), 2 have played since" in text, \
        "Hollins in weeks 10 and 12 and Cooper in week 10; Hollins was flagged on week 12's Monday"
    assert "ESPN said 0.0 a game, the rate 6.2, they scored 4.2." in text
    assert "1 of 2 scored 2+ a game over ESPN's number." in text


def test_a_flag_needs_him_playing_and_better_than_your_bench():
    names = {1: "Plays And Helps", 2: "Hurt Since Week 7", 3: "Behind Your Bench", 4: "No Bench Here"}
    rows = run_rows("2026-11-24T06:07", 12, {key: [1, 2, 3, 4] for key in shadow.LISTS}, names)
    for row in rows:
        row.update(espn="0.0", rate="6.5")
    rows[1]["active"] = "0"
    rows[2]["bench_floor"] = "7.0"
    rows[3]["bench_floor"] = ""
    flagged = [r["name"] for r in rows if shadow.flagged(r)]
    assert flagged == ["Plays And Helps", "No Bench Here"], flagged


def test_ties_go_to_the_list_s_own_points_a_game_not_ownership():
    tied = [dict(id=i, ahead_gain=0.0, season_gain=0, percent_owned=owned, level=level, season_projection=0.0)
            for i, (owned, level) in enumerate([(38, 4.0), (5, 8.0), (20, 6.0)], start=1)]
    assert shadow.ranks(tied, "ahead_gain") == {2: 1, 3: 2, 1: 3}
    tied[0]["ahead_gain"] = 0.5
    assert shadow.ranks(tied, "ahead_gain")[1] == 1, "points added still come first"


def test_the_table_counts_places_among_quarterbacks_backs_receivers_and_tight_ends():
    names = {1: "Kicker", 2: "Receiver A", 3: "Receiver B"}
    rows = run_rows("2026-11-24T06:07", 12, {key: [1, 2, 3] for key in shadow.LISTS}, names)
    rows[0]["position"] = "K"
    assert shadow.skill_places(rows)["points_now"] == {"2": 1, "3": 2}
    text = shadow.summary(rows, {})[1]
    assert "Receiver A" in text and "Kicker" not in text


def test_your_worst_healthy_bench_player_sets_the_floor():
    """The fixture's bench: a receiver projected 51 for the season (3.0 a game) and a back at 34 (2.0)."""
    def roster(**changes):
        players = full_roster()
        for key, change in changes.items():
            players[key] = dict(players[key], **change)
        return list(players.values())
    assert shadow.bench_floors(roster(), SLOTS, GAMES) == {"WR": 3.0, "RB": 2.0}
    assert shadow.bench_floors(roster(bench_rb={"injury_status": "OUT"}), SLOTS, GAMES) == {"WR": 3.0, "RB": 3.0}, \
        "an injured back doesn't set it; with no healthy back on the bench, the worst bench receiver does"
    assert shadow.bench_floors(roster(bench_wr={"rate": 6.0, "active": True}), SLOTS, GAMES)["WR"] == 6.0, \
        "valued by his rate, as the rate's lists value him"


def test_a_log_with_older_columns_is_carried_over_not_appended_to_crooked():
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "shadow.csv"
        old_fields = [f for f in shadow.FIELDS if f not in ("active", "bench_floor")]
        path.write_text(",".join(old_fields) + "\n" + ",".join("7" if f == "week" else "x" for f in old_fields) + "\n")
        shadow.upgrade(path)
        rows = shadow.read(path)
        assert list(rows[0]) == shadow.FIELDS and rows[0]["week"] == "7" and rows[0]["active"] == ""
        shadow.upgrade(path)
        assert shadow.read(path) == rows, "a second pass changes nothing"


def test_a_player_who_has_stopped_playing_is_shown_idle_and_left_out_of_the_disagreements():
    names = {1: "Hurt Since Week 7", 2: "Playing"}
    rows = run_rows("2026-11-24T06:07", 12, {key: [1, 2] for key in shadow.LISTS}, names)
    rows[0].update(active="0", rate="9.0", level="2.0")
    text = shadow.summary(rows, {})[1]
    table = text[text.index("TODAY'S TOP"):text.index("HIDDEN VALUE")]
    assert "idle" in table.split("Hurt Since Week 7")[1].splitlines()[0]
    disagreements = text[text.index("WHERE THE RATE"):text.index("SCORECARD")]
    assert "Hurt Since Week 7" not in disagreements and "Playing" in disagreements


def test_a_flag_needs_at_least_five_a_game():
    rows = run_rows("2026-11-24T06:07:00", 12, {key: [1, 2] for key in shadow.LISTS}, {1: "Worth It", 2: "Thin"})
    rows[0].update(espn="0.0", rate="5.0", bench_floor="1.8")
    rows[1].update(espn="0.0", rate="4.9", bench_floor="1.8")
    assert [r["name"] for r in rows if shadow.flagged(r)] == ["Worth It"]


def test_quarterbacks_and_tight_ends_stay_out_of_the_comparison():
    rows = run_rows("2026-11-24T06:07:00", 12, {key: [1, 2, 3] for key in shadow.LISTS},
                    {1: "A Quarterback", 2: "A Tight End", 3: "A Receiver"})
    rows[0]["position"], rows[1]["position"] = "QB", "TE"
    text = shadow.summary(rows, {})[1]
    assert "A Receiver" in text and "A Quarterback" not in text and "A Tight End" not in text
    assert shadow.skill_places(rows)["points_now"] == {"3": 1}


def test_two_logs_of_one_player_in_one_run_count_once():
    rows = run_rows("2026-11-24T06:07:00", 12, {key: [1, 2] for key in shadow.LISTS}, {1: "Once", 2: "Other"})
    text = shadow.summary(rows + [dict(rows[0])], {})[1]
    table = text[text.index("TODAY'S TOP"):text.index("HIDDEN VALUE")]
    assert table.count("Once") == 1


def test_a_hair_under_zero_wins_ties_with_zero():
    tied = [dict(id=1, wins_now=-0.0002, ahead_gain=0.0, season_gain=0, percent_owned=5, level=9.0, season_projection=0.0),
            dict(id=2, wins_now=0.0, ahead_gain=0.0, season_gain=0, percent_owned=50, level=3.0, season_projection=0.0)]
    assert shadow.ranks(tied, "wins_now") == {1: 1, 2: 2}, "the better player a game wins the tie"


def test_nothing_logged_says_so():
    subject, text = shadow.summary([], {})
    assert "nothing logged" in subject and "No shadow runs" in text


def test_the_comparison_goes_out_on_tuesdays_with_the_report():
    sent, emailed = [], []
    saved = (waivers.ESPN.__dict__["from_env"], waivers.gather, waivers.ask_claude, waivers.mailer.send,
             shadow.send, shadow.is_summary_day)
    waivers.ESPN.from_env = lambda: type("FakeESPN", (), {"season": SEASON})()
    waivers.gather = lambda espn, rate_shadow=False: _sample_context()
    waivers.ask_claude = lambda brief: None
    waivers.mailer.send = lambda subject, body, html=None, **slack_parts: emailed.append(subject) or True
    shadow.send = lambda season=None: sent.append(season)
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            for tuesday, email, rate_shadow in ((True, True, True), (False, True, True), (True, False, True),
                                                (True, True, False)):
                shadow.is_summary_day = lambda now=None, answer=tuesday: answer
                waivers.main(email=email, rate_shadow=rate_shadow)
    finally:
        (waivers.ESPN.from_env, waivers.gather, waivers.ask_claude, waivers.mailer.send,
         shadow.send, shadow.is_summary_day) = saved
    assert sent == [SEASON], "only a Tuesday run that emails and runs the shadow sends it"
    assert len(emailed) == 3, "and the waiver report goes out as before"


def test_tuesday_is_the_day():
    assert shadow.is_summary_day(datetime(2026, 11, 24)) and not shadow.is_summary_day(datetime(2026, 11, 25))


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
