"""Tests for the waiver report. Run: python3 test_waivers.py"""
import sys

import bids
import waivers
from espn import BENCH, IR
from test_lineup import GAMES, SLOTS, full_roster, player
from lineup import plan


def free_agent(position, projection, season=None, status="ACTIVE", owned=12.0, change=0.0):
    person = player(position, BENCH, projection, status=status, season=season)
    person.update({"percent_owned": owned, "percent_change": change})
    return person


def test_candidate_gain_is_what_he_adds_to_the_starting_lineup():
    roster = list(full_roster().values())          # weakest starter projects 10.0
    pool = [free_agent("WR", 14.0), free_agent("WR", 4.0)]
    ranked = waivers.score_pool(roster, pool, SLOTS, GAMES)
    assert round(ranked[0]["gain"], 1) == 4.0 and ranked[0]["would_start"]
    assert ranked[1]["gain"] == 0.0 and not ranked[1]["would_start"], "a bench-quality add gains nothing"


def test_a_player_who_adds_nothing_is_not_listed():
    """Nov 16: seven of the eight rows were the most widely rostered free agents at +0.0, +0.0, +0, each
    for $0-1, Justin Herbert among them."""
    roster = list(full_roster().values())          # weakest starter projects 10.0
    useful = free_agent("WR", 14.0, season=238.0)
    pool = [useful] + [free_agent("QB", 12.0, season=100.0, owned=30.0), free_agent("WR", 4.0, owned=25.0)]
    scored = waivers.score_pool(roster, pool, SLOTS, GAMES)
    assert [waivers.bid_band(c) for c in scored] == [0, 3, 3]
    assert [c["id"] for c in waivers.shortlist_from(scored)] == [useful["id"]]
    assert waivers.shortlist_from(scored[1:]) == [], "and an empty list is a fine answer"
    promoted = dict(scored[1], jev=0.8, jev_confidence=0.7)
    assert waivers.shortlist_from([promoted]) == [promoted], "a confident Jev rating is worth a bid too"


def test_both_reports_say_so_when_nobody_is_worth_a_bid():
    context = _sample_context(candidates=[])
    assert waivers.NOBODY in waivers.build_report(context)
    html = waivers.build_html(context)
    assert waivers.NOBODY in html and "<table" not in html


def test_backup_quarterbacks_do_not_crowd_out_the_list():
    """The bug from the first live run: with every gain at zero, QBs won on raw season points."""
    roster = list(full_roster().values())          # starting QB projects 20.0 a week, 340 a season
    pool = [free_agent("QB", 17.0, season=290.0 - i) for i in range(6)]
    pool += [free_agent("WR", 9.5, season=200.0), free_agent("RB", 9.0, season=190.0)]
    ranked = waivers.candidates(roster, pool, SLOTS, GAMES)
    assert sum(1 for c in ranked if c["position"] == "QB") <= waivers.PER_POSITION
    assert ranked[0]["position"] in ("WR", "RB"), "a startable flex piece outranks a backup QB"


def test_season_gain_ignores_points_you_would_never_start():
    roster = list(full_roster().values())
    backup_qb = free_agent("QB", 17.0, season=300.0)   # huge season total, still behind your starter
    flex_help = free_agent("WR", 9.5, season=200.0)    # behind your flex this week, ahead of him all season
    ranked = {c["position"]: c for c in waivers.score_pool(roster, [backup_qb, flex_help], SLOTS, GAMES)}
    assert ranked["QB"]["season_gain"] == 0, "a season total is not an upgrade if he never starts"
    assert ranked["WR"]["season_gain"] > 0


def test_a_better_kicker_is_worth_nothing_while_yours_plays():
    """Kicker projections predicted nothing over 2025 and 2026, so a better one isn't better."""
    scored = waivers.score_pool(list(full_roster().values()), [free_agent("K", 11.0)], SLOTS, GAMES)
    assert scored[0]["gain"] == 0.0 and scored[0]["season_gain"] == 0     # your kicker projects 8.0
    assert waivers.shortlist_from(scored) == [], "so he isn't on the list at all"


def test_a_kicker_is_worth_plenty_when_yours_is_on_bye():
    roster = full_roster(k=player("K", 17, 8.0, bye=True))
    ranked = waivers.candidates(list(roster.values()), [free_agent("K", 11.0)], SLOTS, GAMES)
    assert round(ranked[0]["gain"], 1) == 11.0


def test_a_kicker_for_a_later_week_waits_for_that_week():
    """Jason Myers is on bye next week. The report listed two kickers for it a week early, at the top."""
    roster = list(full_roster(k=dict(player("K", 17, 8.0), pro_team=2)).values())   # he plays this week
    scored = waivers.score_pool(roster, [free_agent("K", 9.0), free_agent("WR", 14.0)], SLOTS, GAMES,
                                byes={2: {6}}, week=5)
    waiting = next(c for c in scored if c["position"] == "K")
    assert waiting["gain"] == 0 and waiting["ahead_gain"] == 9.0 and waivers.held(waiting)
    assert [c["position"] for c in waivers.shortlist_from(scored)] == ["WR"]
    games_without_his = {team: game for team, game in GAMES.items() if team != 2}
    needed_now = waivers.score_pool(roster, [free_agent("K", 9.0)], SLOTS, games_without_his, byes={2: {5}}, week=5)
    assert needed_now[0]["gain"] == 9.0 and not waivers.held(needed_now[0]), "the week he's needed, he is listed"


def test_a_player_who_has_played_this_week_is_free_to_move_in_later_weeks():
    """Monday morning: Sunday's players are locked, and next week's lineup must not inherit that."""
    starter = dict(player("TE", 6, 9.0, locked=True), pro_team=2)      # your tight end, idle in week 6
    backup = player("TE", BENCH, 8.0, locked=True)                     # his backup played on Sunday too
    roster = list(full_roster(te=starter, bench_wr=backup).values())
    byes = {2: {6}}
    assert waivers.bye_gaps(roster, SLOTS, waivers.schedule_ahead(GAMES, byes, 5), 5) == [], \
        "the backup covers week 6, so there is no hole"
    fill_in = waivers.score_pool(roster, [free_agent("TE", 8.5)], SLOTS, GAMES, byes=byes, week=5)[0]
    assert fill_in["gain"] == 0 and fill_in["ahead_gain"] == 0.5, "half a point over your backup, in week 6 only"


def test_a_free_agent_whose_game_has_started_adds_nothing_until_next_week():
    from datetime import datetime, timedelta
    kickoff = datetime(2026, 11, 15, 13)
    played = free_agent("WR", 14.0)
    tonight, idle = dict(free_agent("WR", 14.0), pro_team=2), dict(free_agent("WR", 14.0), pro_team=3)
    games = dict(GAMES)
    games.update({1: (9, kickoff), 2: (8, kickoff + timedelta(hours=31))})
    del games[3]                                                       # team 3 has a bye
    waivers.lock_started([played, tonight, idle], games, kickoff + timedelta(hours=20))     # Monday morning
    assert [p["locked"] for p in (played, tonight, idle)] == [True, False, False]
    roster = list(full_roster().values())                              # your weakest starter projects 10.0
    scored = {c["id"]: c for c in waivers.score_pool(roster, [played, tonight], SLOTS, games, byes={}, week=5)}
    assert scored[played["id"]]["gain"] == 0 and scored[played["id"]]["ahead_gain"] == 16.0, "four later weeks at +4"
    assert scored[tonight["id"]]["gain"] == 4.0 and scored[tonight["id"]]["ahead_gain"] == 20.0
    waivers.lock_started([played], {1: (9, None)}, kickoff)
    assert played["locked"] is False, "with no kickoff time there is nothing to go on"


