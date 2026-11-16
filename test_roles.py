"""Tests for the next-man-up rule and what the waiver report does with it. Run: python3 test_roles.py"""
import sys
from datetime import datetime, timedelta

import roles
import waivers
from espn import BENCH
from lineup import plan
from test_lineup import GAMES, SLOTS, full_roster, player
from test_waivers import free_agent

SEASON = 2026
_next_id = [9000]


def back(name, team=5, status="ACTIVE", **by_week):
    """A running back with his carries plus targets by week: back("Lead", w1=18, w2=20)."""
    _next_id[0] += 1
    return {"id": _next_id[0], "name": name, "position": "RB", "pro_team": team, "injury_status": status,
            "touches": {int(week[1:]): count for week, count in by_week.items()}}


def room(lead_latest=0, lead_status="ACTIVE", heir_latest=19, heir_status="ACTIVE"):
    """A lead back at about 18 touches a game, a change-of-pace back, and a third who rarely plays."""
    lead = back("Lead", status=lead_status, w1=18, w2=20, w3=17, w4=lead_latest)
    heir = back("Heir", status=heir_status, w1=4, w2=5, w3=3, w4=heir_latest)
    third = back("Third", w1=1, w2=0, w3=2, w4=3)
    return lead, heir, third


WEEKS = [1, 2, 3, 4]


def test_touches_are_carries_plus_targets_in_the_weeks_asked_for():
    def line(week, carries, targets, source=0, season=SEASON, stats=None):
        return {"seasonId": season, "scoringPeriodId": week, "statSourceId": source, "statSplitTypeId": 1,
                "stats": {"23": carries, "58": targets, "53": 9.0} if stats is None else stats}
    card = {"stats": [line(1, 10.0, 3.0), line(2, 0.0, 0.0, stats={}), line(3, 7.0, 2.0),
                      line(4, 20.0, 5.0),                    # not asked for
                      line(3, 30.0, 30.0, source=1),         # a projection
                      line(1, 40.0, 40.0, season=SEASON - 1)]}
    assert roles.touches(card, SEASON, [1, 2, 3]) == {1: 13, 3: 9}, "a week with no stats is a game he missed"
    assert roles.touches({}, SEASON, [1, 2, 3]) == {}


def test_this_week_counts_once_the_game_is_over():
    kickoff = datetime(2026, 11, 15, 13)
    byes = {5: {3}, 6: set(), 7: {5}}
    games = {5: (6, kickoff), 6: (5, kickoff)}                 # team 7 is idle this week
    monday = roles.finished_weeks(5, byes, games, kickoff + timedelta(hours=17))
    assert monday == {5: [1, 2, 4, 5], 6: [1, 2, 3, 4, 5], 7: [1, 2, 3, 4]}, "a bye is not a game"
    during = roles.finished_weeks(5, byes, games, kickoff + timedelta(hours=2))
    assert during[5] == [1, 2, 4], "a game still being played has told us nothing yet"
    assert roles.finished_weeks(5, byes, {5: (6, None)}, kickoff)[5] == [1, 2, 4], "no kickoff time, no claim"


def test_a_lead_back_carries_the_load_in_the_games_before_the_latest():
    lead, heir, third = room()
    found, usual = roles.lead_back([lead, heir, third], WEEKS)
    assert found is lead and round(usual, 1) == 18.3
    assert roles.lead_back([lead, heir, third], WEEKS[:2]) is None, "two games is too few to call it"


def test_a_committee_has_no_job_to_inherit():
    split = [back("A", w1=13, w2=12, w3=14, w4=0), back("B", w1=12, w2=13, w3=11, w4=20),
             back("C", w1=9, w2=10, w3=8, w4=9)]
    assert roles.lead_back(split, WEEKS) is None, "no back has 45% of the work"
    light = [back("A", w1=9, w2=10, w3=8, w4=0), back("B", w1=2, w2=1, w3=3, w4=12)]
    assert roles.lead_back(light, WEEKS) is None and roles.next_man_up(light, WEEKS) is None


def test_the_back_who_took_the_work_when_the_lead_missed_the_game():
    lead, heir, third = room(lead_latest=0, heir_latest=19)
    flag = roles.next_man_up([lead, heir, third], WEEKS)
    assert flag["player"] is heir and flag["lead"] is lead and flag["week"] == 4
    assert flag["latest"] == 19 and flag["usual"] == 4.0 and flag["lead_latest"] == 0


def test_a_lead_who_left_early_counts_as_down():
    lead, heir, third = room(lead_latest=5, heir_latest=16)        # 5 of his usual 18
    assert roles.next_man_up([lead, heir, third], WEEKS)["player"] is heir
    lead, heir, third = room(lead_latest=8, heir_latest=16)        # a quiet game, not an exit
    assert roles.next_man_up([lead, heir, third], WEEKS) is None


def test_a_healthy_lead_means_nobody_is_next():
    lead, heir, third = room(lead_latest=21, heir_latest=6)
    assert roles.next_man_up([lead, heir, third], WEEKS) is None


