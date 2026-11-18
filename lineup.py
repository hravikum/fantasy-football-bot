"""Step 3: lineup and IR manager.

Works out the best legal lineup, and with --live sends the moves to ESPN, reads the
roster back to confirm them, and emails what changed.

Usage:
  python3 lineup.py           practice mode, changes nothing
  python3 lineup.py --live    apply the moves it judges worth making
  python3 lineup.py --live --force   apply the best lineup even below the point bar
  python3 lineup.py --live --near-kickoff   the same, but only if a game kicks off within
                                            95 minutes; silent otherwise (the 15-minute job)
"""
import json
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import requests

import mailer
from espn import BENCH, ESPN, IR, SLOT_NAMES, SLOT_ORDER, ESPNError, ESPNWriteRejected, parse_roster_entry

# Over the 2025 season (accuracy.py 2025), one projected point was worth about 0.9 real points at
# RB, WR and TE, so an upgrade held back costs nearly its whole edge. The bar only stops the
# lineup flipping back and forth as projections wobble through the week. Defense projections
# barely track results, but a projected point was still worth 0.65 real ones over 2025 and 0.73
# over 2026's first five weeks, so defenses follow projections like every other slot.
MIN_GAIN = 0.5
# Kickers don't: a projected point was worth +1.04 real ones over 2025 and -1.70 over 2026's first
# five weeks, about zero together. Swapping one kicker who plays for another is a coin flip that
# the waiver report would spend FAAB chasing, so a kicker who plays keeps his slot.
STEADY_SLOTS = {17}  # K
CANT_PLAY = {"OUT", "INJURY_RESERVE", "SUSPENSION"}
HEALTHY = {"ACTIVE", "NORMAL"}
IR_ELIGIBLE = {"OUT", "INJURY_RESERVE"}                   # ESPN allows only the O and IR tags on IR
IR_MAY_STAY = IR_ELIGIBLE | {"QUESTIONABLE", "DOUBTFUL"}  # an upgrade to Q or D doesn't invalidate the roster
FLEX = 23
KICKOFF_WINDOW = timedelta(minutes=95)     # inactives come out 90 minutes before kickoff
SCHEDULE = Path(__file__).with_name("cache") / "kickoffs.json"
SCHEDULE_MAX_AGE = 24 * 3600               # kickoffs only move when the NFL flexes a game


def expected_points(player, games):
    """What he'd score if started. Zero when he can't play."""
    if player["injury_status"] in CANT_PLAY or player["pro_team"] not in games:
        return 0.0
    return player["projection"] or 0.0


def kickoff_of(player, games):
    """When his game starts, or None if he has no game, or no time for it, this week."""
    game = games.get(player["pro_team"])
    return game[1] if game else None


def plan(players, slot_counts, games):
    """Target slot for every player, plus anyone stuck on IR with nowhere to go.

    Slots are filled most-restrictive first, which is optimal while slot eligibility stays
    nested the way ESPN defines it. Locked players keep the slot they are already in, and so
    does a kicker who plays (STEADY_SLOTS).
    """
    starting = {s: n for s, n in slot_counts.items() if n and s not in (BENCH, IR)}
    capacity = sum(starting.values()) + slot_counts.get(BENCH, 0)

    # IR first: who stays, who gets stashed, who has to come back.
    stays = [p for p in players if p["slot"] == IR and p["injury_status"] in IR_MAY_STAY]
    room = slot_counts.get(IR, 0) - len(stays)
    candidates = sorted((p for p in players if p["slot"] != IR and not p["locked"]
                         and p["injury_status"] in IR_ELIGIBLE),
                        key=lambda p: (p["injury_status"] != "INJURY_RESERVE", -(p["season_projection"] or 0)))
    reserve = {p["id"]: IR for p in stays + candidates[:max(room, 0)]}

    active = [p for p in players if p["id"] not in reserve]
    blocked = []
    while len(active) > capacity:
        returning = [p for p in active if p["slot"] == IR]
        if not returning:
            break  # roster is over capacity for some other reason; leave it to the human
        worst = min(returning, key=lambda p: p["season_projection"] or 0)
        reserve[worst["id"]] = IR
        active.remove(worst)
        blocked.append(worst)

    lineup = dict(reserve)
    for player in active:
        if player["locked"] or (player["slot"] in STEADY_SLOTS and expected_points(player, games) > 0):
            lineup[player["id"]] = player["slot"]
            if player["slot"] in starting:
                starting[player["slot"]] -= 1

    # Best first; on a tie keep whoever is already starting, so we don't churn for nothing.
    available = sorted((p for p in active if p["id"] not in lineup),
                       key=lambda p: (-expected_points(p, games), p["slot"] in (BENCH, IR)))
    for slot in sorted(starting, key=lambda s: sum(1 for p in players if s in p["eligible_slots"])):
        for _ in range(starting[slot]):
            pick = next((p for p in available if slot in p["eligible_slots"]), None)
            if pick is None:
                break
            available.remove(pick)
            lineup[pick["id"]] = slot
    for player in available:
        lineup[player["id"]] = BENCH
    return later_kickoffs_to_flex(players, lineup, games), blocked


