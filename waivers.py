"""Step 4: the daily waiver report.

Read-only. It ranks the free agent pool by what each player would add to your starting
lineup, suggests a bid range, asks Claude to turn that into recommendations, and emails
it. It never places a claim.

Usage:
  python3 waivers.py                  print the report
  python3 waivers.py --email          print it and send it
  python3 waivers.py --ranker jev     rank the pool with Jev instead of projections
  python3 waivers.py --jev-shadow     score with Jev and log it, but rank and email as normal
  python3 waivers.py --rate-shadow    also rank the wire with rates.py and by wins, and log it (shadow.py);
                                      on Tuesdays, with --email, also email the comparison
"""
import csv
import html as html_escape
import json
import math
import re
import os
import sys
import textwrap
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

import requests
from dotenv import load_dotenv

import bids
import jev
import mailer
import roles
import slack
import snaps
import values
from espn import BENCH, ESPN, IR, PRO_TEAMS, SLOT_NAMES, SLOT_ORDER, ESPNError, parse_roster_entry
from lineup import CANT_PLAY, expected_points, plan, starters_total
from values import GAMES_PER_SEASON, season_pace

JEV_LOG = Path(__file__).with_name("logs") / "jev_scores.csv"
JEV_LOG_FIELDS = ["logged_at", "week", "player_id", "name", "position", "jev", "jev_confidence", "math_rank",
                  "projection", "season_projection", "percent_owned", "usage_per_game", "usage_trend"]
POOL_LOG = Path(__file__).with_name("logs") / "pool.csv"
POOL_LOG_FIELDS = ["logged_at", "week", "player_id", "name", "position", "percent_owned", "percent_change",
                   "projection", "band", "bid"]
MODEL = "claude-sonnet-5"
# Sonnet 5 thinks by default, and max_tokens caps thinking and text together, so a
# budget sized for the text alone runs out partway through. It's a ceiling, not a spend.
MAX_TOKENS = 16000
MAX_CONTINUATIONS = 3  # a paused web-search turn is resumed, up to this many times
POOL_SIZE = 200     # free agents to score, most widely rostered first. At 75 the list stopped near 3%
                    # rostered, above players this league went on to pay $28 and $11 for.
SHORTLIST = 8       # how many make the report
PER_POSITION = 2    # at most this many of any one position, so the list never becomes all QBs
ROLE_SLOTS = 3      # next men up (roles.py) listed on top of that, whatever they add to your lineup
LOOKAHEAD = 4          # weeks after this one that ranking, holes and drops plan for, byes included


def season_view(players):
    """The same players scored by season pace (values.py), with nobody on a bye.

    Used for the rest-of-season comparison: raw season totals can't be compared across
    positions, but what a player adds to your season-long lineup can.
    """
    return [dict(p, projection=season_pace(p)) for p in players]


def replacement_level(pool):
    """The best freely available season pace at each position."""
    best = {}
    for candidate in pool:
        season = season_pace(candidate)
        if season > best.get(candidate["position"], 0):
            best[candidate["position"]] = season
    return best


def surplus(player, replacement):
    """What dropping him actually costs: his season value above the best free agent at his spot.

    This is why a second kicker goes before a bench receiver. Kickers are interchangeable, so
    the incumbent's surplus is near zero even when his raw projection is the higher number.
    """
    return season_pace(player) - replacement.get(player["position"], 0)


def schedule_ahead(games, byes, week, lookahead=LOOKAHEAD):
    """[(week, teams playing)] for this week and the next LOOKAHEAD (trades look further).

    This week uses the real schedule. For later weeks, all that matters is who is on bye.
    """
    teams = set(byes) | set(games)
    horizon = [(week, games)]
    for future in range(week + 1, week + 1 + lookahead):
        horizon.append((future, {team: None for team in teams if future not in byes.get(team, set())}))
    return horizon


def projected_for(players, week, this_week):
    """This week's real projection, or his level (values.py) for a later week.

    A back filling in for a lead runner who is down (roles.py) counts for more in the weeks that covers.
    Nobody is locked in a later week. Carrying this week's locks forward froze everyone who had
    already played in the slot he was in, so from Thursday night to Tuesday morning the report
    saw holes your bench could fill and missed ones it couldn't.
    """
    if week != this_week:
        players = [dict(p, projection=season_pace(p) / GAMES_PER_SEASON, locked=False) for p in players]
    return [dict(p, projection=max(p["projection"] or 0.0, p["fill_in"][week])) if week in p.get("fill_in", ())
            else p for p in players]


def lineup_value_and_holes(players, slot_counts, games):
    """(points your best lineup scores, starting slots nobody who plays can fill) in one pass."""
    lineup, _ = plan(players, slot_counts, games)
    by_id = {p["id"]: p for p in players}
    value, filled = 0.0, Counter()
    for player_id, slot in lineup.items():
        if slot in (BENCH, IR):
            continue
        points = expected_points(by_id[player_id], games)
        value += points
        if points > 0:
            filled[slot] += 1
    holes = sum(max(0, count - filled[slot]) for slot, count in slot_counts.items()
                if count and slot not in (BENCH, IR))
    return value, holes


def shortfalls(players, slot_counts, games):
    """{slot: how many short} for starting slots your best lineup can't fill with someone who plays.

    Uses the lineup planner, so flex coverage counts: a running back on the bench does
    cover a receiver's bye if he can slide into the flex spot.
    """
    lineup, _ = plan(players, slot_counts, games)
    by_id = {p["id"]: p for p in players}
    playing = Counter(slot for pid, slot in lineup.items()
                      if slot not in (BENCH, IR) and expected_points(by_id[pid], games) > 0)
    return {slot: count - playing[slot] for slot, count in slot_counts.items()
            if count and slot not in (BENCH, IR) and playing[slot] < count}


def best_lineup_value(players, slot_counts, games):
    return starters_total(players, plan(players, slot_counts, games)[0], games)


def candidates(roster, pool, slot_counts, games):
    """The projection ranking, shortlisted. The default."""
    return shortlist_from(score_pool(roster, pool, slot_counts, games))


def lock_started(pool, games, now):
    """A free agent whose game this week has kicked off can add nothing until next week.

    Without this, Monday's report still counted points from games that were already over.
    """
    for free_agent in pool:
        kickoff = (games.get(free_agent["pro_team"]) or (None, None))[1]
        free_agent["locked"] = bool(kickoff and kickoff <= now)


def held(candidate):
    """True for a kicker, a defense or a one-week fill-in who does nothing for you this week.

    There will be one like him on the wire in the week you need him, and until then he costs a
    roster spot. So he is listed in that week and not before. A player who would also make your
    lineup better over the season is not a fill-in, and neither is a next man up.
    """
    if candidate["gain"] > 0 or candidate.get("role"):
        return False
    later_fill_in = candidate.get("weeks_helped") == 1 and candidate["season_gain"] <= 0
    return candidate["position"] in STREAM_ONLY or later_fill_in


def shortlist_from(scored):
    """Top of the list, capped per position so it never becomes eight of one thing.

    Only players worth a bid are listed (bid_band): when nobody adds to your lineup, the list used
    to fill up with the most widely rostered free agents, each at $0-1, and that is how a quarterback
    you had no use for sat on it for weeks. A next man up is listed even when he adds nothing to
    your lineup: the league bids on him the night after the game that flagged him, and by the time
    he looks good on paper he is gone.
    """
    shortlist, seen = [], Counter()
    for candidate in scored:
        if held(candidate) or bid_band(candidate) == 3:
            continue
        if seen[candidate["position"]] < PER_POSITION:
            shortlist.append(candidate)
            seen[candidate["position"]] += 1
        if len(shortlist) == SHORTLIST:
            break
    listed = {c["id"] for c in shortlist}
    stepping_in = sorted((c for c in scored if c.get("role") and c["id"] not in listed),
                         key=lambda c: -c["role"]["touches"])
    return shortlist + stepping_in[:ROLE_SLOTS]


def swap_value(roster, candidate, dropped, slot_counts, horizon, this_week, bases):
    """What adding him and dropping that player is worth, week by week across the window.

    A roster spot has to come from somewhere, so an add is only worth what it adds after the
    drop it forces. That is what makes a second kicker score badly: the lineup gains nothing
    and the dropped player's value is gone.
    """
    after = [p for p in roster if dropped is None or p["id"] != dropped["id"]] + [candidate]
    return [lineup_value_and_holes(projected_for(after, week, this_week), slot_counts, games)[0] - base
            for (week, games), (base, _) in zip(horizon, bases)]


def _kicker_plays(players, games):
    return any(p["position"] == "K" and p["injury_status"] not in CANT_PLAY and p["pro_team"] in games
               for p in players)