def test_a_tag_alone_is_enough_and_names_the_next_in_line():
    lead, heir, third = room(lead_latest=21, lead_status="OUT", heir_latest=1)
    third["touches"][4] = 6                                        # one odd game doesn't change the order
    flag = roles.next_man_up([lead, heir, third], WEEKS)
    assert flag["player"] is heir, "with no game to go on, the back with the most work before it"
    lead["injury_status"] = "QUESTIONABLE"
    assert roles.next_man_up([lead, heir, third], WEEKS) is None, "questionable backs mostly play"
    lead["injury_status"] = "DOUBTFUL"
    assert roles.next_man_up([lead, heir, third], WEEKS)["player"] is heir


def test_a_back_who_cant_play_isnt_next():
    lead, heir, third = room(lead_latest=0, heir_latest=19, heir_status="INJURY_RESERVE")
    assert roles.next_man_up([lead, heir, third], WEEKS)["player"] is third
    lead, heir, third = room(lead_latest=0, heir_latest=0)
    third["touches"][4] = 0
    assert roles.next_man_up([lead, heir, third], WEEKS) is None, "nobody took the work"


def card(person):
    """The player as an ESPN card: every touch a carry."""
    return {"stats": [{"seasonId": SEASON, "scoringPeriodId": week, "statSourceId": 0, "statSplitTypeId": 1,
                       "stats": {"23": float(count)}} for week, count in person["touches"].items()]}


def league(heir_available=True):
    """Team 5's lead missed week 4. Team 6 is healthy. Returns (free agents, rostered, cards, team weeks)."""
    lead, heir, third = room(lead_latest=0, heir_latest=19)
    steady = back("Steady", team=6, w1=20, w2=19, w3=21, w4=22)
    backup = back("Backup", team=6, w1=3, w2=2, w3=4, w4=3)
    receiver = dict(player("WR", BENCH, 9.0), pro_team=5)
    everyone = [lead, heir, third, steady, backup]
    cards = {p["id"]: card(p) for p in everyone}
    for person in everyone:
        del person["touches"]                                     # mark() reads them from the cards
    free = [third, backup, receiver] + ([heir] if heir_available else [])
    rostered = [lead, steady] + ([] if heir_available else [heir])
    return free, rostered, cards, {5: WEEKS, 6: WEEKS}


def test_mark_tags_the_free_agent_and_values_his_next_four_weeks():
    free, rostered, cards, team_weeks = league()
    found = roles.mark(free, rostered, cards, SEASON, team_weeks)
    assert [p["name"] for p in found] == ["Heir"]
    heir = found[0]
    assert heir["role"]["lead"] == "Lead" and heir["role"]["touches"] == 19 and heir["role"]["week"] == 4
    assert heir["fill_in"] == {5: 9.4, 6: 9.4, 7: 9.4, 8: 9.4}
    assert all("role" not in p for p in free if p is not heir), "the healthy team's backup is just a backup"


def test_mark_says_nothing_when_the_next_man_is_already_rostered():
    free, rostered, cards, team_weeks = league(heir_available=False)
    assert roles.mark(free, rostered, cards, SEASON, team_weeks) == []
    assert all("role" not in p for p in free + rostered)


def test_the_reason_reads_differently_for_a_missed_game_an_exit_and_a_tag():
    flagged = {"name": "Heir", "role": {"lead": "Lead", "lead_status": "ACTIVE", "week": 4, "touches": 19,
                                        "usual": 4.0, "lead_touches": 0, "lead_usual": 18.3}}
    assert "Lead, who missed week 4. He had 19 carries and targets in week 4 (usually 4)" in roles.describe(flagged)
    flagged["role"]["lead_touches"] = 5
    assert "who left week 4 after 5 touches (usually 18)" in roles.describe(flagged)
    flagged["role"].update(lead_touches=21, lead_status="INJURY_RESERVE")
    assert "who is tagged injury reserve" in roles.describe(flagged)


def flagged_agent(projection=4.0, level=3.0, week=4):
    person = dict(free_agent("RB", projection), level=level)
    person["role"] = {"lead": "Lead", "lead_status": "ACTIVE", "week": week, "touches": 19, "usual": 4.0,
                      "lead_touches": 0, "lead_usual": 18.3}
    person["fill_in"] = {w: roles.FILL_IN for w in range(week + 1, week + 1 + roles.FILL_IN_WEEKS)}
    return person


def test_a_fill_in_lifts_the_weeks_it_covers_and_no_others():
    person = flagged_agent(projection=4.0, level=3.0, week=4)      # covers weeks 5 to 8
    by_week = {week: waivers.projected_for([person], week, 5)[0]["projection"] for week in (5, 6, 8, 9)}
    assert by_week == {5: 9.4, 6: 9.4, 8: 9.4, 9: 3.0}, "this week beats ESPN's 4.0; week 9 is back to his level"
    proven = dict(flagged_agent(projection=14.0, level=12.0, week=4))
    assert waivers.projected_for([proven], 5, 5)[0]["projection"] == 14.0, "a fill-in never lowers a number"
    assert waivers.projected_for([proven], 6, 5)[0]["projection"] == 12.0
    plain = free_agent("RB", 4.0)
    assert waivers.projected_for([plain], 5, 5)[0] is plain


