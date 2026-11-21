"""Does rates.py's rate beat values.py's level at saying what a back or receiver scores next?

Two checks, both scored on what a player averaged over his next four games:

1. Replay, 2019 to now, nflverse data only (no ESPN login needed). The prior stands in for a
   preseason projection: last season's points a game, regressed the way 2019-2023 say to. The
   level's weight and the rate's weights are fit on 2019-2024 and scored on 2025 and 2026.
2. The free agents the waiver report sent to Jev this season (logs/jev_scores.csv), with ESPN's
   preseason projection as the prior, the way both run live. Small, but it is the real wire.
3. Reacting faster: whether the work level should move more per game, or lean less on ESPN's
   projection, so that a jump in work shows sooner. Scored on both of the above, and on the
   wire's players in their first two games.

The first download is about 45 MB, cached in cache/.

Usage: python3 rate_test.py
"""
import csv
import statistics
from collections import defaultdict
from datetime import datetime
from itertools import product
from pathlib import Path

import rates
import snaps
import values

FIRST, FIT_THROUGH = 2019, 2024        # replay seasons; the weights are fit through FIT_THROUGH
SEASON = 2026
AHEAD = 4                              # games scored after the estimate
RELEVANT = 5.0                         # points a game, prior or so far, below which nobody is counted
WORK_CHECKS, BEYOND_CHECKS = (1, 2, 4, 8), (8, 16, 32, 64)
DRIFT_CHECKS = (0.02, 0.05, 0.1, 0.2, 0.4)
LEVEL_CHECKS = (1, 2, 4, 8, 16)
JEV_LOG = Path(__file__).with_name("logs") / "jev_scores.csv"
HIDDEN = 2.0                           # shadow.HIDDEN; not imported, so this runs without ESPN's modules


def season_rows(season):
    """{gsis_id: (position, name, [(week, expected, scored)])} for backs and receivers."""
    path = snaps.CACHE / f"ep_weekly_{season}.csv"
    text = snaps._fetch(rates.URL.format(season=season), path, rates.MAX_AGE if season == SEASON else 10 ** 9)
    found = {}
    for row in csv.DictReader(text.splitlines()):
        if row["position"] not in rates.RATED or int(row["week"]) > 18:
            continue
        expected, scored = rates.game_points(row)
        entry = found.setdefault(row["player_id"], (row["position"], row["full_name"], []))
        entry[2].append((int(row["week"]), expected, scored))
    for _, _, games in found.values():
        games.sort()
    return found


def regression(pairs):
    """(intercept, slope) of a least-squares line through [(x, y)]."""
    xs, ys = [x for x, _ in pairs], [y for _, y in pairs]
    mx, my = statistics.fmean(xs), statistics.fmean(ys)
    slope = sum((x - mx) * (y - my) for x, y in pairs) / sum((x - mx) ** 2 for x in xs)
    return my - slope * mx, slope


def priors(by_season):
    """{(season, gsis_id): prior points a game} from the season before, fit on 2019-2023."""
    ppg = {(s, pid): (len(g), statistics.fmean(p for _, _, p in g))
           for s, rows in by_season.items() for pid, (_, _, g) in rows.items()}
    fits = {}
    for position in rates.RATED:
        pairs, newcomers = [], []
        for (s, pid), (games, average) in ppg.items():
            if s > FIT_THROUGH - 1 or games < 4 or by_season[s][pid][0] != position:
                continue
            before = ppg.get((s - 1, pid))
            if before and before[0] >= 4:
                pairs.append((before[1], average))
            else:
                newcomers.append(average)
        fits[position] = regression(pairs) + (statistics.fmean(newcomers),)
    found = {}
    for (s, pid) in ppg:
        intercept, slope, newcomer = fits[by_season[s][pid][0]]
        before = ppg.get((s - 1, pid))
        found[(s, pid)] = intercept + slope * before[1] if before and before[0] >= 4 else newcomer
    return found


def replay_cases():
    """One case per player per game: what was known after it, and what he averaged over the next four."""
    by_season = {s: season_rows(s) for s in range(FIRST - 1, SEASON + 1)}
    prior = priors(by_season)
    cases = []
    for season in range(FIRST, SEASON + 1):
        for pid, (position, name, games) in by_season[season].items():
            for index, (week, _, _) in enumerate(games):
                ahead = [p for w, _, p in games if week < w <= week + AHEAD]
                seen = games[:index + 1]
                start = prior[(season, pid)]
                if not ahead or max(start, statistics.fmean(p for _, _, p in seen)) < RELEVANT:
                    continue
                cases.append({"season": season, "position": position, "name": name, "prior": start,
                              "games": [(w, p) for w, _, p in seen], "weeks": {w: (x, p) for w, x, p in seen}, "actual": statistics.fmean(ahead)})
    return cases


