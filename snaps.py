"""Snap share from nflverse: how much of each game a player is on the field.

ESPN doesn't publish snap counts, and they're the earliest sign of a role change: a receiver
who goes from 40% to 80% of snaps shows it before the fantasy points do.

nflverse keys players by Pro Football Reference id, and its player table maps those to ESPN
ids. Both files are cached in cache/ and refreshed when stale; if a download fails, a stale
copy is used rather than nothing.
"""
import csv
import io
import time
from pathlib import Path

import requests

BASE = "https://github.com/nflverse/nflverse-data/releases/download"
CACHE = Path(__file__).with_name("cache")
SNAPS_MAX_AGE = 6 * 3600          # updated after each game
PLAYERS_MAX_AGE = 7 * 86400       # the id table barely changes, and it's 7 MB
TREND_POINTS = 15                 # percentage points of snap share that count as a real change


def _fetch(url, path, max_age):
    """The file's text, from cache when fresh, from the network otherwise, stale if it must."""
    if path.exists() and time.time() - path.stat().st_mtime < max_age:
        return path.read_text()
    try:
        resp = requests.get(url, timeout=60)
        resp.raise_for_status()
    except requests.exceptions.RequestException:
        if path.exists():
            return path.read_text()   # yesterday's snaps beat no snaps
        raise
    path.parent.mkdir(exist_ok=True)
    path.write_text(resp.text)
    return resp.text


def load(season):
    """({espn_id: {week: snap %}}, latest week with data) for this season."""
    players = _fetch(f"{BASE}/players/players.csv", CACHE / "players.csv", PLAYERS_MAX_AGE)
    snaps = _fetch(f"{BASE}/snap_counts/snap_counts_{season}.csv",
                   CACHE / f"snap_counts_{season}.csv", SNAPS_MAX_AGE)
    return join(players, snaps)


def _espn_id(value):
    """ESPN ids arrive as '4258173', or '4258173.0' depending on what wrote the file."""
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def join(players_text, snaps_text):
    to_espn = {}
    for row in csv.DictReader(io.StringIO(players_text)):
        espn_id = _espn_id(row.get("espn_id"))
        if espn_id and row.get("pfr_id"):
            to_espn[row["pfr_id"]] = espn_id

    shares, latest = {}, 0
    for row in csv.DictReader(io.StringIO(snaps_text)):
        if row.get("game_type", "REG") != "REG":
            continue
        week = int(row["week"])
        latest = max(latest, week)
        espn_id = to_espn.get(row.get("pfr_player_id"))
        if espn_id is None:
            continue
        percent = round(float(row["offense_pct"] or 0) * 100)
        weeks = shares.setdefault(espn_id, {})
        weeks[week] = max(percent, weeks.get(week, 0))
    return shares, latest


def summary(weeks, through_week):
    """Snap share by week, with None where he didn't play, and the trend over games he did.

    A missed game isn't a shrinking role, so it's shown but kept out of the trend.
    """
    if not weeks or not through_week:
        return None
    by_week = [weeks.get(week) for week in range(1, through_week + 1)]
    played = [pct for pct in by_week if pct is not None]
    if not played:
        return None
    if len(played) < 2:
        trend = "one game so far"
    else:
        change = played[-1] - sum(played[:-1]) / len(played[:-1])
        trend = "rising" if change >= TREND_POINTS else "falling" if change <= -TREND_POINTS else "steady"
    return {"by_week": by_week, "latest": played[-1], "trend": trend,
            "missed_last_game": by_week[-1] is None}


def label(snaps):
    """The last two weeks, compact enough for a table cell: 40→78, 72→out, 65."""
    if not snaps:
        return "-"
    recent = snaps["by_week"][-2:]
    shown = ["out" if pct is None else str(pct) for pct in recent]
    return "→".join(shown) if len(shown) == 2 else shown[0]