def later_kickoffs_to_flex(players, lineup, games):
    """Same starters, same points, but later kickoffs move into FLEX wherever a swap is legal.

    A late scratch in FLEX can be replaced by anyone on the bench whose game hasn't started;
    stuck in a running back slot, only by a running back.
    """
    starters = [p for p in players if not p["locked"] and lineup[p["id"]] not in (BENCH, IR)
                and kickoff_of(p, games)]
    swapped = True
    while swapped:
        swapped = False
        for flex in [p for p in starters if lineup[p["id"]] == FLEX]:
            later = [p for p in starters if lineup[p["id"]] != FLEX and FLEX in p["eligible_slots"]
                     and lineup[p["id"]] in flex["eligible_slots"]
                     and kickoff_of(p, games) > kickoff_of(flex, games)]
            if later:
                latest = max(later, key=lambda p: kickoff_of(p, games))
                lineup[flex["id"]], lineup[latest["id"]] = lineup[latest["id"]], FLEX
                swapped = True
    return lineup


def later_in_flex(players, target, games):
    """Whether the target lineup has later kickoffs in FLEX than the lineup as it stands."""
    def flex_kickoffs(lineup):
        return sorted((kickoff_of(p, games) for p in players
                       if lineup[p["id"]] == FLEX and kickoff_of(p, games)), reverse=True)
    return flex_kickoffs(target) > flex_kickoffs({p["id"]: p["slot"] for p in players})


def starters_total(players, lineup, games):
    return sum(expected_points(p, games) for p in players if lineup[p["id"]] not in (BENCH, IR))


def decide(players, slot_counts, games):
    """Target lineup, the moves to reach it, points gained, dead starters, stuck IR players."""
    target, blocked = plan(players, slot_counts, games)
    current = {p["id"]: p["slot"] for p in players}
    moves = [(p, p["slot"], target[p["id"]]) for p in players if target[p["id"]] != p["slot"]]
    gain = starters_total(players, target, games) - starters_total(players, current, games)
    broken = [p for p in players if p["slot"] not in (BENCH, IR) and not p["locked"]
              and expected_points(p, games) == 0]
    return target, moves, gain, broken, blocked


def would_act(moves, gain, broken, later_flex=False):
    """Fix a lineup that can't play, or an IR slot, regardless of gain. Otherwise only a real
    upgrade, or later kickoffs into FLEX, which has to happen before the early games lock."""
    if not moves:
        return False
    if broken or any(IR in (old, new) for _, old, new in moves):
        return True
    return gain >= MIN_GAIN or later_flex


def describe(players, target, moves, gain, broken, blocked, games, week):
    lines = [f"Week {week} lineup", ""]
    current = {p["id"]: p["slot"] for p in players}
    lines.append(f"Current starters project {starters_total(players, current, games):.1f}")
    lines.append(f"Best legal lineup projects {starters_total(players, target, games):.1f}  ({gain:+.1f})")
    lines.append("")
    if not moves:
        lines.append("Already the best legal lineup. No moves.")
    else:
        for player, old, new in sorted(moves, key=lambda m: SLOT_ORDER.index(m[2])):
            note = "" if player["injury_status"] in HEALTHY else f"  [{player['injury_status']}]"
            lines.append(f"  {player['name'][:24]:<24} {SLOT_NAMES[old]:>5} -> {SLOT_NAMES[new]:<5} "
                         f"{expected_points(player, games):>5.1f}{note}")
        later = later_in_flex(players, target, games)
        if later:
            lines += ["", "Later kickoffs go to FLEX, so a late scratch there can be covered from any position."]
        if not would_act(moves, gain, broken, later):
            lines.append("")
            lines.append(f"Holding: {gain:+.1f} is under the {MIN_GAIN} point bar.")
    locked = [p["name"] for p in players if p["locked"]]
    if locked:
        lines += ["", "Locked, can't be moved: " + ", ".join(locked)]
    for player in blocked:
        lines += ["", f"NEEDS YOU: {player['name']} is off IR ({player['injury_status']}) and there is no roster "
                      "spot for him. ESPN can block add/drops until you drop someone."]
    for player in players:
        if target[player["id"]] not in (BENCH, IR) and player["injury_status"] not in HEALTHY:
            lines.append(f"Watch: {player['name']} would start and is listed {player['injury_status']}")
    return "\n".join(lines)


def without_ir(moves):
    """The same plan with the IR slot left alone: a stash stays on the bench instead."""
    fallback = []
    for player, old, new in moves:
        if old == IR:
            continue  # leave him on IR; the slot he would have filled just stays empty
        if new == IR:
            new = BENCH
        if old != new:
            fallback.append((player, old, new))
    return fallback


