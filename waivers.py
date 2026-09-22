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
import json
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
from espn import BENCH, ESPN, IR, PRO_TEAMS, ESPNError, parse_roster_entry
from lineup import CANT_PLAY, plan, starters_total

JEV_LOG = Path(__file__).with_name("logs") / "jev_scores.csv"
JEV_LOG_FIELDS = ["logged_at", "week", "player_id", "name", "position", "jev", "jev_confidence", "math_rank",
                  "projection", "season_projection", "percent_owned", "usage_per_game", "usage_trend"]
MODEL = "claude-sonnet-5"
# Sonnet 5 thinks by default, and max_tokens caps thinking and text together, so a
# budget sized for the text alone runs out partway through. It's a ceiling, not a spend.
MAX_TOKENS = 16000
MAX_CONTINUATIONS = 3  # a paused web-search turn is resumed, up to this many times
POOL_SIZE = 75      # free agents to score, most widely rostered first
SHORTLIST = 8       # how many make the report
PER_POSITION = 2    # at most this many of any one position, so the list never becomes all QBs


def season_view(players):
    """The same players scored by season projection, with nobody on a bye.

    Used for the rest-of-season comparison: raw season totals can't be compared across
    positions, but what a player adds to your season-long lineup can.
    """
    return [dict(p, projection=p["season_projection"] or 0.0) for p in players]


def best_lineup_value(players, slot_counts, games):
    return starters_total(players, plan(players, slot_counts, games)[0], games)


def candidates(roster, pool, slot_counts, games):
    """The projection ranking, shortlisted. The default."""
    return shortlist_from(score_pool(roster, pool, slot_counts, games))


def shortlist_from(scored):
    """Top of the list, capped per position so it never becomes eight of one thing."""
    shortlist, seen = [], Counter()
    for candidate in scored:
        if seen[candidate["position"]] < PER_POSITION:
            shortlist.append(candidate)
            seen[candidate["position"]] += 1
        if len(shortlist) == SHORTLIST:
            break
    return shortlist


def score_pool(roster, pool, slot_counts, games):
    """Every free agent ranked by what he would add now, then by what he'd add all season."""
    base = best_lineup_value(roster, slot_counts, games)
    every_team = {p["pro_team"]: (0, None) for p in roster + pool}
    season_base = best_lineup_value(season_view(roster), slot_counts, every_team)

    scored = []
    for free_agent in pool:
        if free_agent["injury_status"] in CANT_PLAY:
            continue
        combined = roster + [free_agent]
        target, _ = plan(combined, slot_counts, games)
        season_gain = best_lineup_value(season_view(combined), slot_counts, every_team) - season_base
        scored.append(dict(free_agent,
                           gain=round(starters_total(combined, target, games) - base, 1),
                           season_gain=round(season_gain),
                           would_start=target[free_agent["id"]] not in (BENCH, IR)))
    scored.sort(key=lambda c: (-c["gain"], -c["season_gain"], -c["percent_owned"]))
    for rank, candidate in enumerate(scored, start=1):
        candidate["math_rank"] = rank
    return scored


BANDS = [(0.20, 0.35), (0.08, 0.15), (0.02, 0.05)]   # starter, useful piece, stash


def _projection_band(candidate):
    gain, season_gain = candidate["gain"], candidate["season_gain"]
    if gain >= 3.0 and season_gain > 0:
        return 0
    if gain >= 1.5:
        return 1
    if gain > 0 or season_gain >= 15:
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


def suggest_bid(candidate, budget_left, minimum_bid):
    """Bid bands as a share of what's left, taking whichever signal rates him higher."""
    band = min(_projection_band(candidate), _jev_band(candidate))
    if band == 3:
        return minimum_bid, max(minimum_bid, 1)
    low, high = BANDS[band]
    return max(minimum_bid, round(budget_left * low)), max(minimum_bid + 1, round(budget_left * high))


def jev_order(candidate):
    """Jev can promote a player it is confident about; it never demotes one.

    Same rule as the bids. When Jev is unsure, which it is when the state is thin, the
    list is exactly the projection list rather than noise sorted around 0.5.
    """
    confident = _jev_band(candidate) < 3
    return (not confident, -(candidate.get("jev") or 0) if confident else 0, candidate["math_rank"])


