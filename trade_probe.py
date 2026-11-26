"""Read-only probe for the trade analyzer. Run it once and paste what it prints into the chat.

The analyzer leans on parts of ESPN the bot hasn't read before: trade proposals, trade settings,
the schedule and standings (for playoff odds) and the draft (for keepers). It also checks whether
ESPN's season projection, which every week after this one is built on, moves during the season.
It changes nothing, and account ids are hidden so the output is safe to share.

Usage: python3 trade_probe.py
"""
import csv
import json
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

from espn import ESPN, ESPNError, parse_roster_entry
from trades import team_name
from waivers import GAMES_PER_SEASON

# Jev's old judgment log (no longer written): its season projections, day after day.
JEV_LOG = Path(__file__).with_name("logs") / "jev_scores.csv"
TRADE_TYPES = ["TRADE_PROPOSAL", "TRADE_ACCEPT", "TRADE_DECLINE", "TRADE_VETO", "TRADE_UPHOLD", "TRADE_ERROR"]


def hidden(value):
    """Account ids out of anything printed raw: the analyzer never needs them."""
    if isinstance(value, dict):
        return {k: "<hidden>" if "member" in k.lower() or k.lower() in ("owners", "swid", "primaryowner")
                else hidden(v) for k, v in value.items()}
    if isinstance(value, list):
        return [hidden(v) for v in value]
    return value


def when(ms):
    return datetime.fromtimestamp(ms / 1000).strftime("%a %b %d %I:%M %p") if ms else "-"


def settings_part(league):
    settings = league["settings"]
    schedule = settings.get("scheduleSettings", {})
    print("tradeSettings:", json.dumps(hidden(settings.get("tradeSettings", {}))))
    print("scheduleSettings:", json.dumps({k: schedule.get(k) for k in (
        "matchupPeriodCount", "matchupPeriodLength", "playoffTeamCount", "playoffMatchupPeriodLength",
        "playoffSeedingRule", "playoffSeedingRuleBy")}))
    periods = schedule.get("matchupPeriods") or {}
    print("matchupPeriods:", ", ".join(f"{k}:{v}" for k, v in sorted(periods.items(), key=lambda kv: int(kv[0]))))
    status = league.get("status", {})
    print("status:", json.dumps({k: status.get(k) for k in (
        "currentMatchupPeriod", "firstScoringPeriod", "latestScoringPeriod", "finalScoringPeriod")}))
    scoring = settings.get("scoringSettings", {})
    print("scoring:", json.dumps({k: scoring.get(k) for k in (
        "scoringEnhancementType", "matchupTieRule", "playoffMatchupTieRule")}))
    draft = settings.get("draftSettings", {})
    print("draftSettings, keeper keys:", json.dumps({k: v for k, v in draft.items() if "keep" in k.lower()}))


def standings_part(league, names):
    print("Compare these with the standings page, to see whether the median wins are included:")
    for team in sorted(league["teams"], key=lambda t: t.get("playoffSeed") or 99):
        overall = team.get("record", {}).get("overall", {})
        print(f"  seed {team.get('playoffSeed')}: {names[team['id']][:24]:<24} {overall.get('wins')}-"
              f"{overall.get('losses')}-{overall.get('ties')}  PF {overall.get('pointsFor')}")


def schedule_part(espn, my_id, names):
    matchups = espn.league("mMatchupScore").get("schedule", [])
    periods = Counter(m.get("matchupPeriodId") for m in matchups)
    print(f"{len(matchups)} matchups; per period: {dict(sorted(periods.items()))}")
    for m in matchups:
        home, away = m.get("home", {}), m.get("away", {})
        if my_id in (home.get("teamId"), away.get("teamId")):
            print(f"  period {m.get('matchupPeriodId')}: {names.get(home.get('teamId'))} {home.get('totalPoints')} "
                  f"v {names.get(away.get('teamId'))} {away.get('totalPoints')}  winner {m.get('winner')}  "
                  f"{m.get('playoffTierType')}")
    if matchups:
        print("one entry, raw (cut at 1500 characters):", json.dumps(hidden(matchups[0]))[:1500])