def test_who_is_held_back_and_who_is_not():
    base = {"position": "WR", "gain": 0.0, "ahead_gain": 9.0, "season_gain": 0, "weeks_helped": 1}
    assert waivers.held(base), "a receiver who fills one later week and nothing else"
    assert not waivers.held(dict(base, season_gain=4)), "he also makes your lineup better all season"
    assert not waivers.held(dict(base, weeks_helped=3)), "three weeks of help is not a fill-in"
    assert not waivers.held(dict(base, gain=2.0)), "he helps this week"
    assert not waivers.held(dict(base, role={"touches": 19})), "a next man up is the exception that pays"
    assert not waivers.held(dict(base, ahead_gain=0.0, weeks_helped=0)), "a row that adds nothing is left as it was"
    assert waivers.held(dict(base, position="D/ST", weeks_helped=3)), "a defense waits whatever it adds later"
    assert waivers.held(dict(base, position="K", ahead_gain=0.0, weeks_helped=0))
    assert not waivers.held(dict(base, position="K", gain=8.0))


def test_held_kickers_leave_their_places_to_players_you_can_use():
    roster = list(full_roster(k=dict(player("K", 17, 8.0), pro_team=2),
                              dst=dict(player("D/ST", 16, 7.0), pro_team=2)).values())
    pool = [free_agent("K", 9.0), free_agent("K", 8.5), free_agent("D/ST", 6.5), free_agent("D/ST", 6.0)] \
        + [free_agent("WR", 10.5 + n / 2) for n in range(2)] + [free_agent("RB", 10.5 + n / 2) for n in range(2)]
    scored = waivers.score_pool(roster, pool, SLOTS, GAMES, byes={2: {6}}, week=5)   # both idle in week 6
    assert [c["position"] for c in scored[:4]] == ["K", "K", "D/ST", "D/ST"], "they used to lead the list"
    assert sorted(c["position"] for c in waivers.shortlist_from(scored)) == ["RB", "RB", "WR", "WR"]


def test_a_free_agent_who_cleared_waivers_is_added_not_bid_on():
    context = _sample_context()
    assert waivers.FREE_NOTE not in waivers.build_report(context) and "$8&ndash;15" in waivers.build_html(context)
    assert waivers.brief_for_claude(context)["available"][0]["availability"].startswith("on waivers")
    context["candidates"][0]["free_now"] = True
    text, html = waivers.build_report(context), waivers.build_html(context)
    row = next(line for line in text.splitlines() if line.startswith(context["candidates"][0]["name"]))
    assert f" {waivers.FREE} " in row and "$8" not in row, "no bid is shown for a player who costs nothing"
    assert waivers.FREE_NOTE in text and f">{waivers.FREE}<" in html and "$8&ndash;15" not in html
    assert waivers.brief_for_claude(context)["available"][0]["availability"].startswith("free agent")


def test_the_holes_block_says_fill_ins_wait():
    context = _sample_context()                                # has a week-5 hole
    assert waivers.HELD_NOTE in waivers.build_report(context) and waivers.HELD_NOTE in waivers.build_html(context)
    context["gaps"] = []
    assert waivers.HELD_NOTE not in waivers.build_report(context)


def test_espn_marks_who_can_be_added_on_the_spot():
    from espn import ESPN
    from test_lineup import ELIGIBLE
    def entry(pid, status):
        return {"status": status, "player": {"id": pid, "fullName": f"P{pid}", "eligibleSlots": ELIGIBLE["WR"],
                                             "proTeamId": 1, "stats": [], "ownership": {}}}
    espn = ESPN(1, "s2", "{swid}", season=2026)
    espn.league = lambda *views, **kwargs: {"players": [entry(1, "FREEAGENT"), entry(2, "WAIVERS"), entry(3, None)]}
    assert [p["free_now"] for p in espn.free_agents(5)] == [True, False, False], "unknown is treated as on waivers"


def _prompt_sent():
    """The text ask_claude sends to the Messages API."""
    import os
    sent = []
    def capture(url, json=None, headers=None, timeout=None):
        sent.append(json["messages"][0]["content"])
        class Reply:
            status_code = 200
            def json(self):
                return {"content": [{"type": "text", "text": "ok"}]}
        return Reply()
    saved = waivers.requests.post
    waivers.requests.post = capture
    os.environ["ANTHROPIC_API_KEY"] = "test-key"
    try:
        waivers.ask_claude({"week": 2})
    finally:
        waivers.requests.post = saved
        os.environ["ANTHROPIC_API_KEY"] = ""
    return sent[0]


def test_claude_is_told_to_wait_for_the_week_and_not_to_carry_two_kickers():
    prompt = _prompt_sent()
    assert "claiming a few days" not in prompt and "early rather than scrambling" not in prompt
    assert "A hole in a later week is not a claim today" in prompt
    assert "never drop another position to carry two" in prompt
    assert "early on Wednesday" in prompt and "Waivers process daily" not in prompt
    assert "should be added today" in prompt


# What this league's claims took to win through week 10 of 2026, after a breakout game and otherwise.
PAID = {bids.IN_DEMAND: [0, 2, 3, 3, 5, 6, 6, 6, 7, 7, 8, 11, 11, 14, 17, 19, 28, 30, 33, 37],
        bids.OTHER: [0] * 20 + [1] * 9 + [2] * 8 + [3] * 8 + [4] * 5 + [5, 5, 6, 8, 9, 11, 11, 11, 12, 13, 25, 39],
        bids.STREAMED: [0, 0, 1, 2, 3]}


def test_both_reports_say_what_bids_have_won_here():
    context = _sample_context(past_claims=PAID)
    text, html = waivers.build_report(context), waivers.build_html(context)
    for piece in ("next men up (20 claims): $8 won half, $18 won three in four, $31 won nine in ten",
                  "tight end (62 claims): $3 won half, $5 won three in four, $12 won nine in ten",
                  "Kickers and defenses: 5 claims so far, so a share of your budget until there are 10"):
        assert piece in " ".join(text.split()) and piece in html, piece      # the text report wraps long notes
    said = ("For a player who would start for you, the range runs from the bid that won three in four to the one that "
            "won nine in ten; for a stash, from half to three in four.")
    assert said in " ".join(text.split()) and said in html
    assert max(len(line) for line in text.splitlines() if "$" in line and "PLAYER" not in line) <= 110
    assert "Priced as in demand" not in text + html, "nobody on this list is"
    context["candidates"][0]["breakout"] = {"week": 2, "points": 21.0, "before": 4.0}
    named = f"Priced as in demand today: {context['candidates'][0]['name']} (21.0 points in week 2, 4.0 a game before)"
    assert named in waivers.build_report(context) and named in waivers.build_html(context)
    context["candidates"][0]["free_now"] = True
    assert "Priced as in demand" not in waivers.build_report(context), "a free add has no price"


