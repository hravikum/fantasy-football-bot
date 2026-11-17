"""Step 4: the daily waiver report.

Read-only. It ranks the free agent pool by what each player would add to your starting
lineup, suggests a bid range, asks Claude to turn that into recommendations, and emails
it. It never places a claim.

Usage:
  python3 waivers.py                  print the report
  python3 waivers.py --email          print it and send it
  python3 waivers.py --ranker jev     rank the pool with Jev instead of projections
  python3 waivers.py --jev-shadow     score with Jev and log it, but rank and email as normal
"""
import csv
import html as html_escape
import json
import re
import os
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

import requests
from dotenv import load_dotenv

import jev
import mailer
import roles
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

    A next man up is listed even when he adds nothing to your lineup: the league bids on him the
    night after the game that flagged him, and by the time he looks good on paper he is gone.
    """
    shortlist, seen = [], Counter()
    for candidate in scored:
        if held(candidate):
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
                           weeks_helped=sum(1 for points in by_week if points > 0),
                           season_gain=round(season_gain),
                           drop=dropped,
                           would_start=by_week[0] > 0))
    scored.sort(key=lambda c: (-c["ahead_gain"], -c["season_gain"], -c["percent_owned"]))
    for rank, candidate in enumerate(scored, start=1):
        candidate["math_rank"] = rank
    return scored


BANDS = [(0.20, 0.35), (0.08, 0.15), (0.02, 0.05)]   # starter, useful piece, stash: used until
MIN_CLAIMS = 10        # this many claims have been seen; then your league's own prices take over
# How badly you want him, as a percentile of what winning claims actually cost here.
TIER_PERCENTILES = {0: (0.90, 0.97), 1: (0.60, 0.80), 2: (0.20, 0.50)}
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
        band = 0   # he starts for somebody while the lead is down, and that is who you are bidding against
    if candidate.get("position") in STREAM_ONLY:
        band = max(band, 2)   # there's always another one on the wire, so never real money
    return band


def suggest_bid(candidate, budget_left, minimum_bid, claim_prices=()):
    """What to bid: what claims actually cost here, scaled by how much you want him.

    Until this league has priced enough claims, falls back to a share of the budget.
    """
    band = bid_band(candidate)
    if band == 3:
        return minimum_bid, max(minimum_bid, 1)
    if len(claim_prices) >= MIN_CLAIMS:
        low_mark, high_mark = TIER_PERCENTILES[band]
        low = round(percentile(claim_prices, low_mark))
        high = round(percentile(claim_prices, high_mark))
        high = max(high, low + 1 if band < 2 else low)   # a claim you want should outbid the last one
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
    at the same position, never with a skill player.
    """
    joined = roster + [candidate]
    placed, _ = plan(joined, slot_counts, horizon[0][1])
    holes = [sum(shortfalls(projected_for(joined, w, this_week), slot_counts, g).values()) for w, g in horizon]
    spare = sorted((p for p in joined if p["id"] != candidate["id"] and placed[p["id"]] == BENCH),
                   key=lambda p: (surplus(p, replacement), season_pace(p)))
    if candidate["position"] in STREAM_ONLY:
        same = sorted((p for p in joined if p["id"] != candidate["id"]
                       and p["position"] == candidate["position"]),
                      key=lambda p: (surplus(p, replacement), season_pace(p)))
        spare = same + [p for p in spare if p["position"] != candidate["position"]]
    for person in spare:
        rest = [p for p in joined if p["id"] != person["id"]]
        after = [sum(shortfalls(projected_for(rest, w, this_week), slot_counts, g).values()) for w, g in horizon]
        if all(a <= b for a, b in zip(after, holes)):
            return person
    return None


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
        "Recommend at most three claims, and none is a good answer when nothing qualifies. Every claim "
        "needs a reason: a positive lineup gain in the data, news you found and cite, or a next_man_up note. "
        "For a next_man_up, search first for how long the lead back is expected to "
        "miss: if it is weeks, the claim is a priority even as a bench stash, and if he is due back at once, say so "
        "and bid low. A hole in a later week is not a claim today: name the week and leave the claim for "
        "that week, unless the player would also start for you before then. A kicker or a defense replaces "
        "the one you have; never drop another position to carry two. A candidate whose availability says free "
        "agent should be added today, before someone else does, with no bid. For each: the player, a bid inside "
        "the suggested range (or a different number with a reason), and the drop. Every candidate comes with the drop that costs least; use it unless "
        "you have a reason to prefer another, and give that reason. Compare players only with numbers that appear in the data, never an "
        "estimate of one that doesn't. Say plainly what to skip and why. "
        "Mention keeper value only when it genuinely changes the call. If you search, check the last few "
        "days for injury, role, or discipline news on these players (a snap share that jumped is often the "
        "first sign of a role change; null in the weekly list means he didn't play), and say when news takes effect: "
        "a pending suspension is not an absence this week. Under 250 words, no preamble, no headings."
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
FREE_NOTE = ("\"add now\": he has cleared waivers, so he costs nothing and goes to whoever adds him first. "
             "Everyone else is decided by bid at the next run.")