def trades_part(espn, week, my_id, names):
    rules = {"transactions": {"filterType": {"value": TRADE_TYPES}}}
    found = {}
    for period in range(1, week + 2):
        try:
            data = espn.league("mTransactions2", week=period, headers={"x-fantasy-filter": json.dumps(rules)})
        except ESPNError as problem:
            print(f"  week {period}: {problem}")
            continue
        for transaction in data.get("transactions", []):
            found.setdefault(transaction.get("id"), dict(transaction, asked_week=period))
    try:
        pending = espn.league("mPendingTransactions")
        keys = {k: len(v) if isinstance(v, list) else type(v).__name__
                for k, v in pending.items() if "transaction" in k.lower()}
        print("mPendingTransactions view:", keys or "answered, but with no transaction keys")
    except Exception as problem:  # broad on purpose: this view may simply not exist
        print(f"mPendingTransactions view: {type(problem).__name__}: {str(problem)[:120]}")
    if not found:
        print("No trade transactions found in any week.")
        return
    players = {item.get("playerId") for t in found.values() for item in t.get("items") or []}
    cards = espn.player_cards([pid for pid in players if pid], week)
    name = lambda pid: cards.get(pid, {}).get("fullName", str(pid))
    team = lambda tid: names.get(tid, tid)
    print("count by type and status:", dict(Counter(f"{t.get('type')}/{t.get('status')}" for t in found.values())))
    for t in sorted(found.values(), key=lambda t: t.get("proposedDate") or 0):
        print(f"  {str(t.get('id'))[:8]} {t.get('type')} {t.get('status')} period {t.get('scoringPeriodId')} "
              f"(asked {t['asked_week']}) by {team(t.get('teamId'))}; proposed {when(t.get('proposedDate'))}, "
              f"accepted {when(t.get('acceptedDate'))}, processed {when(t.get('processDate'))}, "
              f"related {str(t.get('relatedTransactionId'))[:8]}")
        items = [f"{item.get('type')} {name(item.get('playerId'))} ({team(item.get('fromTeamId'))} -> "
                 f"{team(item.get('toTeamId'))})" for item in t.get("items") or []]
        print("      " + ("; ".join(items) or "no items listed"))
    proposals = [t for t in found.values() if t.get("type") == "TRADE_PROPOSAL"]
    yours = [t for t in proposals if t.get("teamId") == my_id
             or any(my_id in (i.get("fromTeamId"), i.get("toTeamId")) for i in t.get("items") or [])]
    sample = (yours or proposals or list(found.values()))[-1]
    print("one proposal, raw:", json.dumps(hidden({k: v for k, v in sample.items() if k != "asked_week"}), indent=1))


def draft_part(espn, week, my_id, names):
    draft = espn.league("mDraftDetail").get("draftDetail", {})
    picks = draft.get("picks", [])
    keepers = [p for p in picks if p.get("keeper")]
    print(f"drafted {draft.get('drafted')}, {len(picks)} picks, {len(keepers)} marked keeper")
    wanted = {p.get("playerId") for p in keepers} | {p.get("playerId") for p in picks if p.get("teamId") == my_id}
    cards = espn.player_cards([pid for pid in wanted if pid], week)
    name = lambda pid: cards.get(pid, {}).get("fullName", str(pid))
    for pick in keepers:
        print(f"  keeper: {names.get(pick.get('teamId'))} kept {name(pick.get('playerId'))} in round {pick.get('roundId')}")
    print("  your picks: " + ", ".join(
        f"{p.get('roundId')}.{p.get('roundPickNumber')} {name(p.get('playerId'))}{' (kept)' if p.get('keeper') else ''}"
        for p in picks if p.get("teamId") == my_id))
    if picks:
        print("one pick, raw:", json.dumps(hidden((keepers or picks)[0])))


def projection_part(espn, week):
    if JEV_LOG.exists():
        seen = defaultdict(list)
        with open(JEV_LOG, newline="") as handle:
            for row in csv.DictReader(handle):
                if row.get("season_projection"):
                    seen[row["player_id"]].append((row["logged_at"], row["name"], float(row["season_projection"])))
        repeated = {pid: rows for pid, rows in seen.items() if len({r[0][:10] for r in rows}) > 1}
        spread = lambda rows: max(r[2] for r in rows) - min(r[2] for r in rows)
        changed = {pid: rows for pid, rows in repeated.items() if spread(rows) >= 1}
        print(f"From {JEV_LOG.name}: {len(repeated)} players logged on more than one day, "
              f"{len(changed)} of them with a season projection that moved a point or more")
        for rows in sorted(changed.values(), key=lambda rows: -spread(rows))[:8]:
            first, last = min(rows), max(rows)
            print(f"  {first[1]}: {first[2]:.0f} on {first[0][:10]} -> {last[2]:.0f} on {last[0][:10]}")
    else:
        print(f"No {JEV_LOG.name} yet, so only the snapshot below.")

    data = espn.league("mRoster", week=week)
    rows, below = [], 0
    for team in data["teams"]:
        for entry in team["roster"]["entries"]:
            stats = entry["playerPoolEntry"]["player"].get("stats", [])
            actual = next((s for s in stats if s.get("seasonId") == espn.season and s.get("scoringPeriodId") == 0
                           and s.get("statSourceId") == 0), None)
            parsed = parse_roster_entry(entry, week, espn.season)
            games = (actual or {}).get("stats", {}).get("210") or 0
            if games < 2 or not parsed["season_projection"]:
                continue
            scored = actual.get("appliedTotal", 0)
            below += parsed["season_projection"] < scored
            rows.append((parsed["name"], parsed["position"], int(games), scored / games,
                         parsed["season_projection"] / GAMES_PER_SEASON, parsed["projection"] or 0))
    print(f"Rostered players with 2+ games: {len(rows)}; season projection already below points scored: {below}")
    print(f"  {'PLAYER':<22} {'POS':<4} {'GP':>3} {'ACT/G':>6} {'SEASON/17':>9} {'THIS WK':>8}")
    for name, position, games, per_game, season, this_week in sorted(rows, key=lambda r: -abs(r[3] - r[4]))[:12]:
        print(f"  {name[:22]:<22} {position:<4} {games:>3} {per_game:>6.1f} {season:>9.1f} {this_week:>8.1f}")
    sample = data["teams"][0]["roster"]["entries"][0]["playerPoolEntry"]["player"].get("stats", [])
    print("stat lines per player (source, period, split):",
          sorted({(s.get("statSourceId"), s.get("scoringPeriodId"), s.get("statSplitTypeId")) for s in sample}))


