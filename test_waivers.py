"""Tests for the waiver report. Run: python3 test_waivers.py"""
import sys

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
    ranked = waivers.candidates(roster, pool, SLOTS, GAMES)
    assert round(ranked[0]["gain"], 1) == 4.0 and ranked[0]["would_start"]
    assert ranked[1]["gain"] == 0.0 and not ranked[1]["would_start"], "a bench-quality add gains nothing"


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
    ranked = {c["position"]: c for c in waivers.candidates(roster, [backup_qb, flex_help], SLOTS, GAMES)}
    assert ranked["QB"]["season_gain"] == 0, "a season total is not an upgrade if he never starts"
    assert ranked["WR"]["season_gain"] > 0


def test_a_better_kicker_gains_what_he_outprojects_yours_by():
    ranked = waivers.candidates(list(full_roster().values()), [free_agent("K", 11.0)], SLOTS, GAMES)
    assert round(ranked[0]["gain"], 1) == 3.0      # your kicker projects 8.0


def test_a_kicker_is_worth_plenty_when_yours_is_on_bye():
    roster = full_roster(k=player("K", 17, 8.0, bye=True))
    ranked = waivers.candidates(list(roster.values()), [free_agent("K", 11.0)], SLOTS, GAMES)
    assert round(ranked[0]["gain"], 1) == 11.0


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
    assert ranked[0]["gain"] == 3.0


def test_a_worse_kicker_costs_points_instead_of_taking_a_bench_spot():
    roster = list(roster_at_capacity().values())
    worse = free_agent("K", 6.0, season=102.0)
    ranked = waivers.score_pool(roster, [worse], SLOTS, GAMES)
    assert ranked[0]["drop"]["position"] == "K" and ranked[0]["ahead_gain"] < 0, "so he's never recommended"


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
        assert brief[group] and all({"week_projection", "season_projection"} <= set(p) for p in brief[group])
    assert {"week_projection", "season_projection"} <= set(brief["available"][0])


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
        def send_message(self, message): captured["types"] = [p.get_content_type() for p in message.walk()]
    saved_smtp, saved_env = smtplib.SMTP, os.environ.get("GMAIL_APP_PASSWORD")
    smtplib.SMTP = FakeSMTP
    os.environ.update({"GMAIL_ADDRESS": "x@gmail.com", "GMAIL_APP_PASSWORD": "abcd efgh ijkl mnop"})
    try:
        import mailer
        assert mailer.send("subject", "plain body", "<div>rich body</div>") is True
        assert "text/plain" in captured["types"] and "text/html" in captured["types"]
    finally:
        smtplib.SMTP = saved_smtp
        os.environ["GMAIL_APP_PASSWORD"] = saved_env or ""


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