def level_of(case, games_weight):
    return values.level(case["prior"], [p for _, p in case["games"]], games_weight)


def rate_of(case, work=None, beyond=None, drift=None):
    position = case["position"]
    saved = rates.OPPORTUNITY_GAMES[position], rates.EFFICIENCY_GAMES[position], rates.WORK_DRIFT
    try:
        rates.OPPORTUNITY_GAMES[position] = work or saved[0]
        rates.EFFICIENCY_GAMES[position] = beyond or saved[1]
        rates.WORK_DRIFT = saved[2] if drift is None else drift
        return rates.rate(case["prior"], case["games"], case["weeks"], set(case["weeks"]), position)[0]
    finally:
        rates.OPPORTUNITY_GAMES[position], rates.EFFICIENCY_GAMES[position], rates.WORK_DRIFT = saved


def miss(cases, estimate):
    return statistics.fmean(abs(estimate(c) - c["actual"]) for c in cases)


def replay_report(cases):
    lines = [f"1. Replay: average miss on the next {AHEAD} games' points a game (nflverse, last season as the prior)", ""]
    for position in sorted(rates.RATED):
        fit = [c for c in cases if c["position"] == position and c["season"] <= FIT_THROUGH]
        test = [c for c in cases if c["position"] == position and c["season"] > FIT_THROUGH]
        best = min(LEVEL_CHECKS, key=lambda n: miss(fit, lambda c: level_of(c, n)))
        grid = {(w, b): miss(fit, lambda c: rate_of(c, w, b)) for w, b in product(WORK_CHECKS, BEYOND_CHECKS)}
        lines += [f"{position}: rate weights against {len(fit)} fitted player-games, 2019-{FIT_THROUGH} "
                  f"(* is what rates.py uses)",
                  "  work\\beyond " + " ".join(f"{b:>7}" for b in BEYOND_CHECKS)]
        for w in WORK_CHECKS:
            cells = [f"{grid[(w, b)]:.3f}" + ("*" if (w, b) == (rates.OPPORTUNITY_GAMES[position],
                                                                rates.EFFICIENCY_GAMES[position]) else " ")
                     for b in BEYOND_CHECKS]
            lines.append(f"  {w:>11} " + " ".join(f"{cell:>7}" for cell in cells))
        level_miss, rate_miss = miss(test, lambda c: level_of(c, best)), miss(test, rate_of)
        lines += [f"  Scored on {FIT_THROUGH + 1}-{SEASON}, {len(test)} player-games: prior alone "
                  f"{miss(test, lambda c: c['prior']):.3f}, level ({best} games, its best fit) {level_miss:.3f}, "
                  f"rate {rate_miss:.3f} ({100 * (rate_miss - level_miss) / level_miss:+.1f}%)", ""]
    return lines


def ranks(numbers):
    order = sorted(range(len(numbers)), key=numbers.__getitem__)
    found = [0.0] * len(numbers)
    for rank, index in enumerate(order):
        found[index] = rank
    return found


def rank_correlation(xs, ys):
    """Spearman's: the correlation of the two orders (written out; statistics.correlation needs 3.10)."""
    a, b = ranks(xs), ranks(ys)
    ma, mb = statistics.fmean(a), statistics.fmean(b)
    covariance = sum((x - ma) * (y - mb) for x, y in zip(a, b))
    return covariance / (sum((x - ma) ** 2 for x in a) * sum((y - mb) ** 2 for y in b)) ** 0.5