def test_the_notes_read_the_odds_from_the_same_settings_the_bids_use():
    assert set(waivers.STARTER + waivers.STASH) == set(waivers.ODDS), "every share a bid uses has its words"
    assert waivers.STARTER[0] > waivers.STASH[0] and waivers.STARTER[1] > waivers.STARTER[0]
    notes = waivers.bid_notes(_sample_context(past_claims=PAID))
    low, high = waivers.suggest_bid({"position": "RB", "gain": 4.0, "season_gain": 60, "role": {"touches": 19}}, 100, 0, PAID)
    assert f"${low} won three in four, ${high} won nine in ten" in notes[1], "the range is two of the printed steps"
    unpriced = waivers.bid_notes(_sample_context())
    assert len(unpriced) == 4 and all(note.endswith("until there are 10") for note in unpriced[1:]), \
        "no history: every kind says so, and nothing is said about odds a budget share doesn't have"


def test_the_pace_line_says_what_is_left_a_week_and_what_has_gone():
    context = _sample_context(week=11, budget=100, budget_left=65, weeks_left=7)
    assert waivers.faab_pace(context) == ("$9 a week for the 7 weeks of waivers left, this one included; "
                                          "$4 a week spent so far")
    assert waivers.faab_pace(context) in waivers.build_report(context)
    assert waivers.faab_pace(context) in waivers.build_html(context)
    assert waivers.faab_pace(dict(context, week=1)) == "$9 a week for the 7 weeks of waivers left, this one included"
    assert waivers.faab_pace(dict(context, weeks_left=1)).startswith("$65 a week for the 1 week of waivers left")
    assert waivers.faab_pace(dict(context, weeks_left=0)) is None, "ESPN didn't say when the season ends"
    assert "a week" not in waivers.build_report(_sample_context()), "and then the report says nothing about pace"


def test_claude_is_told_what_the_ranges_mean_and_when_to_go_high():
    context = _sample_context(past_claims=PAID, week=11, budget_left=65, weeks_left=7)
    brief = waivers.brief_for_claude(context)
    assert brief["note_on_bids"] == waivers.bid_notes(context) and brief["faab_pace"] == waivers.faab_pace(context)
    assert "Tuesday that week starts" in brief["note_on_holes"]
    prompt = _prompt_sent()
    for piece in ("note_on_bids says what each end of a suggested range has won", "faab_pace shows money to spare",
                  "FAAB left over when the season ends buys nothing", "bid from the in-demand line and say why"):
        assert piece in prompt, piece
    assert "inside the suggested range" not in prompt


def roster_at_capacity(**overrides):
    """Sixteen players, so an add has to cost a drop, the way it does in a real league."""
    roster = full_roster(**overrides)
    for spare in range(16 - sum(1 for p in roster.values() if p["slot"] != IR)):
        roster[f"filler{spare}"] = player("WR", BENCH, 1.0, season=17.0)
    return roster


def test_an_add_costs_nobody_when_there_is_a_spare_roster_spot():
    ranked = waivers.score_pool(list(full_roster().values()), [free_agent("WR", 14.0, season=238.0)],
                                SLOTS, GAMES)
    assert ranked[0]["drop"] is None and ranked[0]["gain"] == 4.0


def test_adding_a_kicker_pairs_with_your_kicker():
    roster = list(roster_at_capacity().values())             # your kicker projects 8.0
    better = free_agent("K", 11.0, season=187.0)
    ranked = waivers.score_pool(roster, [better], SLOTS, GAMES)
    assert ranked[0]["drop"]["position"] == "K", "swap kicker for kicker, don't carry two"
    assert ranked[0]["gain"] == 0.0, "both play, so the swap is worth nothing"


def test_a_worse_kicker_still_pairs_with_yours_and_gains_nothing():
    roster = list(roster_at_capacity().values())
    worse = free_agent("K", 6.0, season=102.0)
    ranked = waivers.score_pool(roster, [worse], SLOTS, GAMES)
    assert ranked[0]["drop"]["position"] == "K" and ranked[0]["ahead_gain"] == 0.0, "so he's never recommended"


def test_a_kicker_swap_counts_only_the_week_yours_cannot_play():
    """Your kicker's bye three weeks out is the one week a new kicker is worth anything."""
    roster = roster_at_capacity(k=on_team(player("K", 17, 8.0), 30))       # NFL team 30 is off in week 3
    better = on_team(free_agent("K", 11.0, season=187.0), 31)
    ranked = waivers.score_pool(list(roster.values()), [better], SLOTS, GAMES, byes={30: {3}}, week=2)
    assert ranked[0]["drop"]["position"] == "K"
    assert ranked[0]["gain"] == 0.0, "this week both play"
    assert ranked[0]["ahead_gain"] == 11.0 and ranked[0]["weeks_helped"] == 1, "week 3 is worth his average"


def test_a_starter_is_only_dropped_for_someone_better_all_season():
    roster = roster_at_capacity()
    hot_week = free_agent("WR", 20.0, season=85.0)           # big week, poor season
    ranked = waivers.score_pool(list(roster.values()), [hot_week], SLOTS, GAMES)
    dropped = ranked[0]["drop"]
    assert dropped is not None and dropped["season_projection"] <= 85.0, "no starter is worth this much less"


def test_an_injured_player_is_never_the_suggested_drop():
    roster = roster_at_capacity(bench_rb=player("RB", BENCH, 0.0, status="OUT", season=1.0),
                                stashed=player("WR", IR, 0.0, status="INJURY_RESERVE", season=1.0))
    ranked = waivers.score_pool(list(roster.values()), [free_agent("WR", 14.0, season=238.0)], SLOTS, GAMES)
    assert ranked[0]["drop"]["name"] != roster["bench_rb"]["name"]


def test_injured_free_agents_are_skipped():
    pool = [free_agent("RB", 0.0, status="OUT"), free_agent("RB", 0.0, status="INJURY_RESERVE")]
    assert waivers.candidates(list(full_roster().values()), pool, SLOTS, GAMES) == []


def test_bid_scales_with_what_is_left():
    starter = {"gain": 4.0, "season_gain": 60}
    assert waivers.suggest_bid(starter, 100, 0) == (20, 35)
    assert waivers.suggest_bid(starter, 40, 0) == (8, 14), "same player, less budget, smaller bid"


def test_bid_tiers_separate_a_starter_from_a_stash():
    useful = waivers.suggest_bid({"gain": 2.0, "season_gain": 10}, 100, 0)
    speculative = waivers.suggest_bid({"gain": 0.0, "season_gain": 40}, 100, 0)
    nothing = waivers.suggest_bid({"gain": 0.0, "season_gain": 2}, 100, 0)
    assert useful == (8, 15) and speculative == (2, 5) and nothing == (0, 1)
    assert waivers.suggest_bid({"gain": 0.0, "season_gain": 2}, 100, 1) == (1, 1), "minimum bid is respected"


def test_a_confident_jev_score_can_raise_the_bid():
    nothing_on_the_numbers = {"gain": 0.0, "season_gain": 0}
    assert waivers.suggest_bid(nothing_on_the_numbers, 100, 0) == (0, 1)
    confident = dict(nothing_on_the_numbers, jev=0.94, jev_confidence=0.88)
    assert waivers.suggest_bid(confident, 100, 0) == (20, 35), "Jev rates him a starter, so bid like it"
    unsure = dict(nothing_on_the_numbers, jev=0.94, jev_confidence=0.4)
    assert waivers.suggest_bid(unsure, 100, 0) == (0, 1), "a coin-flip judgment does not move money"