def kicker_weeks(by_week, roster, kicker, dropped, horizon):
    """A kicker for a kicker is worth nothing in a week both play (STEADY_SLOTS in lineup.py).

    What's left is real: a week yours can't play and he can, or a week he can't and yours could.
    """
    after = [p for p in roster if dropped is None or p["id"] != dropped["id"]] + [kicker]
    return [0.0 if _kicker_plays(roster, games) and _kicker_plays(after, games) else points
            for points, (_, games) in zip(by_week, horizon)]


def score_pool(roster, pool, slot_counts, games, byes=None, week=0):
    """Every free agent ranked by what he adds over the next few weeks, after the drop he costs.

    The window counts byes on both sides: your players' byes open the holes he could fill,
    and his own bye means he fills nothing that week.
    """
    horizon = schedule_ahead(games, byes, week) if byes is not None else [(week, games)]
    bases = [lineup_value_and_holes(projected_for(roster, w, week), slot_counts, g) for w, g in horizon]
    every_team = {p["pro_team"]: (0, None) for p in roster + pool}
    season_base = best_lineup_value(season_view(roster), slot_counts, every_team)

    target, _ = plan(roster, slot_counts, games)
    replacement = replacement_level(pool)
    capacity = sum(n for slot, n in slot_counts.items() if n and slot != IR)
    spare_spot = len([p for p in roster if target[p["id"]] != IR]) < capacity

    scored = []
    for free_agent in pool:
        if free_agent["injury_status"] in CANT_PLAY:
            continue
        dropped = None if spare_spot else drop_for(free_agent, roster, slot_counts, horizon, week, replacement)
        by_week = swap_value(roster, free_agent, dropped, slot_counts, horizon, week, bases)
        if free_agent["position"] == "K":
            by_week = kicker_weeks(by_week, roster, free_agent, dropped, horizon)
        season_gain = best_lineup_value(season_view(roster + [free_agent]), slot_counts, every_team) - season_base
        scored.append(dict(free_agent,
                           gain=round(by_week[0], 1),
                           ahead_gain=round(sum(by_week), 1),
                           by_week=by_week,
                           weeks_helped=sum(1 for points in by_week if points > 0),
                           season_gain=round(season_gain),
                           drop=dropped,
                           would_start=by_week[0] > 0))
    scored.sort(key=lambda c: (-c["ahead_gain"], -c["season_gain"], -c["percent_owned"]))
    for rank, candidate in enumerate(scored, start=1):
        candidate["math_rank"] = rank
    return scored


BANDS = [(0.20, 0.35), (0.08, 0.15), (0.02, 0.05)]   # starter, useful piece, stash: used until
MIN_CLAIMS = 10        # this many claims of his kind have been seen; then your league's own prices take over
# How often a bid should win, low end and high end, as a share of the past claims of his kind
# (bids.py). Losing one claim in four on a player who would start for you, and one in two on a
# stash, is the target you set; the high end is the next step up, for when you can't miss.
STARTER, STASH = (0.75, 0.90), (0.50, 0.75)
ODDS = {0.50: "half", 0.75: "three in four", 0.90: "nine in ten"}
STREAM_ONLY = {"K", "D/ST"}   # one slot, no flex, another one always available


def _projection_band(candidate):
    gain, season_gain = candidate["gain"], candidate["season_gain"]
    ahead = candidate.get("ahead_gain", gain)
    # A one-week fill-in is worth having but not worth real money; FAAB is for repeat value.
    one_week_fill = candidate.get("weeks_helped") == 1 and season_gain <= 0
    if gain >= 3.0 and season_gain > 0:
        return 0
    if (gain >= 1.5 or ahead >= 6.0) and not one_week_fill:
        return 1
    if gain > 0 or ahead > 0 or season_gain >= 15:
        return 2
    return 3


def _jev_band(candidate):
    """Jev's judgment can raise a bid, but only when it is confident in it."""
    score, confidence = candidate.get("jev"), candidate.get("jev_confidence") or 0
    if score is None:
        return 3
    if score >= 0.90 and confidence >= 0.70:
        return 0
    if score >= 0.75 and confidence >= 0.60:
        return 1
    if score >= 0.60 and confidence >= 0.50:
        return 2
    return 3


def percentile(values, fraction):
    """The value below which that fraction of the sample sits."""
    ordered = sorted(values)
    if not ordered:
        return 0
    spot = fraction * (len(ordered) - 1)
    low = int(spot)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (spot - low)


def bid_band(candidate):
    """0 starter, 1 useful piece, 2 stash, 3 not worth money."""
    band = min(_projection_band(candidate), _jev_band(candidate))
    if candidate.get("role"):
        band = min(band, 2)   # worth a stash whatever your lineup says; who you bid against is bids.kind_of
    if candidate.get("position") in STREAM_ONLY:
        band = max(band, 2)   # there's always another one on the wire, so never real money
    return band


def winning_bid(prices, share):
    """The smallest whole-dollar bid that would have beaten at least that share of these prices."""
    ordered = sorted(prices)
    return int(ordered[math.ceil(share * len(ordered)) - 1]) + 1


def suggest_bid(candidate, budget_left, minimum_bid, past=None):
    """What to bid: what past claims of his kind took to win, at the odds his band calls for.

    past: {kind: [prices to beat]} from bids.history. Until this league has priced enough claims
    of his kind, falls back to a share of the budget.
    """
    band = bid_band(candidate)
    if band == 3:
        return minimum_bid, max(minimum_bid, 1)
    prices = (past or {}).get(bids.kind_of(candidate), ())
    if len(prices) >= MIN_CLAIMS:
        low, high = (winning_bid(prices, share) for share in (STASH if band == 2 else STARTER))
    else:
        low, high = round(budget_left * BANDS[band][0]), round(budget_left * BANDS[band][1])
    return max(minimum_bid, min(low, budget_left)), max(minimum_bid, min(high, budget_left))


def jev_order(candidate):
    """Jev can promote a player it is confident about; it never demotes one.

    Same rule as the bids. When Jev is unsure, which it is when the state is thin, the
    list is exactly the projection list rather than noise sorted around 0.5.
    """
    confident = _jev_band(candidate) < 3
    return (not confident, -(candidate.get("jev") or 0) if confident else 0, candidate["math_rank"])


def drop_candidates(roster, target, slot_counts, horizon, this_week, replacement=None, count=3):
    """Who you'd cut to make room: bench only, cheapest first, never someone covering a hole.

    A drop is offered only if it opens no new hole in any week of the look-ahead, so it
    can't cost you your only kicker, or the receiver who covers a bye three weeks out.
    """
    replacement = replacement or {}
    spare = sorted((p for p in roster if target[p["id"]] == BENCH),
                   key=lambda p: (surplus(p, replacement), season_pace(p)))
    holes = [sum(shortfalls(projected_for(roster, w, this_week), slot_counts, g).values()) for w, g in horizon]
    droppable = []
    for person in spare:
        rest = [p for p in roster if p["id"] != person["id"]]
        after = [sum(shortfalls(projected_for(rest, w, this_week), slot_counts, g).values()) for w, g in horizon]
        if all(a <= b for a, b in zip(after, holes)):
            droppable.append(person)
        if len(droppable) == count:
            break
    return droppable


def drop_for(candidate, roster, slot_counts, horizon, this_week, replacement):
    """Who to cut for this particular add: least surplus first, and no hole left behind.

    The add is on the roster while this is worked out, so a swap is allowed: a kicker can pay
    for a kicker where dropping your only one would otherwise read as leaving a hole.

    Kickers and defenses fill one slot, take no flex, and are always on the wire, so a second
    one is a wasted roster spot whatever he projects. An add at those positions is paid for
    at the same position, never with a skill player, even when that opens a hole in a later
    week: the swap's value counts that week as lost (kicker_weeks), and the hole is filled that
    week. Letting the hole check fall through to the bench is how week 11 offered Treylon Burks
    for Jason Sanders (off in week 13) one row below Jason Myers for Cairo Santos.
    """
    if candidate["position"] in STREAM_ONLY:
        same = sorted((p for p in roster if p["position"] == candidate["position"]),
                      key=lambda p: (surplus(p, replacement), season_pace(p)))
        if same:
            return same[0]
    joined = roster + [candidate]
    placed, _ = plan(joined, slot_counts, horizon[0][1])
    holes = [sum(shortfalls(projected_for(joined, w, this_week), slot_counts, g).values()) for w, g in horizon]
    spare = sorted((p for p in joined if p["id"] != candidate["id"] and placed[p["id"]] == BENCH),
                   key=lambda p: (surplus(p, replacement), season_pace(p)))
    for person in spare:
        rest = [p for p in joined if p["id"] != person["id"]]
        after = [sum(shortfalls(projected_for(rest, w, this_week), slot_counts, g).values()) for w, g in horizon]
        if all(a <= b for a, b in zip(after, holes)):
            return person
    return None