def season_lines_part(espn, week):
    """ESPN sends two season-level projection lines, split types 0 and 2. The bot reads only the
    first one it meets. If split 2 is a rest-of-season projection, it's what later weeks need."""
    data = espn.league("mRoster", week=week)
    rows, two_first = [], 0
    for team in data["teams"]:
        for entry in team["roster"]["entries"]:
            season_lines = [s for s in entry["playerPoolEntry"]["player"].get("stats", [])
                            if s.get("seasonId") == espn.season and s.get("scoringPeriodId") == 0
                            and s.get("statSourceId") == 1]
            splits = {s.get("statSplitTypeId"): s for s in season_lines}
            if 2 in splits:
                two_first += season_lines[0].get("statSplitTypeId") == 2
                rows.append((parse_roster_entry(entry, week, espn.season), splits))
    print(f"{len(rows)} rostered players have a split-2 line; for {two_first} of them it comes first, "
          "so the bot reads it as the season projection")
    print(f"  {'PLAYER':<22} {'THIS WK':>7} {'SPLIT0':>7} {'AVG0':>6} {'SPLIT2':>7} {'AVG2':>6}")
    gap = lambda row: abs((row[0]["projection"] or 0) - (row[1].get(0, {}).get("appliedTotal") or 0) / GAMES_PER_SEASON)
    picked = sorted(rows, key=gap, reverse=True)[:10] + sorted(rows, key=gap)[:3]
    for parsed, splits in picked:
        zero, two = splits.get(0, {}), splits.get(2, {})
        print(f"  {parsed['name'][:22]:<22} {parsed['projection'] or 0:>7.1f} {zero.get('appliedTotal') or 0:>7.1f} "
              f"{zero.get('appliedAverage') or 0:>6.1f} {two.get('appliedTotal') or 0:>7.1f} "
              f"{two.get('appliedAverage') or 0:>6.1f}")
    if picked:
        for split in (0, 2):
            line = picked[0][1].get(split, {})
            print(f"split {split} line for {picked[0][0]['name']}, without its stats:",
                  json.dumps({k: v for k, v in line.items() if k not in ("stats", "appliedStats")}))


def main():
    espn = ESPN.from_env()
    league = espn.league("mSettings", "mTeam")
    week = league["scoringPeriodId"]
    me = espn.my_team(league["teams"])
    names = {team["id"]: team_name(team) for team in league["teams"]}
    print(f"Trade probe, {datetime.now():%a %b %d %I:%M %p}, week {week}. "
          f"Your team: {names[me['id']]} (id {me['id']}). Read-only.")
    parts = [("Settings", lambda: settings_part(league)),
             ("Standings", lambda: standings_part(league, names)),
             ("Schedule", lambda: schedule_part(espn, me["id"], names)),
             ("Trades this season", lambda: trades_part(espn, week, me["id"], names)),
             ("Draft, for keepers", lambda: draft_part(espn, week, me["id"], names)),
             ("Does ESPN's season projection move?", lambda: projection_part(espn, week)),
             ("Season projection lines: split 0 against split 2", lambda: season_lines_part(espn, week))]
    for title, part in parts:
        print(f"\n== {title}")
        try:
            part()
        except Exception as problem:  # broad on purpose: one section failing shouldn't hide the rest
            print(f"  (this section failed: {type(problem).__name__}: {str(problem)[:200]})")


if __name__ == "__main__":
    try:
        main()
    except ESPNError as error:
        sys.exit(f"Error: {error}")
