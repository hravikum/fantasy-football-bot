"""Does a rising snap share predict points above ESPN's projection?

Snap share is in the waiver report but not the ranking, because it has to earn that first.
The fair test is against the projection ESPN made for the same week, which already includes
whatever ESPN knew. Every player-week accuracy.py collects gets the snap trend the report
would have shown going into that week; the question is whether rising players beat their
projection by more than steady ones did.

Usage: python3 snap_test.py [season]     e.g. 2025; defaults to this season
"""
import sys
from collections import defaultdict

import accuracy
import snaps
from espn import ESPN, ESPNError

POSITIONS = ("RB", "WR", "TE")   # quarterbacks play every snap; kickers and defenses have no role to read
TRENDS = ("rising", "steady", "falling")


def label(rows, shares):
    """The RB, WR and TE rows, each with the trend from the weeks before it and none after."""
    labelled = []
    for row in rows:
        if row["position"] not in POSITIONS:
            continue
        snap = snaps.summary(shares.get(row["player_id"]), row["week"] - 1)
        trend = snap["trend"] if snap and snap["trend"] in TRENDS else None
        labelled.append(dict(row, trend=trend))
    return labelled


def beat(errors):
    """(player-weeks, how much they beat the projection on average, standard error of that)."""
    n = len(errors)
    mean = sum(errors) / n
    if n < 2:
        return n, mean, None
    spread = (sum((error - mean) ** 2 for error in errors) / (n - 1)) ** 0.5
    return n, mean, spread / n ** 0.5


def by_trend(rows):
    """{(position, trend): beat(...)}, with "ALL" for the three positions together."""
    groups = defaultdict(list)
    for row in rows:
        if row["trend"]:
            groups[(row["position"], row["trend"])].append(row["error"])
            groups[("ALL", row["trend"])].append(row["error"])
    return {key: beat(errors) for key, errors in groups.items()}


def gap(stats, position):
    """(rising minus steady, its standard error), or None when either side is too thin."""
    rising, steady = stats.get((position, "rising")), stats.get((position, "steady"))
    if not rising or not steady or rising[2] is None or steady[2] is None:
        return None
    return rising[1] - steady[1], (rising[2] ** 2 + steady[2] ** 2) ** 0.5


def report(season, labelled, stats):
    with_trend = sum(1 for row in labelled if row["trend"])
    lines = [f"Snap share against ESPN's projection, {season} (RB, WR, TE)",
             f"{with_trend} of {len(labelled)} player-weeks had a snap trend going into the week", ""]
    if not with_trend:
        return "\n".join(lines + ["Nothing to compare yet."])
    lines += ["How much players beat their projection, by their snap trend going into the week",
              "(± is one standard error, then player-weeks)", "",
              f"{'POS':<5}" + "".join(f"{trend.upper():>20}" for trend in TRENDS) + f"{'RISING - STEADY':>20}"]
    for position in ("ALL",) + POSITIONS:
        cells = []
        for trend in TRENDS:
            found = stats.get((position, trend))
            if not found:
                cells.append("-")
                continue
            n, mean, error = found
            cells.append(f"{mean:+.1f}" + (f" ±{error:.1f}" if error is not None else "") + f" ({n})")
        difference = gap(stats, position)
        cells.append(f"{difference[0]:+.1f} ±{difference[1]:.1f}" if difference else "-")
        lines.append(f"{position:<5}" + "".join(f"{cell:>20}" for cell in cells))
    overall = gap(stats, "ALL")
    if overall:
        difference, error = overall
        verdict = ("more than twice its standard error, so it looks like signal rather than luck"
                   if abs(difference) > 2 * error else "within twice its standard error, so it could be luck")
        lines += ["", f"Rising minus steady, all three positions: {difference:+.1f} points a game "
                      f"(±{error:.1f}). That's {verdict}."]
    return "\n".join(lines)


def main(season=None):
    espn = ESPN.from_env()
    if season:
        espn.season = season
    _, rows = accuracy.collect(espn)
    shares, _ = snaps.load(espn.season)
    labelled = label(rows, shares)
    print(report(espn.season, labelled, by_trend(labelled)))


if __name__ == "__main__":
    try:
        main(int(sys.argv[1]) if len(sys.argv) > 1 else None)
    except ESPNError as error:
        sys.exit(f"Error: {error}")