CANT_PLAY_WORDS = {"OUT": "out", "INJURY_RESERVE": "on IR", "SUSPENSION": "suspended"}


def _why_out(player, games):
    if player["pro_team"] not in games:
        return "bye"
    return CANT_PLAY_WORDS.get(player["injury_status"], "projected 0")


def holes_now(roster, slot_counts, games, week):
    """Starting slots nobody on your roster can fill this week, each with who is out of it.

    bye_gaps covers the weeks after this one. This week's are the ones a fill-in is claimed for
    today, and the write-up has to be told: shown only the regular starter's season numbers, it
    compared Cairo Santos with Jason Myers, on bye in week 11, and called him no better.
    """
    lines = []
    for slot, missing in sorted(shortfalls(projected_for(roster, week, week), slot_counts, games).items(),
                                key=lambda item: SLOT_ORDER.index(item[0])):
        out = [p for p in roster if p["position"] == SLOT_NAMES[slot] and expected_points(p, games) == 0]
        line = f"week {week}: {SLOT_NAMES[slot]} short by {missing}"
        if out:
            line += " — " + ", ".join(f"{p['name']} ({PRO_TEAMS.get(p['pro_team'], '?')}, {_why_out(p, games)})"
                                      for p in out)
        lines.append(line)
    return lines


def bye_gaps(roster, slot_counts, horizon, this_week, pool=()):
    """Holes in your lineup over the coming weeks, each with who available could fill it."""
    gaps = []
    for week, games in horizon[1:]:
        short = shortfalls(projected_for(roster, week, this_week), slot_counts, games)
        for slot, missing in sorted(short.items(), key=lambda item: SLOT_ORDER.index(item[0])):
            cover = sorted((c for c in pool if slot in c["eligible_slots"] and c["pro_team"] in games
                            and c["injury_status"] not in CANT_PLAY),
                           key=lambda c: -season_pace(c))[:2]
            line = f"week {week}: {SLOT_NAMES[slot]} short by {missing}"
            if cover:
                line += " — available and playing that week: " + ", ".join(
                    f"{c['name']} ({PRO_TEAMS.get(c['pro_team'], '?')})" for c in cover)
            gaps.append(line)
    return gaps


RETRY_WAITS = (3, 20)   # seconds before the second and third tries at a call to Claude
NO_WRITE_UP = ("No write-up today: the call to Claude failed, and the reason is in the log. The table is "
               "complete, but nobody has checked the news behind it.")


def _post(body, headers):
    """Up to three tries at one Messages call.

    Through mid-November 30 of 53 reports went out with no write-up, and 24 of those were the Mac
    going back to sleep in the middle of this call. It then dies the moment the Mac wakes, as a
    timeout or a reset, often before Wi-Fi is back, which is why the last wait is the long one.
    """
    for wait in RETRY_WAITS + (None,):
        try:
            return requests.post("https://api.anthropic.com/v1/messages", json=body, headers=headers, timeout=120)
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout):
            if wait is None:
                raise
            time.sleep(wait)


def _complete(body, headers):
    """One Messages call, resumed while the API pauses a long web-search turn.

    Returns (http_status, content_blocks, stop_reason). Unhandled, a pause_turn arrives
    as a sentence that simply stops, which is what the early reports did.
    """
    blocks = []
    for _ in range(1 + MAX_CONTINUATIONS):
        messages = body["messages"] + ([{"role": "assistant", "content": blocks}] if blocks else [])
        resp = _post(dict(body, messages=messages), headers)
        if resp.status_code != 200:
            return resp.status_code, None, None
        data = resp.json()
        blocks = blocks + data.get("content", [])
        if data.get("stop_reason") != "pause_turn":
            return 200, blocks, data.get("stop_reason")
    return 200, blocks, "pause_turn"


def _render(blocks, stop_reason):
    """The write-up as Claude wrote it, plus the sources behind its claims.

    Cited passages arrive as separate text blocks mid-sentence, so they are joined with
    nothing between them. The sources are what let you check a claim before acting on it.
    """
    text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text").strip()
    if not text:
        return None
    if stop_reason == "max_tokens":
        text += f"\n\n[Cut off: the write-up hit the {MAX_TOKENS:,}-token output limit.]"
    elif stop_reason == "pause_turn":
        text += "\n\n[Cut off: the web search was still running after the last resume.]"
    sources = []
    for block in blocks:
        for citation in block.get("citations") or []:
            source = (citation.get("title") or citation.get("url"), citation.get("url"))
            if source[1] and source not in sources:
                sources.append(source)
    if sources:
        text += "\n\nSources:\n" + "\n".join(f"- {title}\n  {url}" for title, url in sources)
    return text


def ask_claude(brief):
    """Claude turns the numbers into recommendations. Returns None if it can't."""
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
    key = os.getenv("ANTHROPIC_API_KEY")
    if not key:
        return None
    prompt = (
        "You advise one manager in a 14-team, 0.5 PPR ESPN keeper league (one keeper carries to next "
        "season). Nearly every claim is decided by blind bid in the waiver run early on Wednesday. A player "
        "who clears waivers is a free agent anyone can add at once, for nothing, until his next game starts. "
        "The wire is thin with 14 teams.\n\n"
        f"Here is today's data:\n{json.dumps(brief, indent=1)}\n\n"
        "Bid on at most three players, and none is a good answer when nothing qualifies; adding a free agent "
        "costs nothing and doesn't count toward the three. Every claim "
        "needs a reason: a positive lineup gain in the data, news you found and cite, or a next_man_up note. "
        "For a next_man_up, search first for how long the lead back is expected to "
        "miss: if it is weeks, the claim is a priority even as a bench stash, and if he is due back at once, say so "
        "and bid low. A hole in a later week is not a claim today: name the week and leave the claim for "
        "that week, unless the player would also start for you before then. A hole this week is different: "
        "lineup_holes_this_week names each starting slot nobody on the roster can fill this week, and who is out "
        "of it. That slot scores zero unless it is filled, so a candidate who fills it gains what "
        "lineup_gain_this_week says even if he is worse than the regular starter the rest of the season: compare "
        "him with zero, not with that starter. A kicker or a defense replaces "
        "the one you have; never drop another position to carry two. Kickers are interchangeable in any week "
        "both play (their projections predicted nothing in 2025 or 2026), so dropping yours for one who covers "
        "his bye costs nothing after it. A candidate whose availability says free "
        "agent should be added today, before someone else does, with no bid. For each: the player, a bid (none "
        "for a free agent), and the drop. note_on_bids says what each end of a suggested range has won in this "
        "league. Take the bid from the range, toward the high end when he would start for you and faab_pace shows money to spare: "
        "FAAB left over when the season ends buys nothing. If your search shows he is this week's most wanted "
        "pickup (a new starter, a featured role) and he is not already priced as in demand, bid from the "
        "in-demand line and say why. Every candidate comes with the drop that costs least; use it unless "
        "you have a reason to prefer another, and give that reason. Compare players only with numbers that appear in the data, never an "
        "estimate of one that doesn't. Say plainly what to skip and why. "
        "Mention keeper value only when it genuinely changes the call. If you search, check the last few "
        "days for injury, role, or discipline news on these players (a snap share that jumped is often the "
        "first sign of a role change; null in the weekly list means he didn't play), and say when news takes effect: "
        "a pending suspension is not an absence this week. The report's table prints your call on every "
        "candidate beside his numbers, so the text must agree with those calls; spend it on what a row can't "
        "say: the news you found, and the reason for any call that leaves a suggested bid or drop. Under 250 "
        "words, no preamble, no headings. Then end with your call on every candidate in available, as a JSON "
        "block fenced with ```json and nothing after it: "
        '{"calls": [{"player": "<name exactly as in available>", "call": "add" (a free agent) or "bid" (on '
        'waivers) or "skip", "bid": <dollars, for a bid>, "drop": "<name, for an add or a bid>", '
        '"why": "<under 15 words>"}]}'
    )
    body = {"model": MODEL, "max_tokens": MAX_TOKENS, "messages": [{"role": "user", "content": prompt}],
            "tools": [{"type": "web_search_20250305", "name": "web_search"}]}
    headers = {"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"}
    started = time.time()
    for attempt in (body, {k: v for k, v in body.items() if k != "tools"}):
        try:
            status, blocks, stop_reason = _complete(attempt, headers)
        except requests.exceptions.RequestException as problem:
            print(f"(Claude call failed after {time.time() - started:.0f}s: "
                  f"{type(problem).__name__}: {str(problem)[:150]})")
            return None
        if status == 200:
            text = _render(blocks, stop_reason)
            if text:
                return text
        elif status < 500:
            continue  # most likely the web search tool isn't available; try again without it
    print("(Claude call failed; sending the numbers without a write-up)")
    return None