def drop_candidates(roster, target, slot_counts, games, count=3):
    """Who you'd cut to make room: on the bench, cheapest first, and only if you can still field a lineup."""
    spare = [p for p in roster if target[p["id"]] == BENCH]
    spare.sort(key=lambda p: (p["season_projection"] or 0, p["projection"] or 0))
    needed = {slot: n for slot, n in slot_counts.items() if n and slot not in (BENCH, IR)}
    droppable = []
    for person in spare:
        filled = Counter(plan([p for p in roster if p["id"] != person["id"]], slot_counts, games)[0].values())
        if all(filled.get(slot, 0) >= n for slot, n in needed.items()):
            droppable.append(person)
        if len(droppable) == count:
            break
    return droppable


def bye_gaps(roster, target, byes, week, ahead=3):
    """Weeks where a starting position runs thin because of byes."""
    starters = [p for p in roster if target[p["id"]] not in (BENCH, IR)]
    gaps = []
    for future in range(week + 1, week + 1 + ahead):
        out = Counter(p["position"] for p in starters if future in byes.get(p["pro_team"], set()))
        for position, missing in sorted(out.items()):
            cover = sum(1 for p in roster if p["position"] == position
                        and target[p["id"]] == BENCH and future not in byes.get(p["pro_team"], set()))
            if cover < missing:
                gaps.append(f"week {future}: {missing} {position} on bye, {cover} on the bench to cover")
    return gaps


def _post(body, headers):
    """One retry for a dropped connection: a single blip shouldn't cost the day's write-up."""
    for attempt in range(2):
        try:
            return requests.post("https://api.anthropic.com/v1/messages", json=body, headers=headers, timeout=120)
        except requests.exceptions.ConnectionError:
            if attempt:
                raise
            time.sleep(3)


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
        "season). Waivers process daily, so claims are cheap to make but the wire is thin with 14 teams.\n\n"
        f"Here is today's data:\n{json.dumps(brief, indent=1)}\n\n"
        "Recommend at most three claims, and none is a good answer when nothing qualifies. Every claim "
        "needs a reason: a positive lineup gain in the data, news you found and cite, or a bye gap it "
        "fills. For each: the player, a bid inside the suggested range (or a different number with a "
        "reason), and who to drop. Compare players only with numbers that appear in the data, never an "
        "estimate of one that doesn't. Say plainly what to skip and why. "
        "Mention keeper value only when it genuinely changes the call. If you search, check the last few "
        "days for injury, role, or discipline news on these players, and say when news takes effect: "
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
    lines.append("")

    if context["gaps"]:
        lines += ["Coming up:"] + [f"  {gap}" for gap in context["gaps"]] + [""]

    if context.get("ranked_by"):
        lines += [context["ranked_by"], ""]
    jev_column = any("jev" in c for c in context["candidates"])
    header = f"{'PLAYER':<22} {'POS':<5} {'TEAM':<5} {'OWN%':>6} {'WK':>5} {'NOW':>5} {'SEASON':>7}"
    lines.append(header + ("  JEV  CONF  PROJ#  BID" if jev_column else "  BID"))
    for candidate in context["candidates"]:
        low, high = candidate["bid"]
        bid = f"${low}" if low == high else f"${low}-{high}"
        row = (f"{candidate['name'][:22]:<22} {candidate['position']:<5} "
               f"{PRO_TEAMS.get(candidate['pro_team'], '?'):<5} "
               f"{candidate['percent_owned']:>5.0f}% {candidate['projection'] or 0:>5.1f} "
               f"{candidate['gain']:>+5.1f} {candidate['season_gain']:>+7.0f}")
        if jev_column:
            judged = "jev" in candidate  # kickers and defenses are never asked
            row += (f" {candidate['jev']:>5.2f} {candidate['jev_confidence']:>5.2f}" if judged else f" {'-':>5} {'-':>5}")
            row += f" {candidate.get('math_rank', 0):>6}"
        lines.append(f"{row}  {bid}")
    lines += ["", "Drop first: " + ", ".join(
        f"{p['name']} ({p['season_projection'] or 0:.0f})" for p in context["drops"])]

    if context["advice"]:
        lines += ["", "-" * 60, "", context["advice"]]
    lines += ["", "Bids are suggestions only. Place claims yourself in ESPN."]
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