def wire_cases(path=JEV_LOG):
    """The backs and receivers the waiver report sent to Jev, first time each week, with ESPN's prior,
    and what each averaged over the next AHEAD weeks whose games came after that run."""
    if not path.exists():
        return []
    by_player, _, _ = rates.load(SEASON)
    seen, cases = set(), []
    with open(path) as handle:
        for row in sorted(csv.DictReader(handle), key=lambda r: r["logged_at"]):
            key = (row["week"], row["player_id"])
            weeks = by_player.get(int(row["player_id"]))
            if row["position"] not in rates.RATED or not row["season_projection"] or key in seen or not weeks:
                continue
            seen.add(key)
            week = int(row["week"])
            # A Monday run comes after that week's Sunday games (ESPN is still on the week just
            # played), so they don't count as ahead of it; shadow.first_week_after does the same.
            start = week + 1 if datetime.fromisoformat(row["logged_at"]).weekday() == 0 else week
            before = sorted((w, p) for w, (_, p) in weeks.items() if w < week)
            ahead = [p for w, (_, p) in weeks.items() if start <= w < start + AHEAD]
            if ahead:
                cases.append({"week": week, "name": row["name"], "position": row["position"],
                              "prior": float(row["season_projection"]) / values.GAMES_PER_SEASON,
                              "games": before, "weeks": weeks, "actual": statistics.fmean(ahead)})
    return cases


def wire_report(cases):
    if not cases:
        return ["2. The wire: no logs/jev_scores.csv here, so skipped."]
    level_now = lambda c: level_of(c, values.PRIOR_GAMES[c["position"]])
    by_week = defaultdict(list)
    for case in cases:
        by_week[case["week"]].append(case)
    weeks = [group for group in by_week.values() if len(group) >= 5]
    correlation = lambda estimate: statistics.fmean(
        rank_correlation([estimate(c) for c in group], [c["actual"] for c in group]) for group in weeks)
    lines = [f"2. The wire: {len(cases)} back and receiver weeks the report sent to Jev, "
             f"{len({c['name'] for c in cases})} players, ESPN's preseason projection as the prior",
             f"  Average miss: level as values.py runs it {miss(cases, level_now):.3f}, rate {miss(cases, rate_of):.3f}",
             f"  Rank correlation with what they scored, average of {len(weeks)} weeks: "
             f"level {correlation(level_now):.3f}, rate {correlation(rate_of):.3f}",
             ]
    flagged = [c for c in cases if rate_of(c) - c["prior"] >= HIDDEN]
    if flagged:
        paid = sum(1 for c in flagged if c["actual"] >= c["prior"] + HIDDEN)
        lines.append(f"  Hidden value (rate {HIDDEN:.0f}+ a game over ESPN's projection, shadow.py): {len(flagged)} player-weeks, "
                     f"{len({c['name'] for c in flagged})} players. ESPN said "
                     f"{statistics.fmean(c['prior'] for c in flagged):.1f}, level "
                     f"{statistics.fmean(level_now(c) for c in flagged):.1f}, rate "
                     f"{statistics.fmean(rate_of(c) for c in flagged):.1f}; they scored "
                     f"{statistics.fmean(c['actual'] for c in flagged):.1f} a game, and {paid} scored "
                     f"{HIDDEN:.0f}+ over ESPN's number.")
    lines.append("  Where the two disagreed most:")
    for case in sorted(cases, key=lambda c: -abs(rate_of(c) - level_now(c)))[:8]:
        lines.append(f"    week {case['week']:<3} {case['name'][:22]:<22} {case['position']}  "
                     f"level {level_now(case):4.1f}  rate {rate_of(case):4.1f}  scored {case['actual']:4.1f} a game")
    return lines


def faster_report(cases, wire):
    test = [c for c in cases if c["season"] > FIT_THROUGH]
    early = [c for c in wire if len(c["games"]) <= 2]
    lines = ["", "3. Reacting faster, backs and receivers: average miss on the next four games' points a game",
             f"  {'':<34}{'replay ' + str(FIT_THROUGH + 1) + '-' + str(SEASON):>15}{'wire':>8}{'wire, first 2 games':>21}"]
    rows = [(f"work drift {d}" + (" (rates.py)" if d == rates.WORK_DRIFT else ""), dict(drift=d)) for d in DRIFT_CHECKS]
    rows.append(("ESPN worth 1 game of work, not 2", dict(work=1)))
    for label, settings in rows:
        cells = [f"{miss(group, lambda c: rate_of(c, **settings)):.3f}" if group else "-" for group in (test, wire, early)]
        lines.append(f"  {label:<34}{cells[0]:>15}{cells[1]:>8}{cells[2]:>21}")
    return lines + [f"  ({len(early)} wire player-weeks in a first or second game.)"]


if __name__ == "__main__":
    cases, wire = replay_cases(), wire_cases()
    print("\n".join(replay_report(cases) + wire_report(wire) + faster_report(cases, wire)))