HELD_NOTE = "A kicker, defense or one-week fill-in is listed in the week you need him, not before."
NOBODY = "Nobody on the wire is worth a bid today."
FREE = "free"   # the bid column for a player who has cleared waivers: the call column says whether to add him
FREE_NOTE = ("\"free\": he has cleared waivers, so he costs nothing and goes to whoever adds him first. "
             "Everyone else is decided by bid at the next run.")


def _bid(candidate, dash="-"):
    """The bid as the reports show it. A free agent who has cleared waivers needs none."""
    if candidate.get("free_now"):
        return FREE
    low, high = candidate["bid"]
    return f"${low}" if low == high else f"${low}{dash}{high}"


THIS_WEEK_ONLY = "This week only: fills a hole or streams one game, adds nothing after"
CALLS = ("add", "bid", "skip")
CALL_ORDER = {"add": 0, "bid": 0, None: 1, "skip": 2}
CALLS_BLOCK = re.compile(r"```json\s*(\{.*?\})\s*```", re.S)


def this_week_only(candidate):
    """A fill-in for this week: he helps your lineup now, in no later week, and not over the season.

    One week of a kicker covering a bye is worth ten points, so a $2 rental used to head the table
    and read as the pick of the day. He is listed under the players with more to offer.
    """
    return (candidate["gain"] > 0 and candidate.get("weeks_helped", 1) <= 1 and candidate["season_gain"] <= 0
            and not candidate.get("role"))


def table_sections(candidates):
    """[(heading, rows)]: players with value past this week (no heading), then this week's fill-ins.

    Within each, the write-up's adds and bids come first and its skips last; otherwise the ranked order.
    """
    ordered = sorted(candidates, key=lambda c: CALL_ORDER[(c.get("call") or {}).get("call")])
    sections = [(None, [c for c in ordered if not this_week_only(c)]),
                (THIS_WEEK_ONLY, [c for c in ordered if this_week_only(c)])]
    return [(heading, rows) for heading, rows in sections if rows]


def split_calls(text):
    """(the write-up without its calls block, {player name: his call as the write-up gave it}).

    No block, or one that doesn't parse, gives no calls, and the table shows the numbers alone, as
    it did before the write-up had a say in it.
    """
    found = list(CALLS_BLOCK.finditer(text or ""))
    if not found:
        return text, {}
    block = found[-1]
    prose = "\n\n".join(part for part in (text[:block.start()].strip(), text[block.end():].strip()) if part)
    try:
        return prose, {row["player"]: row for row in json.loads(block.group(1))["calls"] if row.get("call") in CALLS}
    except (ValueError, KeyError, TypeError, AttributeError):
        print("(the write-up's calls didn't parse; the table shows the numbers alone)")
        return prose, {}


def _dollars(value, fallback):
    try:
        return max(0, int(float(str(value).strip().lstrip("$"))))
    except (TypeError, ValueError):
        return fallback


def attach_calls(candidates, calls):
    """Put the write-up's call on each row it names, held to what the table knows about him.

    A free agent is added, never bid on: on Nov 18 the write-up said to bid $5 on Cade Otton, who
    had cleared waivers and cost nothing. A bid with no amount takes the low end of the range.
    """
    folded = {name.casefold(): call for name, call in calls.items() if isinstance(name, str)}
    for candidate in candidates:
        said = calls.get(candidate["name"]) or folded.get(candidate["name"].casefold())
        if not said:
            continue
        call = "skip" if said["call"] == "skip" else "add" if candidate.get("free_now") else "bid"
        drop = said.get("drop")
        candidate["call"] = {"call": call,
                             "bid": _dollars(said.get("bid"), candidate["bid"][0]) if call == "bid" else None,
                             "drop": drop if isinstance(drop, str) and drop.strip() and call != "skip" else None,
                             "why": str(said.get("why") or "").strip()}


def _call(candidate):
    """The write-up's call as the table shows it; empty where it made none."""
    said = candidate.get("call")
    if not said:
        return ""
    return f"bid ${said['bid']}" if said["call"] == "bid" else said["call"]


def _drop(candidate):
    """Who goes for him: the write-up's choice when it named one, else the cheapest drop.

    Nobody, for a player the write-up skips: on Nov 19 Baker Mayfield's skip row still offered
    Emari Demercado, the same drop as Justin Herbert's add just above it.
    """
    said = candidate.get("call") or {}
    if said.get("call") == "skip":
        return None
    if said.get("drop"):
        return said["drop"]
    return candidate["drop"]["name"] if candidate.get("drop") else None


def headline(candidates):
    """The subject line: the write-up's adds and bids when it made calls, else the table's count."""
    said = [c["call"]["call"] for c in candidates if c.get("call")]
    if said:
        parts = [f"{said.count(call)} to {words}" for call, words in (("add", "add"), ("bid", "bid on"))
                 if said.count(call)]
        return ", ".join(parts) or "nothing to add or bid on"
    worth_it = [c for c in candidates if c["gain"] > 0 or c.get("role")]
    return f"{len(worth_it)} worth a claim" if worth_it else "nothing worth a claim"


def faab_pace(context):
    """One line on how fast the budget is going, or None when ESPN doesn't say when the season ends.

    A bid isn't scaled by this: what a claim costs is set by the other thirteen teams. It is here
    because money left after the last week buys nothing.
    """
    weeks_left, played = context.get("weeks_left"), context["week"] - 1
    if not weeks_left:
        return None
    line = (f"${context['budget_left'] / weeks_left:.0f} a week for the {weeks_left} "
            f"week{'s' if weeks_left != 1 else ''} of waivers left, this one included")
    if played:
        line += f"; ${(context['budget'] - context['budget_left']) / played:.0f} a week spent so far"
    return line


def bid_notes(context):
    """What the bid ranges are priced off, one note to a line. Both reports print them."""
    past = context.get("past_claims") or {}
    lines = ["What it has taken to win a claim in this league (bids.py):"]
    for kind, label in bids.KINDS.items():
        prices = past.get(kind, ())
        if len(prices) >= MIN_CLAIMS:
            steps = ", ".join(f"${winning_bid(prices, share)} won {said}" for share, said in ODDS.items())
            lines.append(f"{label} ({len(prices)} claims): {steps}")
        else:
            lines.append(f"{label}: {len(prices)} claims so far, so a share of your budget until there are {MIN_CLAIMS}")
    if any(len(prices) >= MIN_CLAIMS for prices in past.values()):
        lines.append(f"For a player who would start for you, the range runs from the bid that won {ODDS[STARTER[0]]} to "
                     f"the one that won {ODDS[STARTER[1]]}; for a stash, from {ODDS[STASH[0]]} to {ODDS[STASH[1]]}.")
    in_demand = [bids.describe(c) for c in context["candidates"]
                 if bids.kind_of(c) == bids.IN_DEMAND and not c.get("free_now")]
    if in_demand:
        lines.append("Priced as in demand today: " + "; ".join(in_demand))
    return lines


def _level(player):
    """His level as the reports show it; a dash where values.py keeps ESPN's number (tight ends, kickers,
    defenses) and for anyone ESPN sent no games for."""
    return "-" if player.get("level") is None else f"{player['level']:.1f}"