def test_projection_band_still_wins_when_it_rates_him_higher():
    clear_upgrade = {"gain": 4.0, "season_gain": 60, "jev": 0.1, "jev_confidence": 0.9}
    assert waivers.suggest_bid(clear_upgrade, 100, 0) == (20, 35)


def test_an_unsure_jev_leaves_the_projection_order_alone():
    """The first live Jev run: every confidence under 0.55, so the order was noise near 0.5."""
    pool = [dict(math_rank=i, jev=0.5 + (0.01 if i % 2 else -0.01), jev_confidence=0.3) for i in range(1, 7)]
    assert [c["math_rank"] for c in sorted(pool, key=waivers.jev_order)] == [1, 2, 3, 4, 5, 6]


def test_a_confident_jev_pick_moves_up_and_nobody_moves_down():
    pool = [dict(math_rank=i, jev=0.5, jev_confidence=0.3) for i in range(1, 6)]
    pool[3].update(jev=0.93, jev_confidence=0.85)          # projection rank 4, Jev is sure about him
    order = [c["math_rank"] for c in sorted(pool, key=waivers.jev_order)]
    assert order == [4, 1, 2, 3, 5]


def test_claude_retries_a_dropped_connection_once():
    import requests
    calls = []
    def flaky(url, json=None, headers=None, timeout=None):
        calls.append(1)
        if len(calls) == 1:
            raise requests.exceptions.ConnectionError("connection reset")
        class Reply:
            status_code = 200
            def json(self):
                return {"content": [{"type": "text", "text": "Bid $3 on Somebody."}]}
        return Reply()
    saved_post, saved_sleep = waivers.requests.post, waivers.time.sleep
    waivers.requests.post, waivers.time.sleep = flaky, lambda seconds: None
    import os
    os.environ["ANTHROPIC_API_KEY"] = "test-key"
    try:
        assert waivers.ask_claude({"week": 2}) == "Bid $3 on Somebody." and len(calls) == 2
    finally:
        waivers.requests.post, waivers.time.sleep = saved_post, saved_sleep
        os.environ["ANTHROPIC_API_KEY"] = ""


def _ask_through(failures):
    """ask_claude against a Messages API that raises each failure in turn, then answers.
    Returns (what it returned, how many calls it made, the waits between them)."""
    import os
    calls, waits = [], []
    def flaky(url, json=None, headers=None, timeout=None):
        calls.append(1)
        if len(calls) <= len(failures):
            raise failures[len(calls) - 1]
        class Reply:
            status_code = 200
            def json(self):
                return {"content": [{"type": "text", "text": "Bid $3 on Somebody."}]}
        return Reply()
    saved_post, saved_sleep = waivers.requests.post, waivers.time.sleep
    waivers.requests.post, waivers.time.sleep = flaky, waits.append
    os.environ["ANTHROPIC_API_KEY"] = "test-key"
    try:
        return waivers.ask_claude({"week": 2}), len(calls), waits
    finally:
        waivers.requests.post, waivers.time.sleep = saved_post, saved_sleep
        os.environ["ANTHROPIC_API_KEY"] = ""


def test_a_call_that_died_while_the_mac_slept_is_tried_again_and_waits_longer_the_second_time():
    import requests
    asleep = requests.exceptions.ReadTimeout("read timed out")          # what a mid-call sleep looks like on waking
    no_wifi = requests.exceptions.ConnectionError("connection reset")   # and the first try after it
    assert _ask_through([asleep, no_wifi]) == ("Bid $3 on Somebody.", 3, [3, 20])
    assert _ask_through([asleep]) == ("Bid $3 on Somebody.", 2, [3])


def test_three_failures_in_a_row_send_the_report_without_a_write_up():
    import requests
    reset = requests.exceptions.ConnectionError("connection reset")
    assert _ask_through([reset, reset, reset]) == (None, 3, [3, 20])


def _with_replies(replies):
    """Swap in a fake Messages API that returns the given bodies in order, recording requests."""
    sent = []
    def fake(url, json=None, headers=None, timeout=None):
        sent.append(json)
        body = replies[len(sent) - 1]
        class Reply:
            status_code = 200
            def json(self):
                return body
        return Reply()
    return fake, sent


def _ask_with(replies):
    import os
    fake, sent = _with_replies(replies)
    saved = waivers.requests.post
    waivers.requests.post = fake
    os.environ["ANTHROPIC_API_KEY"] = "test-key"
    try:
        return waivers.ask_claude({"week": 2}), sent
    finally:
        waivers.requests.post = saved
        os.environ["ANTHROPIC_API_KEY"] = ""


def test_cited_passages_are_joined_as_one_sentence():
    """The early reports broke sentences in two wherever Claude cited a source."""
    cite = {"type": "web_search_result_location", "url": "https://example.com/allen", "title": "Allen news"}
    text, _ = _ask_with([{"stop_reason": "end_turn", "content": [
        {"type": "text", "text": "Skip Allen: he "},
        {"type": "text", "text": "faces a deferred suspension", "citations": [cite]},
        {"type": "text", "text": ", but plays until October."}]}])
    assert text.startswith("Skip Allen: he faces a deferred suspension, but plays until October.")


def test_sources_are_listed_once_each():
    cite = {"type": "web_search_result_location", "url": "https://example.com/a", "title": "Story A"}
    text, _ = _ask_with([{"stop_reason": "end_turn", "content": [
        {"type": "text", "text": "One claim.", "citations": [cite]},
        {"type": "text", "text": " Same source again.", "citations": [cite]}]}])
    assert text.count("https://example.com/a") == 1 and "Sources:" in text and "Story A" in text


def test_a_paused_search_turn_is_resumed_not_truncated():
    """Unhandled, pause_turn is how "working behind Egbuka/Godw" happened."""
    first = {"stop_reason": "pause_turn", "content": [
        {"type": "server_tool_use", "id": "srvtoolu_1", "name": "web_search", "input": {"query": "x"}},
        {"type": "text", "text": "McMillan is working behind Egbuka and Godw"}]}
    rest = {"stop_reason": "end_turn", "content": [{"type": "text", "text": "in, so skip him."}]}
    text, sent = _ask_with([first, rest])
    assert len(sent) == 2, "the paused turn is sent back to continue"
    assert sent[1]["messages"][-1] == {"role": "assistant", "content": first["content"]}
    assert text == "McMillan is working behind Egbuka and Godwin, so skip him."


def test_a_cut_off_write_up_says_so():
    text, _ = _ask_with([{"stop_reason": "max_tokens", "content": [{"type": "text", "text": "Bid $4 on"}]}])
    assert text.endswith("[Cut off: the write-up hit the 16,000-token output limit.]")


def test_jev_log_writes_a_header_once_and_appends_after_it():
    import csv, tempfile
    from pathlib import Path
    path = Path(tempfile.mkdtemp()) / "logs" / "jev_scores.csv"
    judged = [dict(free_agent("WR", 9.0, season=150.0), math_rank=3, jev=0.71, jev_confidence=0.64,
                   usage={"per_game": 7.5, "trend": "rising"})]
    unjudged = [dict(free_agent("RB", 8.0), math_rank=4)]            # its Jev call failed
    assert waivers.log_jev(2, judged + unjudged, path) == 1
    assert waivers.log_jev(3, judged, path) == 1
    rows = list(csv.DictReader(open(path)))
    assert len(rows) == 2 and open(path).read().count("logged_at") == 1
    assert rows[0]["week"] == "2" and rows[0]["jev"] == "0.71" and rows[0]["usage_trend"] == "rising"