def ranker_from(argv):
    if "--ranker" in argv and "jev" in argv:
        return "jev"
    if "--jev-shadow" in argv:
        return "shadow"
    return "math"


def gather(espn, ranker="math"):
    league = espn.league("mSettings", "mTeam")
    settings = league["settings"]
    week = league["scoringPeriodId"]
    team = espn.my_team(league["teams"])
    slot_counts = {int(k): v for k, v in settings["rosterSettings"]["lineupSlotCounts"].items()}
    roster = [parse_roster_entry(e, week, espn.season) for e in espn.roster_entries(team["id"], week)]
    games = espn.pro_games(week)
    byes = espn.bye_weeks()
    target, _ = plan(roster, slot_counts, games)

    acquisition = settings["acquisitionSettings"]
    budget = acquisition.get("acquisitionBudget", 0)
    spent = team.get("transactionCounter", {}).get("acquisitionBudgetSpent", 0)
    rivals = [budget - t.get("transactionCounter", {}).get("acquisitionBudgetSpent", 0)
              for t in league["teams"] if t["id"] != team["id"]]
    budget_left = budget - spent

    scored = score_pool(roster, espn.free_agents(week, POOL_SIZE), slot_counts, games)
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
        candidate["bid"] = suggest_bid(candidate, budget_left, acquisition.get("minimumBid", 0))
    runs = espn.waiver_runs(range(max(1, week - 2), week + 1))

    return {
        "week": week, "budget": budget, "budget_left": budget_left,
        "minimum_bid": acquisition.get("minimumBid", 0), "rivals": rivals,
        "last_runs": runs,
        "gaps": bye_gaps(roster, target, byes, week),
        "candidates": shortlist, "ranked_by": ranked_by,
        "drops": drop_candidates(roster, target, slot_counts, games),
        "roster": roster, "target": target,
    }


def _projections(player):
    """The same fields for roster players as for free agents, so every comparison is like for like."""
    return {"name": player["name"], "position": player["position"],
            "week_projection": player["projection"], "season_projection": player["season_projection"]}


def brief_for_claude(context):
    """Just the decision-relevant numbers, not the whole roster dump."""
    return {
        "week": context["week"],
        "faab_left": context["budget_left"],
        "faab_of_rivals": sorted(context["rivals"], reverse=True),
        "bye_gaps": context["gaps"],
        "my_starters": [_projections(p) for p in context["roster"] if context["target"][p["id"]] not in (BENCH, IR)],
        "my_bench": [_projections(p) for p in context["roster"] if context["target"][p["id"]] == BENCH],
        "available": [{"name": c["name"], "position": c["position"],
                       "rostered_pct": c["percent_owned"], "rostered_pct_change": c["percent_change"],
                       "week_projection": c["projection"], "season_projection": c["season_projection"],
                       "lineup_gain_this_week": c["gain"], "lineup_gain_rest_of_season": c["season_gain"],
                       "jev_score": c.get("jev"), "jev_confidence": c.get("jev_confidence"),
                       "suggested_bid": list(c["bid"])}
                      for c in context["candidates"]],
        "drop_candidates": [p["name"] for p in context["drops"]],
    }


def main(email, ranker="math"):
    espn = ESPN.from_env()
    context = gather(espn, ranker)
    context["advice"] = ask_claude(brief_for_claude(context))
    report = build_report(context)
    print(report)
    if email:
        worth_it = [c for c in context["candidates"] if c["gain"] > 0]
        headline = f"{len(worth_it)} worth a claim" if worth_it else "nothing worth a claim"
        mailer.send(f"Waivers week {context['week']}: {headline}", report)


if __name__ == "__main__":
    try:
        main(email="--email" in sys.argv, ranker=ranker_from(sys.argv))
    except ESPNError as error:
        message = f"Error: {error}"
        print(message, file=sys.stderr)
        if "--email" in sys.argv:
            mailer.send("Fantasy bot: waiver report failed", message)
        sys.exit(1)
