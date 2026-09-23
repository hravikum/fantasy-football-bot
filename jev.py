"""Optional free-agent scorer built on TypeSafe's Jev, used by `waivers.py --ranker jev`.

Jev knows only what we hand it, so the state is assembled deliberately: a few ESPN numbers
for one player plus a summary of the roster he would join. It answers typed questions with
calibrated confidence and cannot look anything up or generate text.

The weights that turn those answers into a ranking live here in code, so they can be
changed and tested without rewriting a prompt.
"""
import os
from pathlib import Path

from dotenv import load_dotenv

from espn import PRO_TEAMS

NEUTRAL = 0.5   # what a judgment is worth when Jev isn't confident in it
# The questions are about a player's role, which means nothing for a kicker or a defense.
# Jev said as much: its confidence on them came back under 0.15.
SCORED_POSITIONS = {"QB", "RB", "WR", "TE"}
DEPTH = 20      # how many of the math-ranked candidates get scored
WEIGHTS = {"role": 0.40, "rest_of_season": 0.35, "usage_growing": 0.15, "keeper": 0.10}


def questions():
    """Four judgments ESPN's numbers can't express. Jev answers all of them in one pass."""
    from typesafe_sdk import Noul, Score
    return {
        "role": Score(
            instructions="How large and secure this player's role on his NFL team is right now",
            criteria=["Inactive, or buried on the depth chart",
                      "Situational or rotational snaps",
                      "Splits the work in a committee",
                      "Primary option at his position",
                      "Featured every-down player"]),
        "rest_of_season": Score(
            instructions="How much this player would help the fantasy roster described in the state, "
                         "over the rest of this season",
            criteria=["Not worth a roster spot",
                      "Bye week or injury cover only",
                      "Useful flex and depth",
                      "Would start most weeks",
                      "Would be among the best players on this roster"]),
        "usage_growing": Noul(
            instructions="This player's role has grown recently"),
        "keeper": Noul(
            instructions="This player is worth keeping for next season in a 14-team league "
                         "where each team keeps one player"),
    }


def usage_summary(weeks, position):
    """Arithmetic in code, judgment to Jev: it is unreliable at counting and comparing numbers.

    Opportunities are what a role looks like in the box score: pass attempts for a
    quarterback, carries plus targets for everyone else.
    """
    if not weeks:
        return None
    count = (lambda w: w["pass_attempts"]) if position == "QB" else (lambda w: w["carries"] + w["targets"])
    series = [int(count(week)) for week in weeks]
    if len(series) < 2:
        trend = "one game so far"
    else:
        earlier = sum(series[:-1]) / len(series[:-1])
        latest = series[-1]
        trend = "rising" if latest >= earlier * 1.25 + 1 else "falling" if latest <= earlier * 0.75 - 1 else "steady"
    return {"measure": "pass attempts" if position == "QB" else "carries plus targets",
            "by_week": series, "per_game": round(sum(series) / len(series), 1), "trend": trend}


def player_state(candidate, roster, league):
    """Only the fields the questions need: padding the state costs accuracy."""
    state = {
        "player": {
            "name": candidate["name"],
            "position": candidate["position"],
            "nfl_team": PRO_TEAMS.get(candidate["pro_team"], "unknown"),
            "injury_status": candidate["injury_status"],
            "rostered_in_espn_leagues_pct": candidate["percent_owned"],
            "recent_change_in_rostered_pct": candidate["percent_change"],
            "espn_projection_this_week": candidate["projection"],
            "espn_projection_full_season": candidate["season_projection"],
        },
        "my_roster": roster,
        "league": league,
    }
    if candidate.get("usage"):
        state["player"]["actual_usage_this_season"] = candidate["usage"]
    if candidate.get("snaps"):
        state["player"]["snap_share"] = {
            "percent_by_week": candidate["snaps"]["by_week"],   # null: he didn't play that week
            "trend": candidate["snaps"]["trend"],
        }
    return state


def composite(answers):
    """Weighted blend of the judgments, shrunk toward neutral when Jev isn't confident.

    Returns (score, confidence). Jev's confidence is calibrated, so an unsure judgment
    should move the ranking less than a sure one rather than counting the same.
    """
    parts, confidences = {}, []
    for name, answer in answers.items():
        if hasattr(answer, "noul"):
            parts[name] = answer.noul
        else:
            levels = max(len(answer.probabilities) - 1, 1)
            parts[name] = answer.score / levels
            confidences.append(answer.confidence)
    raw = sum(WEIGHTS[name] * value for name, value in parts.items() if name in WEIGHTS)
    confidence = sum(confidences) / len(confidences) if confidences else 1.0
    return round(confidence * raw + (1 - confidence) * NEUTRAL, 3), round(confidence, 2)


def roster_summary(roster, target, bench_slot, reserve_slot):
    """The roster as short strings: enough for a judgment, small enough to stay accurate."""
    describe = lambda p: f"{p['name']} ({p['position']}, season projection {p['season_projection'] or 0:.0f})"
    return {
        "starters": [describe(p) for p in roster if target[p["id"]] not in (bench_slot, reserve_slot)],
        "bench": [describe(p) for p in roster if target[p["id"]] == bench_slot],
    }


def score(candidates, roster, league, client=None, depth=DEPTH):
    """Score the top candidates in place. Returns a line describing what happened."""
    if client is None:
        try:
            from typesafe_sdk import TypeSafeClient
        except ImportError:
            return "Jev ranking skipped: pip install typesafe-sdk"
        load_dotenv(Path(__file__).with_name(".env"))
        if not os.getenv("TYPESAFE_API_KEY"):
            return "Jev ranking skipped: no TYPESAFE_API_KEY in .env"
        client = TypeSafeClient(timeout=30.0)  # a scheduled run must not hang on one player

    asked = questions()
    scoring = candidates[:depth]
    model, failed = None, 0
    for candidate in scoring:
        try:
            answer = client.system_one(state=player_state(candidate, roster, league), questions=asked)
        except Exception as problem:  # broad on purpose: a scorer must not take down the report
            failed += 1
            print(f"(Jev call failed for {candidate['name']}: {type(problem).__name__})")
            continue
        model = answer.model
        candidate["jev"], candidate["jev_confidence"] = composite(answer.answers)

    if not scoring:
        return "Jev ranking skipped: nothing to score"
    if failed == len(scoring):
        return "Jev ranking failed; falling back to the projection ranking"
    if failed:
        return f"Ranked by Jev ({model}), {failed} of {len(scoring)} calls failed"
    return f"Ranked by Jev ({model})"