def test_nothing_is_logged_when_jev_scored_nobody():
    import tempfile
    from pathlib import Path
    path = Path(tempfile.mkdtemp()) / "jev_scores.csv"
    assert waivers.log_jev(2, [dict(free_agent("WR", 9.0), math_rank=1)], path) == 0 and not path.exists()


def test_command_line_picks_the_ranker():
    assert waivers.ranker_from(["waivers.py", "--email"]) == "math"
    assert waivers.ranker_from(["waivers.py", "--ranker", "jev"]) == "jev"
    assert waivers.ranker_from(["waivers.py", "--email", "--jev-shadow"]) == "shadow"


def test_claude_sees_the_same_numbers_for_the_bench_as_for_free_agents():
    """The Jaguars-vs-Broncos error: the bench had season numbers only, so Claude guessed the rest."""
    roster = list(full_roster().values())
    target, _ = plan(roster, SLOTS, GAMES)
    ranked = waivers.candidates(roster, [free_agent("WR", 14.0)], SLOTS, GAMES)
    ranked[0]["bid"] = (0, 1)
    brief = waivers.brief_for_claude({"week": 2, "budget_left": 100, "rivals": [], "gaps": [],
                                      "candidates": ranked, "drops": [], "roster": roster, "target": target})
    for group in ("my_starters", "my_bench"):
        assert brief[group] and all({"week_projection", "level_per_game"} <= set(p) for p in brief[group])
    assert {"week_projection", "level_per_game"} <= set(brief["available"][0])


def test_a_paused_turn_that_never_finishes_says_so():
    paused = {"stop_reason": "pause_turn", "content": [{"type": "text", "text": "Still searching"}]}
    text, sent = _ask_with([paused] * (1 + waivers.MAX_CONTINUATIONS))
    assert len(sent) == 1 + waivers.MAX_CONTINUATIONS
    assert text.endswith("[Cut off: the web search was still running after the last resume.]")


def _sample_context(**overrides):
    roster = list(full_roster().values())
    target, _ = plan(roster, SLOTS, GAMES)
    ranked = waivers.candidates(roster, [free_agent("WR", 14.0, season=190.0, owned=41.0)], SLOTS, GAMES)
    ranked[0]["bid"] = (8, 15)
    ranked[0]["drop"] = roster[-1]
    context = {"week": 3, "budget": 100, "budget_left": 92, "minimum_bid": 0, "rivals": [100, 88],
               "last_runs": [], "gaps": ["week 5: WR short by 1"], "candidates": ranked,
               "drops": [roster[-1]], "advice": None, "ranked_by": None, "roster": roster, "target": target}
    context.update(overrides)
    return context


def test_html_table_carries_the_numbers_and_the_drop():
    html = waivers.build_html(_sample_context())
    assert "<table" in html and "Drop for him" in html
    assert waivers.LOOKAHEAD == 4 and "Next 5" in html
    for piece in ("$8&ndash;15", "week 5: WR short by 1", "Place claims yourself"):
        assert piece in html, piece


def test_html_escapes_names_and_links_sources():
    context = _sample_context(advice="Bid $3.\n\nSources:\n- Story\n  https://example.com/a")
    context["candidates"][0]["name"] = "A&W <script>"
    html = waivers.build_html(context)
    assert "A&amp;W &lt;script&gt;" in html and "<script>" not in html
    assert '<a href="https://example.com/a"' in html


def test_html_shows_jev_columns_only_when_jev_ran():
    plain = waivers.build_html(_sample_context())
    context = _sample_context()
    context["candidates"][0].update(jev=0.71, jev_confidence=0.64)
    assert ">Jev<" not in plain and ">Jev<" in waivers.build_html(context)


def test_email_carries_both_a_text_and_an_html_version():
    import smtplib, os
    captured = {}
    class FakeSMTP:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def starttls(self, context=None): pass
        def login(self, user, password): pass
        def send_message(self, message):
            captured["types"] = [p.get_content_type() for p in message.walk()]
            captured["subject"] = message["Subject"]
    saved_smtp, saved_env = smtplib.SMTP, os.environ.get("GMAIL_APP_PASSWORD")
    smtplib.SMTP = FakeSMTP
    os.environ.update({"GMAIL_ADDRESS": "x@gmail.com", "GMAIL_APP_PASSWORD": "abcd efgh ijkl mnop"})
    try:
        import mailer
        assert mailer.send("subject", "plain body", "<div>rich body</div>") is True
        assert "text/plain" in captured["types"] and "text/html" in captured["types"]
        assert captured["subject"] == "Fantasy Football: subject", "every email carries the filterable prefix"
    finally:
        smtplib.SMTP = saved_smtp
        os.environ["GMAIL_APP_PASSWORD"] = saved_env or ""


def test_a_shadow_run_prints_the_subject_and_sends_nothing():
    import contextlib, io, os, smtplib
    import mailer
    class NoSMTP:
        def __init__(self, *a, **k): raise AssertionError("a shadow run connected to Gmail")
    saved_smtp, printed = smtplib.SMTP, io.StringIO()
    smtplib.SMTP = NoSMTP
    os.environ["MAIL_TO_LOG"] = "1"
    try:
        with contextlib.redirect_stdout(printed):
            assert mailer.send("Waiver report, week 11", "body", "<p>body</p>") is True, \
                "counts as delivered, so the trade watch marks a proposal as reported"
        assert printed.getvalue() == '(shadow run: would have emailed "Fantasy Football: Waiver report, week 11")\n'
    finally:
        smtplib.SMTP = saved_smtp
        del os.environ["MAIL_TO_LOG"]


def test_faab_spend_falls_back_to_the_claims_when_espn_reports_zero():
    """Your league: the counter said nothing was spent while 27 claims had been paid for."""
    from collections import Counter as Tally
    silent = {"id": 13, "transactionCounter": {"acquisitionBudgetSpent": 0}}
    assert waivers.faab_spent(silent, Tally({13: 15})) == 15
    assert waivers.faab_spent(silent, Tally()) == 0
    counting = {"id": 4, "transactionCounter": {"acquisitionBudgetSpent": 30}}
    assert waivers.faab_spent(counting, Tally({4: 22})) == 30, "trust the larger record"
    assert waivers.faab_spent({"id": 9}, Tally({9: 7})) == 7, "no counter at all"


def test_drop_list_is_bench_only_and_cheapest_first():
    roster = full_roster(bench_wr=player("WR", BENCH, 3.0, season=30.0),
                         bench_rb=player("RB", BENCH, 2.0, season=200.0))
    target, _ = plan(list(roster.values()), SLOTS, GAMES)
    drops = waivers.drop_candidates(list(roster.values()), target, SLOTS, [(2, GAMES)], 2)
    assert drops[0]["name"] == roster["bench_wr"]["name"], "the 30-point season projection goes first"
    assert all(target[p["id"]] == BENCH for p in drops), "never suggests dropping a starter"


WIRE = {"K": 145.0, "D/ST": 115.0, "WR": 40.0, "RB": 30.0, "TE": 35.0, "QB": 200.0}