def build_report(context):
    lines = [f"Waiver report — {datetime.now():%a %b %d}, week {context['week']}", ""]
    lines.append(f"Your FAAB: ${context['budget_left']} of ${context['budget']}"
                 f"   (minimum bid ${context['minimum_bid']})")
    if faab_pace(context):
        lines.append(f"  {faab_pace(context)}")
    if context["rivals"]:
        rich = sorted(context["rivals"], reverse=True)[:3]
        lines.append("Rivals with the most FAAB: " + ", ".join(f"${r}" for r in rich))
    if context["last_runs"]:
        stamps = [run.strftime("%a %I:%M %p").replace(" 0", " ") for run in context["last_runs"][-3:]]
        lines.append("Claims processed: " + ", ".join(stamps))
    lines.append("")

    if context.get("holes_now"):
        lines += ["Lineup holes this week:"] + [f"  {hole}" for hole in context["holes_now"]] + [""]
    if context["gaps"]:
        lines += [f"Lineup holes in the next {LOOKAHEAD} weeks:"] + [f"  {gap}" for gap in context["gaps"]]
        lines += [f"  {HELD_NOTE}", ""]

    stepping_in = [c for c in context["candidates"] if c.get("role")]
    if stepping_in:
        lines += ["Next man up:"] + [f"  {roles.describe(c)}" for c in stepping_in]
        lines += [f"  Backs flagged this way averaged {roles.FILL_IN} a game over their next {roles.FILL_IN_WEEKS} weeks "
                  f"in 2025-26. Just under", "  half reached 10 a game and a quarter stayed under 5. "
                  "They are listed even with no lineup gain.", ""]

    if context.get("ranked_by"):
        lines += [context["ranked_by"], ""]
    jev_column = any("jev" in c for c in context["candidates"])
    header = (f"{'PLAYER':<20} {'POS':<5} {'TEAM':<5} {'OWN%':>6} {'+/-':>6} {'SNAP%':>7} {'WK':>5} {'LEVEL':>5} "
              f"{'NOW':>5} {str(LOOKAHEAD + 1) + 'WK':>5} {'SEASON':>7}")
    call_column = any(c.get("call") for c in context["candidates"])
    lines.append(header + ("  JEV  CONF  PROJ#" if jev_column else "") + f"  {'BID':<7} "
                 + (f"{'CALL':<9} " if call_column else "") + "DROP FOR HIM")
    for heading, rows in table_sections(context["candidates"]):
        if heading:
            lines += ["", f"{heading}:"]
        for candidate in rows:
            bid = _bid(candidate)
            row = (f"{candidate['name'][:20]:<20} {candidate['position']:<5} "
                   f"{PRO_TEAMS.get(candidate['pro_team'], '?'):<5} "
                   f"{candidate['percent_owned']:>5.0f}% {candidate['percent_change']:>+6.1f} "
                   f"{snaps.label(candidate.get('snaps')):>7} "
                   f"{candidate['projection'] or 0:>5.1f} {_level(candidate):>5} "
                   f"{candidate['gain']:>+5.1f} {candidate.get('ahead_gain', candidate['gain']):>+5.1f} "
                   f"{candidate['season_gain']:>+7.0f}")
            if jev_column:
                judged = "jev" in candidate  # kickers and defenses are never asked
                row += (f" {candidate['jev']:>5.2f} {candidate['jev_confidence']:>5.2f}" if judged else f" {'-':>5} {'-':>5}")
                row += f" {candidate.get('math_rank', 0):>6}"
            call = f"{_call(candidate):<9} " if call_column else ""
            lines.append(f"{row}  {bid:<7} {call}{(_drop(candidate) or '-')[:18]}")
            if (candidate.get("call") or {}).get("why"):
                lines.append(f"    {candidate['call']['why']}")
    if not context["candidates"]:
        lines.append(NOBODY)
    title, *notes = bid_notes(context)
    lines += ["", title] + [wrapped for note in notes
                            for wrapped in textwrap.wrap(note, 100, initial_indent="  ", subsequent_indent="    ")]

    if context["advice"]:
        lines += ["", "-" * 60, "", context["advice"]]
    elif context.get("advice_failed"):
        lines += ["", "-" * 60, "", NO_WRITE_UP]
    if any(c.get("free_now") for c in context["candidates"]):
        lines += ["", FREE_NOTE]
    lines += ["", "WK is ESPN's projection for this week. LEVEL is what he scores a game on this season's",
              "evidence (values.py), and it is what the weeks after this one are valued at. A dash means",
              "ESPN's season projection is used as it is: tight ends, kickers and defenses.",
              "", "Bids are suggestions only. Place claims yourself in ESPN."]
    return "\n".join(lines)


def jev_pass(espn, league, roster, target, scored, faab_left):
    """Jev scores for the top skill-position candidates, added in place. Returns (judged, note, with_usage)."""
    settings = league["settings"]
    judged = [c for c in scored if c["position"] in jev.SCORED_POSITIONS][:jev.DEPTH]
    try:
        usage = espn.usage([c["id"] for c in judged], league.get("status", {}).get("finalScoringPeriod", 18))
    except (ESPNError, requests.exceptions.RequestException) as problem:
        print(f"(usage lookup failed: {problem}); Jev will judge without it")
        usage = {}
    for candidate in judged:
        candidate["usage"] = jev.usage_summary(usage.get(candidate["id"]), candidate["position"])
    note = jev.score(
        judged,
        jev.roster_summary(roster, target, BENCH, IR),
        {"teams": settings["size"], "points_per_reception": 0.5,
         "keepers_next_season": settings.get("draftSettings", {}).get("keeperCount"),
         "my_faab_left": faab_left},
        depth=len(judged),
    )
    return judged, note, sum(1 for c in judged if c.get("usage"))


def log_jev(week, judged, path=None):
    """Append today's judgments, so in a few weeks they can be checked against what happened."""
    path = path or JEV_LOG
    rows = [c for c in judged if "jev" in c]
    if not rows:
        return 0
    path.parent.mkdir(exist_ok=True)
    new_file = not path.exists()
    stamp = datetime.now().isoformat(timespec="minutes")
    with open(path, "a", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=JEV_LOG_FIELDS)
        if new_file:
            writer.writeheader()
        for c in rows:
            usage = c.get("usage") or {}
            writer.writerow({"logged_at": stamp, "week": week, "player_id": c["id"], "name": c["name"],
                             "position": c["position"], "jev": c["jev"], "jev_confidence": c["jev_confidence"],
                             "math_rank": c["math_rank"], "projection": c["projection"],
                             "season_projection": c["season_projection"], "percent_owned": c["percent_owned"],
                             "usage_per_game": usage.get("per_game"), "usage_trend": usage.get("trend")})
    return len(rows)


def log_pool(week, pool, scored, path=None):
    """Append what this run knew about every free agent it fetched, scored or not.

    auctions.py matches each night's waiver auctions to these rows, which is how we'll learn
    whether anything visible at 6am (the add rate, mostly) predicts a bidding war.
    """
    path = path or POOL_LOG
    if not pool:
        return 0
    judged = {c["id"]: c for c in scored}
    path.parent.mkdir(exist_ok=True)
    new_file = not path.exists()
    stamp = datetime.now().isoformat(timespec="minutes")
    with open(path, "a", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=POOL_LOG_FIELDS)
        if new_file:
            writer.writeheader()
        for p in pool:
            c = judged.get(p["id"])        # injured free agents aren't scored, so they get no band
            bid = c.get("bid") if c else None
            writer.writerow({"logged_at": stamp, "week": week, "player_id": p["id"], "name": p["name"],
                             "position": p["position"], "percent_owned": p["percent_owned"],
                             "percent_change": p["percent_change"], "projection": p["projection"],
                             "band": bid_band(c) if c else "", "bid": f"{bid[0]}-{bid[1]}" if bid else ""})
    return len(pool)


def ranker_from(argv):
    if "--ranker" in argv and "jev" in argv:
        return "jev"
    if "--jev-shadow" in argv:
        return "shadow"
    return "math"


CELL = "padding:8px 10px;border-bottom:1px solid #f1f1f3;font-size:14px"
NUMBER = "font-variant-numeric:tabular-nums;text-align:right"
LINK = re.compile(r"(https?://[^\s<]+)")


def _cell(value, extra=""):
    return f'<td style="{CELL};{extra}">{value}</td>'


def _number(value, decimals=1):
    """Gains read better when zero is quiet and a real gain is not."""
    color = "#15803d" if value > 0 else "#9ca3af"
    return _cell(f"{value:+.{decimals}f}", f"{NUMBER};color:{color}")


def _column(label, right=False):
    return (f'<th style="padding:8px 10px;border-bottom:2px solid #e5e7eb;font-size:11px;letter-spacing:.06em;'
            f'text-transform:uppercase;color:#6b7280;text-align:{"right" if right else "left"}">{label}</th>')


def _prose(text):
    """The write-up as paragraphs, with its sources clickable."""
    parts = []
    for block in html_escape.escape(text).split("\n\n"):
        block = LINK.sub(r'<a href="\1" style="color:#2563eb">\1</a>', block).replace("\n", "<br>")
        parts.append(f'<p style="margin:0 0 12px;font-size:14px;line-height:1.5">{block}</p>')
    return "".join(parts)


