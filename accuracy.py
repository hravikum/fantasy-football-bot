"""How good ESPN's weekly projections have been in this league.

Compares every rostered player's weekly projection against what he actually scored, by
position. PER PROJ PT is what sets MIN_GAIN in lineup.py: what one projected point has been
worth in real points, which prices every upgrade the bar holds back.

Usage: python3 accuracy.py [season]     a past season, e.g. 2025, writes its own log
"""
import csv
import sys
from collections import defaultdict
from pathlib import Path

from espn import ESPN, ESPNError, parse_roster_entry
from lineup import MIN_GAIN

LOG = Path(__file__).with_name("logs") / "accuracy.csv"
FIELDS = ["week", "player_id", "player", "position", "projected", "actual", "error"]
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
                rows.append({"week": played, "player_id": player["id"], "player": player["name"],
                             "position": player["position"],
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


def slope(pairs):
    """Real points per projected point: what a one-point projected edge has been worth.

    This, not the size of the misses, prices a held upgrade. A big typical miss means any one
    call can go wrong; it doesn't make a small edge worthless on average.
    """
    if len(pairs) < 3:
        return None
    mean_p = sum(p for p, _ in pairs) / len(pairs)
    mean_a = sum(a for _, a in pairs) / len(pairs)
    spread = sum((p - mean_p) ** 2 for p, _ in pairs)
    return sum((p - mean_p) * (a - mean_a) for p, a in pairs) / spread if spread else None


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
            "slope": slope([(row["projected"], row["actual"]) for row in entries]),
        }
    return summary


def report(week, rows, summary):
    lines = [f"ESPN projection accuracy, weeks 1-{week - 1}",
             f"{len(rows)} player-weeks where the projection was {STARTABLE:.0f} or better", ""]
    if not rows:
        return "\n".join(lines + ["Nothing has finished yet."])
    lines.append(f"{'POS':<6} {'N':>5} {'BIAS':>7} {'AVG MISS':>9} {'TRACKS':>7} {'PER PROJ PT':>12}")
    for position in sorted(summary, key=lambda p: -summary[p]["n"]):
        stats = summary[position]
        tracks = "n/a" if stats["tracks"] is None else f"{stats['tracks']:.2f}"
        worth = "n/a" if stats["slope"] is None else f"{stats['slope']:.2f}"
        lines.append(f"{position:<6} {stats['n']:>5} {stats['bias']:>+7.1f} {stats['average_miss']:>9.1f} "
                     f"{tracks:>7} {worth:>12}")
    lines += ["",
              "BIAS is how much players outscore the projection on average; AVG MISS is the typical",
              "gap either way; TRACKS is how closely the projection follows the result (1.0 is perfect).",
              "PER PROJ PT is what one projected point has been worth in real points: at 0.9, a",
              "2-point projected edge is worth about 1.8 points on average, however big the misses.",
              "", f"MIN_GAIN in lineup.py holds upgrades under {MIN_GAIN} projected points. Each one it",
              "holds costs about PER PROJ PT times its edge."]
    return "\n".join(lines)


def write_log(rows, path=None):
    path = path or LOG
    path.parent.mkdir(exist_ok=True)
    with open(path, "w", newline="") as handle:          # rewritten each run: ESPN keeps the history
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(sorted(rows, key=lambda r: (r["week"], r["position"], r["player"])))
    return len(rows)


def main(season=None):
    espn = ESPN.from_env()
    path = LOG
    if season:   # a past season gets its own log, so this season's stays as it is
        espn.season = season
        path = LOG.with_name(f"accuracy_{season}.csv")
    week, rows = collect(espn)
    print(report(week, rows, by_position(rows)))
    print(f"\n{write_log(rows, path)} player-weeks written to {path}")


if __name__ == "__main__":
    try:
        main(int(sys.argv[1]) if len(sys.argv) > 1 else None)
    except ESPNError as error:
        sys.exit(f"Error: {error}")