def _bid(candidate, dash="-"):
    """The bid as the reports show it. A free agent who has cleared waivers needs none."""
    if candidate.get("free_now"):
        return "add now"
    low, high = candidate["bid"]
    return f"${low}" if low == high else f"${low}{dash}{high}"


def _level(player):
    """His level as the reports show it; a dash where values.py keeps ESPN's number (tight ends, kickers,
    defenses) and for anyone ESPN sent no games for."""
    return "-" if player.get("level") is None else f"{player['level']:.1f}"


def build_report(context):
    lines = [f"Waiver report — {datetime.now():%a %b %d}, week {context['week']}", ""]
    lines.append(f"Your FAAB: ${context['budget_left']} of ${context['budget']}"
                 f"   (minimum bid ${context['minimum_bid']})")
    if context["rivals"]:
        rich = sorted(context["rivals"], reverse=True)[:3]
        lines.append("Rivals with the most FAAB: " + ", ".join(f"${r}" for r in rich))
    if context["last_runs"]:
        stamps = [run.strftime("%a %I:%M %p").replace(" 0", " ") for run in context["last_runs"][-3:]]
        lines.append("Claims processed: " + ", ".join(stamps))
    prices = context.get("claim_prices") or []
    if len(prices) >= MIN_CLAIMS:
        lines.append(f"Bids priced off {len(prices)} claims in this league: median ${percentile(prices, 0.5):.0f}, "
                     f"top of market ${max(prices):.0f}")
    else:
        lines.append(f"Bids are budget shares until this league prices {MIN_CLAIMS} claims "
                     f"({len(prices)} so far)")
    lines.append("")

    if context["gaps"]:
        lines += [f"Lineup holes in the next {LOOKAHEAD} weeks:"] + [f"  {gap}" for gap in context["gaps"]]
        lines += [f"  {HELD_NOTE}", ""]

    stepping_in = [c for c in context["candidates"] if c.get("role")]
    if stepping_in:
        lines += ["Next man up:"] + [f"  {roles.describe(c)}" for c in stepping_in]
        lines += [f"  Backs flagged this way averaged {roles.FILL_IN} a game over their next {roles.FILL_IN_WEEKS} weeks "
                  f"in 2025-26. Just under", "  half reached 10 a game and a quarter stayed under 5. "
                  "They are bid as starters and listed even with no lineup gain.", ""]

    if context.get("ranked_by"):
        lines += [context["ranked_by"], ""]
    jev_column = any("jev" in c for c in context["candidates"])
    header = (f"{'PLAYER':<20} {'POS':<5} {'TEAM':<5} {'OWN%':>6} {'+/-':>6} {'SNAP%':>7} {'WK':>5} {'LEVEL':>5} "
              f"{'NOW':>5} {str(LOOKAHEAD + 1) + 'WK':>5} {'SEASON':>7}")
    lines.append(header + ("  JEV  CONF  PROJ#  BID" if jev_column else "  BID") + "    DROP FOR HIM")
    for candidate in context["candidates"]:
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
        drop = candidate["drop"]["name"] if candidate.get("drop") else "-"
        lines.append(f"{row}  {bid:<7} {drop[:18]}")

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
    headers = [_column("Player"), _column("Rostered", True), _column("+/-", True), _column("Snaps", True),
               _column("Level", True), _column("This wk", True),
               _column(f"Next {LOOKAHEAD + 1}", True), _column("Season", True)]
    if jev_column:
        headers += [_column("Jev", True), _column("Conf", True)]
    headers += [_column("Bid"), _column("Drop for him")]

    rows = []
    for index, candidate in enumerate(context["candidates"]):
        cells = [
            _cell(f'<strong>{html_escape.escape(candidate["name"])}</strong>'
                  f'<div style="color:#6b7280;font-size:12px">{candidate["position"]} &middot; '
                  f'{PRO_TEAMS.get(candidate["pro_team"], "?")}</div>'),
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
        drop = candidate["drop"]["name"] if candidate.get("drop") else "&mdash;"
        cells.append(_cell(html_escape.escape(drop) if candidate.get("drop") else drop, "color:#374151"))
        stripe = "background:#fafafa" if index % 2 else ""
        rows.append(f'<tr style="{stripe}">' + "".join(cells) + "</tr>")

    holes = ""
    if context["gaps"]:
        items = "".join(f'<li style="margin:2px 0">{html_escape.escape(gap)}</li>' for gap in context["gaps"])
        holes = (f'<div style="background:#fff7ed;border-left:3px solid #fb923c;padding:10px 14px;margin:18px 0">'
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
                  f'reached 10 a game and a quarter stayed under 5. Bid as starters, listed even with no lineup gain.'
                  f'</div></div>')

    summary = (f'FAAB <strong>${context["budget_left"]}</strong> of ${context["budget"]} '
               f'&middot; minimum bid ${context["minimum_bid"]}')
    if context["rivals"]:
        summary += " &middot; rivals&rsquo; most: " + ", ".join(
            f"${amount}" for amount in sorted(context["rivals"], reverse=True)[:3])
    if context["last_runs"]:
        stamps = [run.strftime("%a %I:%M %p").replace(" 0", " ") for run in context["last_runs"][-3:]]
        summary += "<br>Claims processed: " + ", ".join(stamps)

    ranked = (f'<p style="color:#6b7280;font-size:13px;margin:0 0 10px">'
              f'{html_escape.escape(context["ranked_by"]).replace(chr(10), "<br>")}</p>'
              if context.get("ranked_by") else "")
    free_note = f"{FREE_NOTE} " if any(c.get("free_now") for c in context["candidates"]) else ""
    write_up = context.get("advice") or (NO_WRITE_UP if context.get("advice_failed") else "")
    advice = _prose(write_up) if write_up else ""

    return (f'<div style="font-family:-apple-system,BlinkMacSystemFont,\'Segoe UI\',Roboto,Helvetica,Arial,'
            f'sans-serif;color:#111827;max-width:720px;margin:0 auto;padding:4px 8px">'
            f'<h2 style="margin:0 0 4px;font-size:19px">Waiver report &middot; week {context["week"]}</h2>'
            f'<p style="color:#4b5563;font-size:14px;margin:0 0 4px">{summary}</p>'
            f'{holes}{ranked}'
            f'<table style="border-collapse:collapse;width:100%;margin-bottom:18px">'
            f'<thead><tr>{"".join(headers)}</tr></thead><tbody>{"".join(rows)}</tbody></table>'
            f'{advice}'
            f'<p style="color:#6b7280;font-size:12px;border-top:1px solid #e5e7eb;padding-top:10px">'
            f'{free_note}Bids are suggestions only. Place claims yourself in ESPN.</p></div>')


def faab_spent(team, spent_by_team):
    """ESPN's spend counter reads zero in some leagues; the claims themselves are the honest record."""
    counter = team.get("transactionCounter", {}).get("acquisitionBudgetSpent") or 0
    return max(counter, spent_by_team.get(team["id"], 0))


def gather(espn, ranker="math"):
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
    claims = espn.waiver_claims(range(1, week + 1))
    prices = [claim["bid"] for claim in claims]
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
    try:
        roles.mark(pool, roster + rival_backs, cards, espn.season,
                   roles.finished_weeks(week, byes, games, datetime.now()))
    except Exception as problem:  # broad on purpose: a missing extra must not stop the report
        print(f"(next-man-up check failed: {type(problem).__name__}: {problem}); report continues without it")
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
        candidate["bid"] = suggest_bid(candidate, budget_left, acquisition.get("minimumBid", 0), prices)
    log_pool(week, pool, scored)
    runs = sorted({claim["processed"] for claim in claims if claim["processed"]})

    return {
        "week": week, "budget": budget, "budget_left": budget_left,
        "minimum_bid": acquisition.get("minimumBid", 0), "rivals": rivals,
        "last_runs": runs, "claim_prices": prices,
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
        "faab_of_rivals": sorted(context["rivals"], reverse=True),
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
                       "availability": ("free agent: can be added today at no cost" if c.get("free_now")
                                        else "on waivers: goes to the highest bid at the next run"),
                       "drop_to_make_room": c["drop"]["name"] if c.get("drop") else None,
                       "suggested_bid": list(c["bid"])}
                      for c in context["candidates"]],
        "drop_candidates": [p["name"] for p in context["drops"]],
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


def main(email, ranker="math"):
    espn = ESPN.from_env()
    context = gather(espn, ranker)
    context["advice"] = ask_claude(brief_for_claude(context))
    # With a key set, a missing write-up is a failure worth seeing in the email, not just in the log.
    context["advice_failed"] = context["advice"] is None and bool(os.getenv("ANTHROPIC_API_KEY"))
    report = build_report(context)
    print(report)
    if email:
        worth_it = [c for c in context["candidates"] if c["gain"] > 0 or c.get("role")]
        headline = f"{len(worth_it)} worth a claim" if worth_it else "nothing worth a claim"
        mailer.send(f"Waivers week {context['week']}: {headline}", report, build_html(context))


if __name__ == "__main__":
    try:
        main(email="--email" in sys.argv, ranker=ranker_from(sys.argv))
    except ESPNError as error:
        message = f"Error: {error}"
        print(message, file=sys.stderr)
        if "--email" in sys.argv:
            mailer.send("Fantasy bot: waiver report failed", message)
        sys.exit(1)