def build_html(context):
    """The report as HTML. The plain text version still goes out as the fallback."""
    jev_column = any("jev" in c for c in context["candidates"])
    call_column = any(c.get("call") for c in context["candidates"])
    headers = [_column("Player")] + ([_column("Call")] if call_column else []) + [_column("Rostered", True), _column("+/-", True), _column("Snaps", True),
               _column("Level", True), _column("This wk", True),
               _column(f"Next {LOOKAHEAD + 1}", True), _column("Season", True)]
    if jev_column:
        headers += [_column("Jev", True), _column("Conf", True)]
    headers += [_column("Bid"), _column("Drop for him")]

    rows, index = [], 0
    for heading, section in table_sections(context["candidates"]):
        if heading:
            rows.append(f'<tr><td colspan="{len(headers)}" style="padding:16px 10px 6px;font-size:12px;font-weight:600;'
                        f'color:#6b7280;border-bottom:2px solid #e5e7eb">{html_escape.escape(heading)}</td></tr>')
        for candidate in section:
            said = candidate.get("call") or {}
            cells = [
                _cell(f'<strong>{html_escape.escape(candidate["name"])}</strong>'
                      f'<div style="color:#6b7280;font-size:12px">{candidate["position"]} &middot; '
                      f'{PRO_TEAMS.get(candidate["pro_team"], "?")}</div>'),
            ]
            if call_column:
                color = "#9ca3af" if said.get("call") == "skip" else "#15803d"
                cells.append(_cell(f'<strong style="color:{color}">{html_escape.escape(_call(candidate))}</strong>'
                                   if said else "&mdash;", "white-space:nowrap"))
            cells += [
                _cell(f'{candidate["percent_owned"]:.0f}%', NUMBER),
                _cell(f'{candidate["percent_change"]:+.1f}', NUMBER),
                _cell(html_escape.escape(snaps.label(candidate.get("snaps"))), NUMBER + ";white-space:nowrap"),
                _cell(_level(candidate).replace("-", "&mdash;"), NUMBER),
                _number(candidate["gain"]),
                _number(candidate.get("ahead_gain", candidate["gain"])),
                _number(candidate["season_gain"], 0),
            ]
            if jev_column:
                cells += ([_cell(f'{candidate["jev"]:.2f}', NUMBER), _cell(f'{candidate["jev_confidence"]:.2f}', NUMBER)]
                          if "jev" in candidate else [_cell("&mdash;", NUMBER), _cell("&mdash;", NUMBER)])
            cells.append(_cell(_bid(candidate, "&ndash;"), "white-space:nowrap"))
            drop = _drop(candidate)
            cells.append(_cell(html_escape.escape(drop) if drop else "&mdash;", "color:#374151"))
            stripe = "background:#fafafa" if index % 2 else ""
            row = "".join(cells)
            if said.get("why"):   # the reason sits under its row, so the row loses its rule
                row = row.replace("border-bottom:1px solid #f1f1f3", "border-bottom:none")
            rows.append(f'<tr style="{stripe}">{row}</tr>')
            if said.get("why"):
                rows.append(f'<tr style="{stripe}"><td colspan="{len(headers)}" style="padding:0 10px 8px;font-size:12px;'
                            f'color:#6b7280;border-bottom:1px solid #f1f1f3">{html_escape.escape(said["why"])}</td></tr>')
            index += 1

    holes = ""
    if context.get("holes_now"):
        items = "".join(f'<li style="margin:2px 0">{html_escape.escape(hole)}</li>' for hole in context["holes_now"])
        holes += (f'<div style="background:#fef2f2;border-left:3px solid #ef4444;padding:10px 14px;margin:18px 0">'
                  f'<div style="font-weight:600;font-size:14px">Lineup holes this week</div>'
                  f'<ul style="margin:6px 0 0;padding-left:18px;color:#374151;font-size:14px">{items}</ul></div>')
    if context["gaps"]:
        items = "".join(f'<li style="margin:2px 0">{html_escape.escape(gap)}</li>' for gap in context["gaps"])
        holes += (f'<div style="background:#fff7ed;border-left:3px solid #fb923c;padding:10px 14px;margin:18px 0">'
                  f'<div style="font-weight:600;font-size:14px">Lineup holes in the next {LOOKAHEAD} weeks</div>'
                  f'<ul style="margin:6px 0 0;padding-left:18px;color:#374151;font-size:14px">{items}</ul>'
                  f'<div style="color:#6b7280;font-size:12px;margin-top:6px">{HELD_NOTE}</div></div>')

    stepping_in = [c for c in context["candidates"] if c.get("role")]
    if stepping_in:
        items = "".join(f'<li style="margin:2px 0">{html_escape.escape(roles.describe(c))}</li>' for c in stepping_in)
        holes += (f'<div style="background:#eff6ff;border-left:3px solid #3b82f6;padding:10px 14px;margin:18px 0">'
                  f'<div style="font-weight:600;font-size:14px">Next man up</div>'
                  f'<ul style="margin:6px 0 0;padding-left:18px;color:#374151;font-size:14px">{items}</ul>'
                  f'<div style="color:#6b7280;font-size:12px;margin-top:6px">Backs flagged this way averaged '
                  f'{roles.FILL_IN} a game over their next {roles.FILL_IN_WEEKS} weeks in 2025-26. Just under half '
                  f'reached 10 a game and a quarter stayed under 5. Listed even with no lineup gain.'
                  f'</div></div>')

    summary = (f'FAAB <strong>${context["budget_left"]}</strong> of ${context["budget"]} '
               f'&middot; minimum bid ${context["minimum_bid"]}')
    if context["rivals"]:
        summary += " &middot; rivals&rsquo; most: " + ", ".join(
            f"${amount}" for amount in sorted(context["rivals"], reverse=True)[:3])
    if faab_pace(context):
        summary += f"<br>{faab_pace(context)}"
    if context["last_runs"]:
        stamps = [run.strftime("%a %I:%M %p").replace(" 0", " ") for run in context["last_runs"][-3:]]
        summary += "<br>Claims processed: " + ", ".join(stamps)

    ranked = (f'<p style="color:#6b7280;font-size:13px;margin:0 0 10px">'
              f'{html_escape.escape(context["ranked_by"]).replace(chr(10), "<br>")}</p>'
              if context.get("ranked_by") else "")
    free_note = f"{FREE_NOTE} " if any(c.get("free_now") for c in context["candidates"]) else ""
    table = (f'<table style="border-collapse:collapse;width:100%;margin-bottom:18px">'
             f'<thead><tr>{"".join(headers)}</tr></thead><tbody>{"".join(rows)}</tbody></table>'
             if rows else f'<p style="font-size:14px;margin:18px 0">{NOBODY}</p>')
    notes = bid_notes(context)
    prices = (f'<div style="background:#f9fafb;border-left:3px solid #d1d5db;padding:10px 14px;margin:0 0 18px;'
              f'color:#374151;font-size:13px;line-height:1.5"><div style="font-weight:600">'
              f'{html_escape.escape(notes[0])}</div>'
              + "".join(f"<div>{html_escape.escape(note)}</div>" for note in notes[1:]) + "</div>")
    write_up = context.get("advice") or (NO_WRITE_UP if context.get("advice_failed") else "")
    advice = _prose(write_up) if write_up else ""

    return (f'<div style="font-family:-apple-system,BlinkMacSystemFont,\'Segoe UI\',Roboto,Helvetica,Arial,'
            f'sans-serif;color:#111827;max-width:720px;margin:0 auto;padding:4px 8px">'
            f'<h2 style="margin:0 0 4px;font-size:19px">Waiver report &middot; week {context["week"]}</h2>'
            f'<p style="color:#4b5563;font-size:14px;margin:0 0 4px">{summary}</p>'
            f'{holes}{ranked}{table}{prices}'
            f'{advice}'
            f'<p style="color:#6b7280;font-size:12px;border-top:1px solid #e5e7eb;padding-top:10px">'
            f'{free_note}Bids are suggestions only. Place claims yourself in ESPN.</p></div>')


CARDS = 10   # player cards in the channel; the table in the thread has every player
COLUMN_NOTE = ("WK is ESPN's projection for this week. LEVEL is what he scores a game on this season's evidence "
               "(values.py), and it is what the weeks after this one are valued at. A dash means ESPN's season "
               "projection is used as it is: tight ends, kickers and defenses.")