def apply_moves(espn, team_id, week, moves):
    """Send the moves as one transaction. Returns (applied, note); applied is None if unsure.

    A rejected transaction applies nothing, so retrying without the IR move is safe.
    """
    try:
        espn.set_slots(team_id, week, [(p["id"], old, new) for p, old, new in moves])
        return moves, None
    except ESPNWriteRejected as rejection:
        fallback = without_ir(moves)
        if not any(IR in (old, new) for _, old, new in moves) or not fallback:
            raise
        espn.set_slots(team_id, week, [(p["id"], old, new) for p, old, new in fallback])
        return fallback, f"ESPN refused the IR move ({rejection.kind}); applied the rest, stash left on the bench"
    except requests.exceptions.RequestException as problem:
        # The request may or may not have landed. Never retry blind; the read-back decides.
        return None, f"connection problem while writing ({type(problem).__name__}); checking the roster"


def verify(espn, team_id, week, target):
    """Read the roster back and return the players whose slot isn't what we asked for."""
    entries = espn.roster_entries(team_id, week)
    actual = {entry["playerId"]: entry["lineupSlotId"] for entry in entries}
    return [pid for pid, slot in target.items() if actual.get(pid) != slot]


def season_kickoffs(espn, path=SCHEDULE):
    """This season's NFL kickoffs, from a copy refreshed daily: the check-in runs every 15 minutes.

    A stale copy beats none if ESPN can't be reached; with no copy at all, the error surfaces.
    """
    if not path.exists() or time.time() - path.stat().st_mtime > SCHEDULE_MAX_AGE:
        try:
            times = espn.kickoffs()
            path.parent.mkdir(exist_ok=True)
            path.write_text(json.dumps([t.isoformat() for t in times]))
        except (ESPNError, requests.exceptions.RequestException):
            if not path.exists():
                raise
    return [datetime.fromisoformat(t) for t in json.loads(path.read_text())]


def game_time_soon(now, kickoffs):
    """Whether a game kicks off within KICKOFF_WINDOW, so its inactives are out or about to be."""
    return any(now < kickoff <= now + KICKOFF_WINDOW for kickoff in kickoffs)


def main(live, force=False, near_kickoff=False):
    espn = ESPN.from_env()
    if near_kickoff and not game_time_soon(datetime.now(), season_kickoffs(espn)):
        return   # nothing kicks off soon: print nothing, so run.sh leaves the log alone
    league = espn.league("mSettings", "mTeam")
    week = league["scoringPeriodId"]
    team = espn.my_team(league["teams"])
    slot_counts = {int(k): v for k, v in league["settings"]["rosterSettings"]["lineupSlotCounts"].items()}
    players = [parse_roster_entry(e, week, espn.season) for e in espn.roster_entries(team["id"], week)]
    games = espn.pro_games(week)

    target, moves, gain, broken, blocked = decide(players, slot_counts, games)
    later = later_in_flex(players, target, games)
    report = describe(players, target, moves, gain, broken, blocked, games, week)
    print(report)

    act = bool(moves) and (would_act(moves, gain, broken, later) or force)
    if act and not would_act(moves, gain, broken, later):
        print("\nApplying anyway (--force).")

    if not live:
        if act:
            print("\nPractice mode. Add --live to apply these moves.")
        return
    if not act:
        if blocked and not near_kickoff:   # the morning run sends this; every check-in would be a flood
            mailer.send(f"Week {week}: roster needs you", report)
        return

    failure = None
    try:
        applied, note = apply_moves(espn, team["id"], week, moves)
    except ESPNWriteRejected as rejection:
        applied, note, failure = None, None, str(rejection)

    lines = [report, ""]
    if failure:
        needs_you = True
        lines += [failure, "Nothing was applied; your lineup is unchanged."]
    else:
        expected = {p["id"]: target[p["id"]] for p in players}
        if applied is not None:  # a fallback may have applied less than the full plan
            expected = {p["id"]: p["slot"] for p in players}
            expected.update({p["id"]: new for p, _, new in applied})
        wrong = verify(espn, team["id"], week, expected)
        by_id = {p["id"]: p for p in players}
        needs_you = bool(wrong)
        if note:
            lines.append(note)
        if wrong:
            lines.append("Did NOT end up as asked: " + ", ".join(by_id[pid]["name"] for pid in wrong if pid in by_id))
        else:
            lines.append(f"Applied and confirmed on ESPN: {len(applied if applied is not None else moves)} move(s).")
    body = "\n".join(lines)
    print("\n" + "\n".join(lines[1:]))
    subject = f"Week {week}: {'lineup needs you' if needs_you else 'lineup updated'}"
    mailer.send(subject, body)


if __name__ == "__main__":
    try:
        main(live="--live" in sys.argv, force="--force" in sys.argv, near_kickoff="--near-kickoff" in sys.argv)
    except ESPNError as error:
        message = f"Error: {error}"
        print(message, file=sys.stderr)
        if "--live" in sys.argv:
            mailer.send("lineup run failed", message)
        sys.exit(1)
