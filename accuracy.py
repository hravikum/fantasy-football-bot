"""How good ESPN's weekly projections have been in this league.

Compares every rostered player's weekly projection against what he actually scored, by
position. MIN_GAIN in lineup.py is currently a number I guessed; these are the numbers that
should set it, and they say more than the average error does: a position whose projections
barely track results has no small edges worth chasing at all.

Usage: python3 accuracy.py
"""
import csv
import sys
from collections import defaultdict
from pathlib import Path

from espn import ESPN, ESPNError, parse_roster_entry

LOG = Path(__file__).with_name("logs") / "accuracy.csv"
FIELDS = ["week", "player", "position", "projected", "actual", "error"]
STARTABLE = 5.0   # ignore players nobody would have started; their misses aren't decisions


def collect(espn):
    """Every projected-and-played week for players rostered in this league.

    Walks the roster view a week at a time: ESPN's player cards keep old results but not the
    projections that were made beforehand, and it's the pair that matters here.
    """
    week = espn.league("mSettings")["scoringPeriodId"]
    byes = espn.bye_weeks()
    rows = []
    for played in range(1, week):
        data = espn.league("mRoster", week=played)
        for team in data.get("teams", []):
            for entry in team.get("roster", {}).get("entries", []):
                player = parse_roster_entry(entry, played, espn.season)
                if player["projection"] is None or player["actual"] is None:
                    continue
                if player["projection"] < STARTABLE:
                    continue  # nobody starts a three-point projection, so its miss isn't a decision
                if played in byes.get(player["pro_team"], set()):
                    continue  # the bot never starts a player on bye, so that projection means nothing
                rows.append({"week": played, "player": player["name"], "position": player["position"],
                             "projected": round(player["projection"], 1),
                             "actual": round(player["actual"], 1),
                             "error": round(player["actual"] - player["projection"], 1)})
    return week, rows


def correlation(pairs):
    """How much of the result the projection actually tracks, from -1 to 1."""
    if len(pairs) < 3:
        return None
    projected = [p for p, _ in pairs]
    actual = [a for _, a in pairs]
    mean_p, mean_a = sum(projected) / len(pairs), sum(actual) / len(pairs)
    covariance = sum((p - mean_p) * (a - mean_a) for p, a in pairs)
    spread_p = sum((p - mean_p) ** 2 for p in projected) ** 0.5
    spread_a = sum((a - mean_a) ** 2 for a in actual) ** 0.5
    return covariance / (spread_p * spread_a) if spread_p and spread_a else None


def by_position(rows):
    grouped = defaultdict(list)
    for row in rows:
        grouped[row["position"]].append(row)
    summary = {}
    for position, entries in grouped.items():
        errors = [row["error"] for row in entries]
        summary[position] = {
            "n": len(entries),
            "bias": sum(errors) / len(errors),
            "average_miss": sum(abs(error) for error in errors) / len(errors),
            "tracks": correlation([(row["projected"], row["actual"]) for row in entries]),
        }
    return summary


def report(week, rows, summary):
    lines = [f"ESPN projection accuracy, weeks 1-{week - 1}",
             f"{len(rows)} player-weeks where the projection was {STARTABLE:.0f} or better", ""]
    if not rows:
        return "\n".join(lines + ["Nothing has finished yet."])
    lines.append(f"{'POS':<6} {'N':>5} {'BIAS':>7} {'AVG MISS':>9} {'TRACKS':>7}  SMALLEST EDGE WORTH A MOVE")
    for position in sorted(summary, key=lambda p: -summary[p]["n"]):
        stats = summary[position]
        tracks = "n/a" if stats["tracks"] is None else f"{stats['tracks']:.2f}"
        # Half the typical miss: below that, a projected edge is mostly noise.
        threshold = stats["average_miss"] / 2
        advice = f"{threshold:.1f} pts" if (stats["tracks"] or 0) > 0.3 else "projections too weak to trust"
        lines.append(f"{position:<6} {stats['n']:>5} {stats['bias']:>+7.1f} {stats['average_miss']:>9.1f} "
                     f"{tracks:>7}  {advice}")
    lines += ["",
              "BIAS is how much players outscore the projection on average; AVG MISS is the typical",
              "gap either way; TRACKS is how closely the projection follows the result (1.0 is perfect).",
              "", f"MIN_GAIN in lineup.py is 2.0 for every position. Three or four more weeks of this",
              "makes these numbers solid enough to set it per position."]
    return "\n".join(lines)


def write_log(rows, path=None):
    path = path or LOG
    path.parent.mkdir(exist_ok=True)
    with open(path, "w", newline="") as handle:          # rewritten each run: ESPN keeps the history
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(sorted(rows, key=lambda r: (r["week"], r["position"], r["player"])))
    return len(rows)


def main():
    espn = ESPN.from_env()
    week, rows = collect(espn)
    print(report(week, rows, by_position(rows)))
    print(f"\n{write_log(rows)} player-weeks written to {LOG}")


if __name__ == "__main__":
    try:
        main()
    except ESPNError as error:
        sys.exit(f"Error: {error}")