def slack_layout(context, subject, report):
    """The report for Slack: (blocks for the channel, layouts for the reply under it).

    The channel gets the day in a few lines, a card a player with the write-up's call on it, and the
    write-up. The reply under it has the full table and what claims have cost, as a Slack table, or
    as the text report in monospace if Slack won't take the table.
    """
    esc = slack.escape
    buttons = os.getenv("SLACK_BUTTONS") == "true"   # on once the Worker behind them is deployed
    money = [f"FAAB ${context['budget_left']} of ${context['budget']}", f"minimum bid ${context['minimum_bid']}"]
    if context["rivals"]:
        money.append("rivals with the most: " + ", ".join(f"${r}" for r in sorted(context["rivals"], reverse=True)[:3]))
    pace = faab_pace(context)
    blocks = [slack.header(subject), slack.context(" · ".join(money) + (f"\n{pace}" if pace else ""))]

    if context.get("holes_now"):
        blocks.append(slack.section("*Holes this week*\n" + "\n".join(f"• {esc(h)}" for h in context["holes_now"])))
    if context["gaps"]:
        blocks.append(slack.section(f"*Holes in the next {LOOKAHEAD} weeks*\n"
                                    + "\n".join(f"• {esc(gap)}" for gap in context["gaps"])))
        blocks.append(slack.context(esc(HELD_NOTE)))
    stepping_in = [c for c in context["candidates"] if c.get("role")]
    if stepping_in:
        blocks.append(slack.section("*Next man up*\n" + "\n".join(f"• {esc(roles.describe(c))}" for c in stepping_in)))
        blocks.append(slack.context(f"Backs flagged this way averaged {roles.FILL_IN} a game over their next "
                                    f"{roles.FILL_IN_WEEKS} weeks in 2025-26; just under half reached 10 a game "
                                    "and a quarter stayed under 5."))

    blocks.append(slack.divider())
    shown = 0
    for heading, rows in _slack_order(context["candidates"]):
        for candidate in rows[:CARDS - shown]:
            if heading and candidate is rows[0]:
                blocks.append(slack.context(f"*{esc(heading)}*"))
            card = slack.section(_card(candidate))
            value = bid_button(candidate, context) if buttons else None
            if value:
                card["accessory"] = slack.button("Bid", "bid", value)
            blocks += [card, slack.divider()]
            shown += 1
    if len(context["candidates"]) > shown:
        blocks.append(slack.context(f"{len(context['candidates']) - shown} more in the table in the thread."))
    if not context["candidates"]:
        blocks += [slack.section(esc(NOBODY)), slack.divider()]

    if context["advice"]:
        blocks += slack.write_up(context["advice"])
    elif context.get("advice_failed"):
        blocks.append(slack.section(f":warning: {esc(NO_WRITE_UP)}"))
    notes = ([FREE_NOTE] if any(c.get("free_now") for c in context["candidates"]) else [])
    notes += ["Bid places a claim in ESPN for the amount you type; change or cancel it in ESPN until waivers run."
              if buttons else "Bids are suggestions only. Place claims yourself in ESPN.",
              "The full table and what claims have cost are in the thread."]
    blocks.append(slack.context(esc(" ".join(notes))))

    details = [slack.section("*The numbers*")]
    when = []
    if context["last_runs"]:
        stamps = [run.strftime("%a %I:%M %p").replace(" 0", " ") for run in context["last_runs"][-3:]]
        when.append("Claims processed: " + ", ".join(stamps))
    if context.get("ranked_by"):
        when.append(context["ranked_by"])
    if when:
        details.append(slack.context(esc("\n".join(when))))
    title, *notes = bid_notes(context)
    details.append(slack.section(f"*{esc(title)}*\n" + "\n".join(f"• {esc(note)}" for note in notes)))
    details.append(slack.context(esc(COLUMN_NOTE)))
    if context["candidates"]:
        details.append(slack.table(*_slack_table(context)))
    return blocks, [details, slack.monospace(report)]


BID_VALUE = 2000   # characters a Slack button can carry


def bid_button(candidate, context):
    """What the Bid button carries to the Worker, which opens the bid form from it and keeps nothing
    itself: the player, his range, the write-up's call and why, the drop to suggest and the roster to
    choose from (anyone whose game hasn't started, bench and IR first), FAAB left and the week. None
    for a player who has cleared waivers: adding him happens at once, and that button comes later."""
    if candidate.get("free_now"):
        return None
    said = candidate.get("call") or {}
    drop_name = _drop(candidate)
    roster = sorted((p for p in context["roster"] if not p.get("locked")),
                    key=lambda p: (p["slot"] not in (BENCH, IR), p["name"]))
    value = {"p": candidate["id"], "n": candidate["name"],
             "t": f"{candidate['position']} · {PRO_TEAMS.get(candidate['pro_team'], '?')}",
             "r": list(candidate["bid"]), "k": said.get("call"), "c": said.get("bid"), "y": said.get("why") or "",
             "d": next((p["id"] for p in roster if drop_name and p["name"].casefold() == drop_name.casefold()), None),
             "o": [[p["id"], p["name"]] for p in roster], "f": context["budget_left"], "w": context["week"]}
    text = json.dumps(value, separators=(",", ":"))
    while len(text) > BID_VALUE and (len(value["y"]) > 80 or len(value["o"]) > 1):   # a long reason, then starters
        if len(value["y"]) > 80:
            value["y"] = value["y"][:77] + "..."
        else:
            value["o"] = [o for o in value["o"] if o[0] == value["d"]] + [o for o in value["o"] if o[0] != value["d"]][:-1]
        text = json.dumps(value, separators=(",", ":"))
    return text


def _slack_order(candidates):
    """[(heading, rows)] for Slack: every add and bid first, this week's fill-ins among them, then the
    rest as the text table has them. In the text table a one-week fill-in Claude bids on sits under
    players with value past this week, even ones it skips (Tuesday Nov 24: Watson, the only bid,
    below two skipped defenses); in Slack what to act on comes first."""
    acts = lambda c: (c.get("call") or {}).get("call") in ("add", "bid")
    sections = table_sections(candidates)
    first = [c for _, rows in sections for c in rows if acts(c)]
    rest = [(heading, [c for c in rows if not acts(c)]) for heading, rows in sections]
    return [(None, first)] * bool(first) + [(heading, rows) for heading, rows in rest if rows]


def _card(candidate):
    """One player: who, the write-up's call, what he adds, what he costs, who goes for him, and why."""
    esc = slack.escape
    call = _call(candidate)
    top = f"*{esc(candidate['name'])}*   {candidate['position']} · {PRO_TEAMS.get(candidate['pro_team'], '?')}"
    if call:
        top += f"   ·   {call if call == 'skip' else f'*{call}*'}"
    gain, ahead = candidate["gain"], candidate.get("ahead_gain", candidate["gain"])
    if this_week_only(candidate):
        worth = [f"{gain:+.1f} pts this week only"]
    else:
        worth = [f"{gain:+.1f} pts this week", f"{ahead:+.1f} over {LOOKAHEAD + 1} weeks",
                 f"{candidate['season_gain']:+.0f} season"]
    worth.append("free, cleared waivers" if candidate.get("free_now") else f"range {_bid(candidate)}")
    if _drop(candidate):
        worth.append(f"drop {esc(_drop(candidate))}")
    lines = [top, " · ".join(worth)]
    if candidate.get("role"):
        lines.append("Next man up")
    why = (candidate.get("call") or {}).get("why")
    if why:
        lines.append(f"_{esc(why)}_")
    return "\n".join(lines)


def _slack_table(context):
    """Every player's numbers, as the text report's table has them: (rows, the columns holding numbers)."""
    jev_column = any("jev" in c for c in context["candidates"])
    call_column = any(c.get("call") for c in context["candidates"])
    head = (["Player", "Pos", "Team", "Own%", "+/-", "Snap%", "Wk", "Level", "Now", f"{LOOKAHEAD + 1}wk", "Season"]
            + (["Jev", "Conf", "Proj#"] if jev_column else []) + ["Bid"] + (["Call"] if call_column else []) + ["Drop"])
    rows = [head]
    for _, group in _slack_order(context["candidates"]):
        for c in group:
            row = [c["name"], c["position"], PRO_TEAMS.get(c["pro_team"], "?"), f"{c['percent_owned']:.0f}%",
                   f"{c['percent_change']:+.1f}", snaps.label(c.get("snaps")), f"{c['projection'] or 0:.1f}",
                   _level(c), f"{c['gain']:+.1f}", f"{c.get('ahead_gain', c['gain']):+.1f}",
                   f"{c['season_gain']:+.0f}"]
            if jev_column:
                row += [f"{c['jev']:.2f}", f"{c['jev_confidence']:.2f}"] if "jev" in c else ["-", "-"]
                row.append(str(c.get("math_rank", 0)))
            rows.append(row + [_bid(c)] + ([_call(c) or "-"] if call_column else []) + [_drop(c) or "-"])
    text_columns = {"Player", "Pos", "Team", "Bid", "Call", "Drop"}
    return rows, {number for number, name in enumerate(head) if name not in text_columns}


def faab_spent(team, spent_by_team):
    """ESPN's spend counter reads zero in some leagues; the claims themselves are the honest record."""
    counter = team.get("transactionCounter", {}).get("acquisitionBudgetSpent") or 0
    return max(counter, spent_by_team.get(team["id"], 0))