def test_a_kicker_add_is_paid_for_by_your_kicker_not_a_receiver():
    """Hari's question: adding a kicker should never cost you a bench wideout."""
    roster = spread(full_roster())                      # kicker season 136, bench WR 51, bench RB 34
    horizon = waivers.schedule_ahead(GAMES, {}, 2)
    better_kicker = on_team(free_agent("K", 9.5, season=161.5), 30)
    cut = waivers.drop_for(better_kicker, list(roster.values()), SLOTS, horizon, 2, WIRE)
    assert cut["name"] == roster["k"]["name"], "swap like for like; the wire always has another kicker"


def test_a_kicker_add_still_costs_a_kicker_when_the_wire_is_deep_at_receiver():
    """Even when a bench receiver scores as the cheaper cut, two kickers is the wasted spot."""
    roster = spread(full_roster(bench_wr=player("WR", BENCH, 5.0, season=60.0)))
    deep_at_receiver = dict(WIRE, WR=190.0)             # a better receiver is sitting on the wire
    horizon = waivers.schedule_ahead(GAMES, {}, 2)
    assert waivers.surplus(roster["bench_wr"], deep_at_receiver) < waivers.surplus(roster["k"], deep_at_receiver)
    cut = waivers.drop_for(on_team(free_agent("K", 9.5, season=161.5), 30),
                           list(roster.values()), SLOTS, horizon, 2, deep_at_receiver)
    assert cut["name"] == roster["k"]["name"]


def test_a_receiver_add_never_costs_you_the_only_kicker():
    roster = spread(full_roster())
    horizon = waivers.schedule_ahead(GAMES, {}, 2)
    receiver = on_team(free_agent("WR", 11.0, season=187.0), 30)
    cut = waivers.drop_for(receiver, list(roster.values()), SLOTS, horizon, 2, WIRE)
    assert cut["name"] != roster["k"]["name"], "dropping him would leave the K slot unfillable"


def test_the_second_defense_goes_before_the_receiver():
    """Raw season points said drop Jeudy (97) over the Broncos (118); replacement level says otherwise."""
    roster = spread(full_roster(bench_wr=player("WR", BENCH, 5.0, season=97.0),
                                spare_dst=player("D/ST", BENCH, 6.0, season=118.0)))
    players = list(roster.values())
    target, _ = plan(players, SLOTS, GAMES)
    order = [p["name"] for p in waivers.drop_candidates(players, target, SLOTS, [(2, GAMES)], 2, WIRE)]
    assert order.index(roster["spare_dst"]["name"]) < order.index(roster["bench_wr"]["name"])


def test_surplus_measures_a_player_against_his_own_position():
    kicker, receiver = player("K", BENCH, 8.0, season=136.0), player("WR", BENCH, 5.0, season=97.0)
    assert waivers.surplus(kicker, WIRE) < 0 < waivers.surplus(receiver, WIRE)
    assert waivers.replacement_level([free_agent("K", 9.0, season=150.0),
                                      free_agent("K", 8.0, season=120.0)]) == {"K": 150.0}


def test_never_drops_your_only_kicker():
    roster = full_roster(k=player("K", 17, 8.0, season=5.0))   # cheapest player on the roster by far
    players = list(roster.values())
    target, _ = plan(players, SLOTS, GAMES)
    names = [p["name"] for p in waivers.drop_candidates(players, target, SLOTS, [(2, GAMES)], 2)]
    assert roster["k"]["name"] not in names, "dropping him would leave the K slot unfillable"


def spread(roster):
    """The shared fixture puts everyone on one NFL team; byes need them on different ones."""
    for pro_team, person in enumerate(roster.values(), start=1):
        person["pro_team"] = pro_team
    return roster


def on_team(person, pro_team):
    person["pro_team"] = pro_team
    return person


def test_a_bye_hole_is_reported_with_who_could_fill_it():
    roster = spread(full_roster())                       # the only QB is on NFL team 1
    byes = {roster["qb"]["pro_team"]: {4}, 31: {4}}
    horizon = waivers.schedule_ahead(GAMES, byes, 2)
    pool = [on_team(free_agent("QB", 15.0, season=255.0), 30),
            on_team(free_agent("QB", 16.0, season=270.0), 31)]   # better, but off the same week
    gaps = waivers.bye_gaps(list(roster.values()), SLOTS, horizon, 2, pool)
    assert len(gaps) == 1 and gaps[0].startswith("week 4: QB short by 1")
    assert pool[0]["name"] in gaps[0] and pool[1]["name"] not in gaps[0], "he's on bye that week too"


def test_the_flex_spot_covers_a_receiver_bye():
    """The old check counted positions one at a time, so it reported this as a hole."""
    roster = spread(full_roster())
    byes = {roster["wr1"]["pro_team"]: {4}, roster["wr2"]["pro_team"]: {4}}
    horizon = waivers.schedule_ahead(GAMES, byes, 2)
    assert waivers.bye_gaps(list(roster.values()), SLOTS, horizon, 2) == []


def test_covering_a_bye_outranks_a_player_who_adds_nothing():
    roster = spread(full_roster())
    byes = {roster["qb"]["pro_team"]: {4}}
    cover = on_team(free_agent("QB", 15.0, season=255.0, owned=3.0), 30)
    popular = on_team(free_agent("WR", 1.0, season=17.0, owned=60.0), 31)
    ranked = waivers.score_pool(list(roster.values()), [popular, cover], SLOTS, GAMES, byes, 2)
    assert ranked[0]["name"] == cover["name"], "the week-4 fill-in ranks first despite 3% rostered"
    assert ranked[0]["gain"] == 0.0 and ranked[0]["ahead_gain"] == 15.0


def test_a_free_agent_on_bye_that_week_gets_no_credit_for_it():
    roster = spread(full_roster())
    byes = {roster["qb"]["pro_team"]: {4}, 30: {4}}
    same_bye = on_team(free_agent("QB", 15.0, season=255.0), 30)
    ranked = waivers.score_pool(list(roster.values()), [same_bye], SLOTS, GAMES, byes, 2)
    assert ranked[0]["ahead_gain"] == 0.0


def test_never_drops_the_player_covering_a_bye():
    roster = spread(full_roster(backup_qb=player("QB", BENCH, 1.0, season=10.0)))   # the cheapest player
    players = list(roster.values())
    horizon = waivers.schedule_ahead(GAMES, {roster["qb"]["pro_team"]: {4}}, 2)
    target, _ = plan(players, SLOTS, GAMES)
    names = [p["name"] for p in waivers.drop_candidates(players, target, SLOTS, horizon, 2)]
    assert roster["backup_qb"]["name"] not in names, "he's the only QB playing in week 4"
    assert names, "other bench players can still go"


def test_repeat_value_earns_real_money_and_a_one_week_fill_does_not():
    recurring = {"gain": 0.0, "season_gain": 0, "ahead_gain": 7.0, "weeks_helped": 3}
    one_week = {"gain": 0.0, "season_gain": 0, "ahead_gain": 14.7, "weeks_helped": 1}
    assert waivers.suggest_bid(recurring, 100, 0) == (8, 15)
    assert waivers.suggest_bid(one_week, 100, 0) == (2, 5), "a bye rental: cheap, even if the week is big"


def test_a_streamer_for_this_week_only_is_also_cheap():
    streamer = {"gain": 2.5, "season_gain": 0, "ahead_gain": 2.5, "weeks_helped": 1}
    assert waivers.suggest_bid(streamer, 100, 0) == (2, 5)


