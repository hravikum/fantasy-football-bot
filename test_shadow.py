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
    assert len(rows) == 3 and {r["logged_at"] for r in rows} == {"2026-10-06T06:07"}
    back = next(r for r in rows if r["position"] == "RB")
    assert float(back["rate"]) > float(back["level"]) + 3, "four games of starter's work show in the rate"
    assert float(back["points_new"]) > float(back["points_now"]), "this week counts at his rate too"
    assert back["espn"] == str(round(60.0 / 17, 2)) and back["espn_week"] == "9.0"
    assert back["work_recent"] == back["work_average"] == "16.0"
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
    assert chosen["points_new"] == {(5, p): 5 for p in (6, 1, 2, 3, 4)}, "two runs in one week count once"
    only_rate = {k: v for k, v in chosen["points_new"].items() if k not in chosen["points_now"]}
    only_level = {k: v for k, v in chosen["points_now"].items() if k not in chosen["points_new"]}
    assert shadow.average(only_rate, by_player) == (1, 11.0)
    assert shadow.average(only_level, by_player) == (1, 2.0)


def test_a_monday_run_counts_from_the_next_week():
    """ESPN is still on the week just played on Monday morning, so its Sunday games are already in."""
    assert shadow.first_week_after(5, "2026-10-06T06:07") == 5, "Tuesday: the week's games are all ahead"
    assert shadow.first_week_after(5, "2026-10-12T06:07") == 6, "Monday: Sunday's games are over"
    by_player = {1: {5: (9.0, 30.0)}}
    assert shadow.scored_after(5, 1, by_player) == 30.0 and shadow.scored_after(6, 1, by_player) is None


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


def test_nothing_logged_says_so():
    subject, text = shadow.summary([], {})
    assert "nothing logged" in subject and "No shadow runs" in text


def test_the_comparison_goes_out_on_tuesdays_with_the_report():
    sent, emailed = [], []
    saved = (waivers.ESPN.__dict__["from_env"], waivers.gather, waivers.ask_claude, waivers.mailer.send,
             shadow.send, shadow.is_summary_day)
    waivers.ESPN.from_env = lambda: type("FakeESPN", (), {"season": SEASON})()
    waivers.gather = lambda espn, ranker, rate_shadow=False: _sample_context()
    waivers.ask_claude = lambda brief: None
    waivers.mailer.send = lambda subject, body, html=None: emailed.append(subject) or True
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