def gather(espn, ranker="math", rate_shadow=False):
    league = espn.league("mSettings", "mTeam")
    settings = league["settings"]
    week = league["scoringPeriodId"]
    team = espn.my_team(league["teams"])
    slot_counts = {int(k): v for k, v in settings["rosterSettings"]["lineupSlotCounts"].items()}
    roster = [parse_roster_entry(e, week, espn.season) for e in espn.roster_entries(team["id"], week)]
    games = espn.pro_games(week)
    byes = espn.bye_weeks()
    horizon = schedule_ahead(games, byes, week)
    target, _ = plan(roster, slot_counts, games)

    acquisition = settings["acquisitionSettings"]
    budget = acquisition.get("acquisitionBudget", 0)
    every_bid = bids.all_bids(espn, week)
    claims = [bid for bid in every_bid if bid["status"] == "EXECUTED"]
    spent_by_team = Counter()
    for claim in claims:
        spent_by_team[claim["team_id"]] += claim["bid"]
    spent = faab_spent(team, spent_by_team)
    rivals = [budget - faab_spent(t, spent_by_team) for t in league["teams"] if t["id"] != team["id"]]
    budget_left = budget - spent

    pool = espn.free_agents(week, POOL_SIZE)
    lock_started(pool, games, datetime.now())
    rival_backs, cards = [], {}
    try:
        # A lead back is nearly always on someone's roster, so the role rule needs every team's backs.
        rival_backs = [p for p in (parse_roster_entry(entry, week, espn.season)
                                   for rival in espn.league("mRoster", week=week)["teams"] if rival["id"] != team["id"]
                                   for entry in rival["roster"]["entries"]) if p["position"] == "RB"]
        cards = values.attach(espn, roster + pool + rival_backs, week)
    except (ESPNError, requests.exceptions.RequestException) as problem:
        print(f"(player levels unavailable: {type(problem).__name__}); later weeks use ESPN's season projection")
    team_weeks = roles.finished_weeks(week, byes, games, datetime.now())
    try:
        roles.mark(pool, roster + rival_backs, cards, espn.season, team_weeks)
    except Exception as problem:  # broad on purpose: a missing extra must not stop the report
        print(f"(next-man-up check failed: {type(problem).__name__}: {problem}); report continues without it")
    past = {}
    try:
        bids.mark(pool, cards, espn.season, week, team_weeks)
        past = bids.history(espn, every_bid, team["id"], cards)
    except Exception as problem:  # broad on purpose: a missing extra must not stop the report
        print(f"(claim prices unavailable: {type(problem).__name__}: {problem}); bids fall back to budget shares")
    scored = score_pool(roster, pool, slot_counts, games, byes, week)
    try:
        shares, snap_week = snaps.load(espn.season)
    except Exception as problem:  # broad on purpose: a missing extra must not stop the report
        print(f"(snap counts unavailable: {type(problem).__name__}); report continues without them")
        shares, snap_week = {}, 0
    for candidate in scored:
        candidate["snaps"] = snaps.summary(shares.get(candidate["id"]), snap_week)
    ranked_by = None
    if ranker == "jev":
        judged, note, with_usage = jev_pass(espn, league, roster, target, scored, budget - spent)
        scored.sort(key=jev_order)
        promoted = sum(1 for c in scored if _jev_band(c) < 3)
        ranked_by = (f"{note}\nUsage data found for {with_usage} of {len(judged)} players. "
                     f"{promoted} rated confidently enough to move up; the rest keep projection order.")
        log_jev(week, judged)
    elif ranker == "shadow":
        # Copies, so nothing Jev says reaches the ranking, the bids or Claude's brief.
        judged, note, _ = jev_pass(espn, league, roster, target, [dict(c) for c in scored], budget - spent)
        print(f"(shadow {note.lower()}; logged {log_jev(week, judged)} judgments)")
    shortlist = shortlist_from(scored)
    for candidate in shortlist:
        candidate["bid"] = suggest_bid(candidate, budget_left, acquisition.get("minimumBid", 0), past)
    log_pool(week, pool, scored)
    if rate_shadow:
        import shadow   # here rather than at the top: shadow imports this module
        try:
            print(shadow.record(espn, league, team["id"], roster, pool, cards, slot_counts, games, byes, week,
                                scored, shortlist))
        except Exception as problem:  # broad on purpose: a shadow must not stop the report
            print(f"(rate shadow failed: {type(problem).__name__}: {problem}); report continues without it")
    runs = sorted({claim["processed"] for claim in claims if claim["processed"]})

    final_week = (league.get("status") or {}).get("finalScoringPeriod") or 0
    return {
        "week": week, "budget": budget, "budget_left": budget_left,
        "weeks_left": max(final_week - week + 1, 0),   # 0 when ESPN doesn't say when the season ends
        "minimum_bid": acquisition.get("minimumBid", 0), "rivals": rivals,
        "last_runs": runs, "past_claims": past,
        "holes_now": holes_now(roster, slot_counts, games, week),
        "gaps": bye_gaps(roster, slot_counts, horizon, week, scored),
        "candidates": shortlist, "ranked_by": ranked_by,
        "drops": drop_candidates(roster, target, slot_counts, horizon, week, replacement_level(scored)),
        "roster": roster, "target": target,
    }


def _per_game(player):
    return round(season_pace(player) / GAMES_PER_SEASON, 1)


def _projections(player):
    """The same fields for roster players as for free agents, so every comparison is like for like."""
    return {"name": player["name"], "position": player["position"],
            "week_projection": player["projection"], "level_per_game": _per_game(player)}


def brief_for_claude(context):
    """Just the decision-relevant numbers, not the whole roster dump."""
    return {
        "week": context["week"],
        "faab_left": context["budget_left"],
        "faab_pace": faab_pace(context),
        "faab_of_rivals": sorted(context["rivals"], reverse=True),
        "lineup_holes_this_week": context.get("holes_now", []),
        "lineup_holes_ahead": context["gaps"],
        "my_starters": [_projections(p) for p in context["roster"] if context["target"][p["id"]] not in (BENCH, IR)],
        "my_bench": [_projections(p) for p in context["roster"] if context["target"][p["id"]] == BENCH],
        "available": [{"name": c["name"], "position": c["position"],
                       "rostered_pct": c["percent_owned"], "rostered_pct_change": c["percent_change"],
                       "week_projection": c["projection"], "level_per_game": _per_game(c),
                       "lineup_gain_this_week": c["gain"],
                       f"lineup_gain_next_{LOOKAHEAD + 1}_weeks_byes_included": c.get("ahead_gain"),
                       "lineup_gain_rest_of_season": c["season_gain"],
                       "snap_share_pct_by_week": (c.get("snaps") or {}).get("by_week"),
                       "snap_trend": (c.get("snaps") or {}).get("trend"),
                       "jev_score": c.get("jev"), "jev_confidence": c.get("jev_confidence"),
                       "next_man_up": roles.describe(c) if c.get("role") else None,
                       "helps_only_this_week": this_week_only(c),
                       "availability": ("free agent: can be added today at no cost" if c.get("free_now")
                                        else "on waivers: goes to the highest bid at the next run"),
                       "drop_to_make_room": c["drop"]["name"] if c.get("drop") else None,
                       "suggested_bid": list(c["bid"])}
                      for c in context["candidates"]],
        "drop_candidates": [p["name"] for p in context["drops"]],
        "note_on_bids": bid_notes(context),
        "note_on_holes": ("The players named for a hole in a later week are listed, with bids, on the report of the "
                          "Tuesday that week starts, a day before its waiver run. Until then there is nothing to claim."),
        "note_on_levels": ("level_per_game is what a player scores a game on this season's evidence: his preseason "
                           "projection, moved by every game he has played. Every lineup gain past this week is "
                           "built on it. For tight ends, kickers and defenses it is ESPN's season projection per game."),
        "note_on_next_man_up": (f"A candidate with next_man_up set took over a backfield in his team's latest game, or "
                                f"is next in line behind a lead back who is ruled out. His lineup gains count him at "
                                f"{roles.FILL_IN} points a game for {roles.FILL_IN_WEEKS} weeks, the average for backs "
                                f"flagged this way; just under half reach 10 a game and a quarter stay under 5."),
        "note_on_drops": ("Each candidate already has the drop that costs least, measured against the "
                          "best free agent at that player's own position. Use it unless you can say why not."),
    }


def main(email, ranker="math", rate_shadow=False):
    espn = ESPN.from_env()
    context = gather(espn, ranker, rate_shadow)
    context["advice"], calls = split_calls(ask_claude(brief_for_claude(context)))
    attach_calls(context["candidates"], calls)
    # With a key set, a missing write-up is a failure worth seeing in the email, not just in the log.
    context["advice_failed"] = context["advice"] is None and bool(os.getenv("ANTHROPIC_API_KEY"))
    report = build_report(context)
    print(report)
    if email:
        subject = f"Waivers week {context['week']}: {headline(context['candidates'])}"
        blocks, thread = slack_layout(context, subject, report)
        mailer.send(subject, report, build_html(context), blocks=blocks, thread=thread)
    if rate_shadow and email:
        import shadow
        if shadow.is_summary_day():
            try:
                shadow.send(season=espn.season)
            except Exception as problem:  # broad on purpose: the report has already gone out
                print(f"(shadow comparison failed: {type(problem).__name__}: {problem})")


if __name__ == "__main__":
    try:
        main(email="--email" in sys.argv, ranker=ranker_from(sys.argv), rate_shadow="--rate-shadow" in sys.argv)
    except ESPNError as error:
        message = f"Error: {error}"
        print(message, file=sys.stderr)
        if "--email" in sys.argv:
            mailer.alert("waiver report failed", message)
        sys.exit(1)