def test_a_kicker_or_defense_upgrade_is_still_bid_cheap():
    """Now that they count as upgrades, a good matchup must not earn starter money."""
    defense = {"position": "D/ST", "gain": 4.0, "season_gain": 20, "ahead_gain": 9.0, "weeks_helped": 3}
    assert waivers.suggest_bid(defense, 100, 0) == (2, 5)
    assert waivers.suggest_bid(dict(defense, position="WR"), 100, 0)[1] > 5, "the same numbers at WR do"


def test_claude_is_optional(monkey_key=None):
    import os
    saved = os.environ.pop("ANTHROPIC_API_KEY", None)
    os.environ["ANTHROPIC_API_KEY"] = ""
    try:
        assert waivers.ask_claude({"week": 2}) is None, "no key means no write-up, not a crash"
    finally:
        if saved:
            os.environ["ANTHROPIC_API_KEY"] = saved


def test_a_kicker_swap_still_counts_the_week_the_new_one_cannot_play():
    """Interchangeable only while both play: his bye with yours gone is a real hole."""
    yours, his = on_team(player("K", 17, 8.0), 1), on_team(free_agent("K", 11.0), 31)
    everyone = {team: None for team in range(1, 35)}
    horizon = [(2, everyone), (3, {team: None for team in everyone if team != 31})]    # he's off in week 3
    assert waivers.kicker_weeks([3.0, -8.0], [yours], his, yours, horizon) == [0.0, -8.0]


def test_pool_log_keeps_every_free_agent_with_what_the_report_made_of_him():
    """auctions.py matches each night's claims to these rows, players the report skipped included."""
    import csv, tempfile
    from pathlib import Path
    path = Path(tempfile.mkdtemp()) / "logs" / "pool.csv"
    roster = list(full_roster().values())                         # weakest starter projects 10.0
    listed = free_agent("WR", 14.0, season=238.0, owned=31.0, change=12.5)
    skipped = free_agent("RB", 1.0, season=17.0, owned=2.0, change=-0.4)
    hurt = free_agent("TE", 0.0, status="OUT", owned=6.0, change=3.1)      # never scored
    pool = [listed, skipped, hurt]
    scored = waivers.score_pool(roster, pool, SLOTS, GAMES)
    scored[0]["bid"] = (8, 15)                                    # only the shortlist carries bids
    assert waivers.log_pool(5, pool, scored, path) == 3
    assert waivers.log_pool(6, pool[:1], scored, path) == 1      # the next run appends
    rows = list(csv.DictReader(open(path)))
    assert len(rows) == 4 and open(path).read().count("logged_at") == 1
    assert rows[0]["name"] == listed["name"] and rows[0]["percent_change"] == "12.5"
    assert rows[0]["band"] == "0" and rows[0]["bid"] == "8-15"
    assert rows[1]["band"] == "3" and rows[1]["bid"] == "", "scored, not worth money, not on the list"
    assert rows[2]["band"] == "" and rows[2]["percent_owned"] == "6.0", "in the pool, never scored"
    assert rows[3]["week"] == "6"


def test_both_reports_show_the_add_rate():
    """ESPN's change in rostered %, the earliest sign of who rivals are about to bid on."""
    context = _sample_context()
    context["candidates"][0]["percent_change"] = 18.4
    assert "+/-" in waivers.build_report(context) and "+18.4" in waivers.build_report(context)
    html = waivers.build_html(context)
    assert ">+/-<" in html and "+18.4" in html


def test_report_reads_correctly_without_advice():
    roster = list(full_roster().values())
    ranked = waivers.candidates(roster, [free_agent("WR", 14.0, season=190.0, owned=41.0)], SLOTS, GAMES)
    ranked[0]["bid"] = waivers.suggest_bid(ranked[0], 100, 0)
    target, _ = plan(roster, SLOTS, GAMES)
    report = waivers.build_report({
        "week": 3, "budget": 100, "budget_left": 100, "minimum_bid": 0, "rivals": [100, 88, 100],
        "last_runs": [], "gaps": [], "candidates": ranked, "drops": waivers.drop_candidates(roster, target, SLOTS, [(2, GAMES)], 2),
        "advice": None, "roster": roster, "target": target})
    assert "Waiver report" in report and "$20-35" in report
    assert "Place claims yourself" in report, "the report must never imply it bid for you"
    assert "No write-up" not in report, "with no API key, a report without a write-up is the normal one"


def test_both_reports_say_so_when_the_write_up_failed():
    context = _sample_context(advice=None)
    assert "No write-up" not in waivers.build_report(context) and "No write-up" not in waivers.build_html(context)
    context["advice_failed"] = True
    assert "No write-up today: the call to Claude failed" in waivers.build_report(context)
    assert "No write-up today: the call to Claude failed" in waivers.build_html(context)
    context.update(advice="Bid $3 on Somebody.", advice_failed=False)
    assert "No write-up" not in waivers.build_report(context) and "Bid $3 on Somebody." in waivers.build_html(context)



def test_this_weeks_hole_is_named_with_who_is_out():
    """Week 11: Myers (Seattle) and Fannin (Cleveland) were both off, and the write-up was never told."""
    from espn import PRO_TEAMS
    roster = full_roster(k=player("K", 17, 8.0, bye=True), te=player("TE", 6, 9.0, status="OUT"))
    assert waivers.holes_now(list(roster.values()), SLOTS, GAMES, 11) == [
        f"week 11: TE short by 1 — {roster['te']['name']} ({PRO_TEAMS.get(1, '?')}, out)",
        f"week 11: K short by 1 — {roster['k']['name']} ({PRO_TEAMS.get(0, '?')}, bye)"]
    assert waivers.holes_now(list(full_roster().values()), SLOTS, GAMES, 11) == []


def test_both_reports_and_the_brief_carry_this_weeks_holes():
    hole = "week 3: K short by 1 — Jason Myers (SEA, bye)"
    context = _sample_context(holes_now=[hole])
    text, html = waivers.build_report(context), waivers.build_html(context)
    assert "Lineup holes this week:" in text and hole in text
    assert "Lineup holes this week<" in html and "Jason Myers (SEA, bye)" in html
    assert waivers.brief_for_claude(context)["lineup_holes_this_week"] == [hole]
    assert "Lineup holes this week" not in waivers.build_report(_sample_context()), "none, so no block"
    prompt = _prompt_sent()
    assert "lineup_holes_this_week names each starting slot" in prompt
    assert "compare him with zero, not with that starter" in prompt
    assert "Kickers are interchangeable in any week both play" in prompt


def test_a_kicker_add_drops_your_kicker_even_when_his_bye_comes_later():
    """Week 11: Jason Sanders (off in week 13) was offered for Treylon Burks, so you'd carry two kickers."""
    roster = roster_at_capacity(k=on_team(player("K", 17, 8.0), 30))
    this_week = {team: game for team, game in GAMES.items() if team != 30}        # yours is off this week
    sanders = on_team(free_agent("K", 9.9, season=131.0), 31)                     # and he is off in week 4
    ranked = waivers.score_pool(list(roster.values()), [sanders], SLOTS, this_week, byes={30: {2}, 31: {4}}, week=2)
    assert ranked[0]["drop"]["name"] == roster["k"]["name"], "a kicker pays for a kicker, never a receiver"
    assert ranked[0]["gain"] == 9.9 and ranked[0]["ahead_gain"] < ranked[0]["gain"], "his own bye counts against him"