def test_a_flagged_back_is_valued_as_a_starter_in_your_lineup():
    roster = list(full_roster(flex2=player("WR", 23, 7.0)).values())   # weakest starter projects 7.0
    ranked = waivers.score_pool(roster, [flagged_agent(), dict(free_agent("RB", 4.0), level=3.0)],
                                SLOTS, GAMES, byes={}, week=5)
    flagged, plain = sorted(ranked, key=lambda c: "role" not in c)
    assert flagged["gain"] == 2.4 and flagged["ahead_gain"] == 9.6, "9.4 against your 7.0, for four weeks"
    assert plain["gain"] == 0 and plain["ahead_gain"] == 0


def test_a_flagged_back_is_listed_and_bid_as_a_starter_even_with_no_lineup_gain():
    roster = list(full_roster().values())                              # weakest starter projects 10.0
    pool = [free_agent("WR", 10.0 + n) for n in range(4)] + [free_agent("TE", 9.0 + n) for n in range(4)] \
        + [free_agent("QB", 20.0 + n) for n in range(4)] + [free_agent("RB", 10.0 + n) for n in range(4)] \
        + [flagged_agent()]
    scored = waivers.score_pool(roster, pool, SLOTS, GAMES, byes={}, week=5)
    listed = waivers.shortlist_from(scored)
    assert len(listed) == waivers.SHORTLIST + 1 and "role" in listed[-1], "on top of the eight, not instead of one"
    assert listed[-1]["ahead_gain"] == 0 and waivers.bid_band(listed[-1]) == 0
    # Shaped like this league's winning claims: mostly $0-3, and about one in nine at $11 or more.
    prices = [0] * 45 + [1] * 25 + [2] * 20 + [3] * 12 + [5] * 10 + [7] * 6 \
        + [11, 11, 12, 13, 14, 17, 18, 19, 25, 28, 30, 33, 37, 38, 39]
    assert waivers.suggest_bid(listed[-1], 65, 0, prices) == (11, 30), "the top of what claims cost here"
    assert waivers.suggest_bid(dict(listed[-1], role=None), 65, 0, prices) == (0, 1), "and without the flag, nothing"


def test_the_list_takes_only_so_many_extra_backs_busiest_first():
    extras = []
    for touches in (11, 25, 14, 19):
        person = flagged_agent()
        person["role"]["touches"] = touches
        extras.append(person)
    scored = waivers.score_pool(list(full_roster().values()), extras, SLOTS, GAMES, byes={}, week=5)
    listed = waivers.shortlist_from(scored)
    assert len(listed) == 4 and len({c["id"] for c in listed}) == 4, "two by the position cap, two more, nobody twice"
    extra = [c["role"]["touches"] for c in listed[waivers.PER_POSITION:]]
    assert extra == sorted(extra, reverse=True), "the extra rows go busiest first"
    six = [flagged_agent() for _ in range(6)]
    scored = waivers.score_pool(list(full_roster().values()), six, SLOTS, GAMES, byes={}, week=5)
    assert len(waivers.shortlist_from(scored)) == waivers.PER_POSITION + waivers.ROLE_SLOTS


def context_with(candidates):
    roster = list(full_roster().values())
    ranked = waivers.shortlist_from(waivers.score_pool(roster, candidates, SLOTS, GAMES, byes={}, week=5))
    for candidate in ranked:
        candidate["bid"] = (12, 30) if candidate.get("role") else (0, 1)
    target, _ = plan(roster, SLOTS, GAMES)
    return {"week": 5, "budget": 100, "budget_left": 65, "minimum_bid": 0, "rivals": [], "last_runs": [],
            "gaps": [], "candidates": ranked, "drops": [], "advice": None, "roster": roster, "target": target}


def test_both_reports_and_the_brief_say_why_he_is_listed():
    context = context_with([flagged_agent(), free_agent("WR", 14.0)])
    name = next(c["name"] for c in context["candidates"] if c.get("role"))
    text = waivers.build_report(context)
    assert "Next man up:" in text and f"{name} is next up behind Lead, who missed week 4" in text
    assert text.index("Next man up:") < text.index("PLAYER"), "above the table, where it gets read"
    context["gaps"] = ["week 7: K short by 1"]
    html = waivers.build_html(context)
    assert "Next man up" in html and f"{name} is next up behind Lead" in html
    assert "Lineup holes" in html and "week 7: K short by 1" in html, "one block doesn't replace the other"
    brief = waivers.brief_for_claude(context)
    notes = {c["name"]: c["next_man_up"] for c in brief["available"]}
    assert notes[name].startswith(f"{name} is next up behind Lead") and list(notes.values()).count(None) == 1
    assert "9.4 points a game" in brief["note_on_next_man_up"]


def test_a_quiet_week_shows_no_block():
    context = context_with([free_agent("WR", 14.0)])
    assert "Next man up" not in waivers.build_report(context) and "Next man up" not in waivers.build_html(context)


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