CALLS_TEXT = ('Add Cade Otton: Fannin is off.\n\n```json\n{"calls": [{"player": "Cade Otton", "call": "add", '
              '"drop": "Treylon Burks", "why": "Fannin is on bye"}, {"player": "Jets D/ST", "call": "skip", '
              '"why": "Eagles are better"}, {"player": "Somebody", "call": "maybe"}]}\n```'
              '\n\nSources:\n- Story\n  https://example.com/a')


def test_the_write_ups_calls_are_read_and_taken_out_of_its_text():
    import contextlib, io
    prose, calls = waivers.split_calls(CALLS_TEXT)
    assert prose == "Add Cade Otton: Fannin is off.\n\nSources:\n- Story\n  https://example.com/a"
    assert set(calls) == {"Cade Otton", "Jets D/ST"}, "a call that isn't add, bid or skip is ignored"
    assert waivers.split_calls("No block here.") == ("No block here.", {})
    assert waivers.split_calls(None) == (None, {})
    with contextlib.redirect_stdout(io.StringIO()) as log:
        broken = waivers.split_calls('Text.\n\n```json\n{"calls": [oops]}\n```')
    assert broken == ("Text.", {}) and "didn't parse" in log.getvalue(), "the table falls back to the numbers"


def test_a_call_is_held_to_what_the_table_knows():
    """Nov 18: the write-up said to bid $5 on Cade Otton, who had cleared waivers and cost nothing."""
    otton = dict(free_agent("TE", 6.0), name="Cade Otton", free_now=True, bid=(3, 5))
    hutchinson = dict(free_agent("WR", 9.4), name="Xavier Hutchinson", bid=(8, 18))
    demercado = dict(free_agent("RB", 7.6), name="Emari Demercado", bid=(18, 31))
    jets = dict(free_agent("D/ST", 6.2), name="Jets D/ST", bid=(1, 4))
    unnamed = dict(free_agent("WR", 3.0), name="Not Mentioned", bid=(0, 1))
    waivers.attach_calls([otton, hutchinson, demercado, jets, unnamed], {
        "Cade Otton": {"call": "bid", "bid": 5, "drop": "Treylon Burks", "why": "Fannin is on bye"},
        "xavier hutchinson": {"call": "bid", "bid": "$11"},
        "Emari Demercado": {"call": "add", "drop": "Keaton Mitchell"},
        "Jets D/ST": {"call": "skip", "drop": "Giants D/ST"}})
    assert otton["call"] == {"call": "add", "bid": None, "drop": "Treylon Burks", "why": "Fannin is on bye"}
    assert hutchinson["call"]["bid"] == 11, "matched whatever the case, and the dollar sign read"
    assert demercado["call"]["call"] == "bid" and demercado["call"]["bid"] == 18, "on waivers: a bid, the low end"
    assert jets["call"]["drop"] is None, "a skip drops nobody"
    assert "call" not in unnamed


def test_this_weeks_fill_ins_sit_under_the_players_worth_more():
    """Week 11: two kickers covering Myers' bye headed the table and read as the picks of the day."""
    context = _sample_context()
    keeper = context["candidates"][0]                          # helps this week and all season
    kicker = dict(free_agent("K", 10.1), name="Cairo Santos", gain=10.1, ahead_gain=10.1, weeks_helped=1,
                  season_gain=0, bid=(1, 4), drop=None)
    context["candidates"] = [kicker, keeper]
    assert waivers.this_week_only(kicker) and not waivers.this_week_only(keeper)
    assert not waivers.this_week_only(dict(kicker, role={"touches": 9})), "a next man up is never a rental"
    text, html = waivers.build_report(context), waivers.build_html(context)
    assert text.index(keeper["name"]) < text.index(waivers.THIS_WEEK_ONLY) < text.index("Cairo Santos")
    assert html.index(keeper["name"]) < html.index(waivers.THIS_WEEK_ONLY) < html.index("Cairo Santos")
    assert ">Call<" not in html and "CALL" not in text, "no write-up, no call column"
    context["candidates"] = [kicker]
    assert waivers.THIS_WEEK_ONLY in waivers.build_report(context), "the heading stands even with nothing above it"


def test_the_table_shows_each_call_with_adds_and_bids_first():
    context = _sample_context()
    first = context["candidates"][0]
    skipped = dict(free_agent("WR", 12.0, season=170.0), name="Skipped Guy", gain=2.0, ahead_gain=4.0,
                   weeks_helped=2, season_gain=5, bid=(3, 5), drop=None)
    context["candidates"] = [skipped, first]
    waivers.attach_calls(context["candidates"], {
        "Skipped Guy": {"call": "skip", "why": "the other one is better"},
        first["name"]: {"call": "bid", "bid": 9, "drop": "Somebody Else", "why": "news: <starter> is out"}})
    text, html = waivers.build_report(context), waivers.build_html(context)
    assert text.index(first["name"] + " ") < text.index("Skipped Guy") and html.index(first["name"] + "<") < html.index("Skipped Guy")
    row = next(line for line in text.splitlines() if line.startswith(first["name"] + " "))
    assert "bid $9" in row and "$8-15" in row and "Somebody Else" in row, "the call beside the range it leaves"
    assert "    the other one is better" in text
    assert ">Call<" in html and "bid $9" in html and "news: &lt;starter&gt; is out" in html


def test_the_subject_counts_the_write_ups_adds_and_bids():
    a, b, c = {"gain": 1.0}, {"gain": 0.0}, {"gain": 3.0}
    assert waivers.headline([a, b, c]) == "2 worth a claim", "no calls: the table's count, as before"
    a["call"], b["call"], c["call"] = {"call": "add"}, {"call": "skip"}, {"call": "bid"}
    assert waivers.headline([a, b, c]) == "1 to add, 1 to bid on"
    a["call"] = c["call"] = {"call": "skip"}
    assert waivers.headline([a, b, c]) == "nothing to add or bid on"
    assert waivers.headline([]) == "nothing worth a claim"


def test_claude_is_asked_for_a_call_on_every_row_and_told_the_table_shows_them():
    prompt = _prompt_sent()
    assert "```json" in prompt and '"call": "add" (a free agent)' in prompt
    assert "the text must agree with those calls" in prompt and "doesn't count toward the three" in prompt
    brief = waivers.brief_for_claude(_sample_context())
    assert brief["available"][0]["helps_only_this_week"] is False


def test_main_puts_the_write_ups_calls_in_the_table_and_the_subject():
    import contextlib, io
    context = _sample_context()
    name = context["candidates"][0]["name"]
    reply = (f"Bid on {name}.\n\n```json\n" + '{"calls": [{"player": "' + name + '", "call": "bid", "bid": 9, '
             '"why": "starter out"}]}\n```')
    sent = []
    saved = (waivers.ESPN.__dict__["from_env"], waivers.gather, waivers.ask_claude, waivers.mailer.send)
    waivers.ESPN.from_env = lambda: None
    waivers.gather = lambda espn, ranker: context
    waivers.ask_claude = lambda brief: reply
    waivers.mailer.send = lambda subject, body, html=None: sent.append((subject, body, html)) or True
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            waivers.main(email=True)
    finally:
        waivers.ESPN.from_env, waivers.gather, waivers.ask_claude, waivers.mailer.send = saved
    subject, body, html = sent[0]
    assert subject == "Waivers week 3: 1 to bid on"
    assert "bid $9" in body and "bid $9" in html and "starter out" in html
    assert "```" not in body and "```" not in html and f"Bid on {name}." in body


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
