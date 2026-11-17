"""Thin client for ESPN's private fantasy football API.

Reads use the same host and views as the espn-api package. Lineup writes
(step 3) will use the lm-api-writes host.
"""
import json
import os
from datetime import date, datetime
from pathlib import Path

import requests
from dotenv import load_dotenv

READ_BASE = "https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl"
WRITE_BASE = "https://lm-api-writes.fantasy.espn.com/apis/v3/games/ffl"

# lineupSlotId -> label, in display order (starters, then bench, then IR)
SLOT_NAMES = {0: "QB", 2: "RB", 4: "WR", 6: "TE", 23: "FLEX", 16: "D/ST", 17: "K", 20: "BE", 21: "IR"}
SLOT_ORDER = list(SLOT_NAMES)
BENCH, IR = 20, 21
# Single-position slots, used to find a player's primary position
POSITION_SLOTS = {0: "QB", 2: "RB", 4: "WR", 6: "TE", 16: "D/ST", 17: "K"}

# ESPN stat ids for the opportunity numbers that show a player's role
USAGE_STATS = {"pass_attempts": 0, "carries": 23, "receptions": 53, "targets": 58}

PRO_TEAMS = {
    0: "FA", 1: "ATL", 2: "BUF", 3: "CHI", 4: "CIN", 5: "CLE", 6: "DAL", 7: "DEN", 8: "DET",
    9: "GB", 10: "TEN", 11: "IND", 12: "KC", 13: "LV", 14: "LAR", 15: "MIA", 16: "MIN",
    17: "NE", 18: "NO", 19: "NYG", 20: "NYJ", 21: "PHI", 22: "ARI", 23: "PIT", 24: "LAC",
    25: "SF", 26: "SEA", 27: "TB", 28: "WSH", 29: "CAR", 30: "JAX", 33: "BAL", 34: "HOU",
}


class ESPNError(Exception):
    pass


class ESPNWriteRejected(ESPNError):
    """ESPN refused a transaction. Nothing was applied: a transaction is all or nothing."""

    def __init__(self, status, kind, message):
        super().__init__(f"ESPN refused the change ({status} {kind}): {message}")
        self.status = status
        self.kind = kind


class ESPN:
    def __init__(self, league_id, espn_s2, swid, season=None):
        self.league_id = league_id
        self.season = season or date.today().year
        self.swid = swid
        self.my_team_id = None
        self._schedule = None
        self.session = requests.Session()
        self.session.cookies.update({"espn_s2": espn_s2, "SWID": swid})

    @classmethod
    def from_env(cls):
        load_dotenv(Path(__file__).with_name(".env"))
        missing = [k for k in ("LEAGUE_ID", "ESPN_S2", "SWID") if not os.getenv(k)]
        if missing:
            raise ESPNError(f"Missing in .env: {', '.join(missing)}")
        return cls(int(os.environ["LEAGUE_ID"]), os.environ["ESPN_S2"], os.environ["SWID"])

    def _get(self, url, params, headers=None):
        for attempt in range(2):
            try:
                resp = self.session.get(url, params=params, headers=headers, timeout=30)
                break
            except requests.exceptions.ConnectionError:
                # ESPN closes a connection left idle while the report does other work, and the session
                # only finds out on its next request. A read is safe to send again.
                if attempt:
                    raise
        if resp.status_code == 401:
            raise ESPNError("ESPN rejected the cookies (401). Re-copy ESPN_S2 and SWID from your browser.")
        if resp.status_code == 404:
            raise ESPNError(f"League {self.league_id} not found for {self.season}. Check LEAGUE_ID.")
        resp.raise_for_status()
        data = resp.json()
        return data[0] if isinstance(data, list) else data

    def league(self, *views, week=None, headers=None):
        params = {"view": list(views)}
        if week is not None:
            params["scoringPeriodId"] = week
        return self._get(f"{READ_BASE}/seasons/{self.season}/segments/0/leagues/{self.league_id}", params, headers)

    def free_agents(self, week, limit=75):
        """Unowned players, most widely rostered first. The filter is a header, not a parameter."""
        rules = {"players": {"filterStatus": {"value": ["FREEAGENT", "WAIVERS"]},
                             "limit": limit,
                             "sortPercOwned": {"sortPriority": 1, "sortAsc": False}}}
        data = self.league("kona_player_info", week=week, headers={"x-fantasy-filter": json.dumps(rules)})
        players = []
        for entry in data.get("players", []):
            player = parse_roster_entry(entry, week, self.season)
            player["locked"] = False  # ESPN's flag isn't the question; waivers.lock_started sets it from kickoffs
            # Cleared waivers: anyone can add him on the spot for nothing. Otherwise he goes to the
            # highest bid at the next run. Unknown counts as on waivers, so a bid is still shown.
            player["free_now"] = entry.get("status") == "FREEAGENT"
            players.append(player)
        return players

    def player_cards(self, player_ids, final_week):
        """{player_id: player payload} with per-week stat lines, fetched in batches ESPN accepts."""
        found = {}
        ids = list(player_ids)
        for start in range(0, len(ids), 50):
            rules = {"players": {"filterIds": {"value": ids[start:start + 50]},
                                 "filterStatsForTopScoringPeriodIds": {
                                     "value": final_week,
                                     "additionalValue": [f"00{self.season}", f"10{self.season}"]}}}
            data = self.league("kona_playercard", headers={"x-fantasy-filter": json.dumps(rules)})
            for entry in data.get("players", []):
                player = entry.get("player") or entry.get("playerPoolEntry", {}).get("player", {})
                if player.get("id"):
                    found[player["id"]] = player
        return found

    def usage(self, player_ids, final_week):
        """{player_id: [{week, carries, targets, ...}]} of actual per-game usage this season."""
        found = {}
        for player_id, player in self.player_cards(player_ids, final_week).items():
            weeks = {}
            for line in player.get("stats", []):
                if (line.get("statSourceId") == 0 and line.get("seasonId") == self.season
                        and line.get("scoringPeriodId") and line.get("statSplitTypeId") != 2):
                    raw = line.get("stats", {})
                    weeks[line["scoringPeriodId"]] = {name: raw.get(str(stat), 0) for name, stat in USAGE_STATS.items()}
            found[player_id] = [dict(week=week, **weeks[week]) for week in sorted(weeks)]
        return found

    def waiver_claims(self, weeks):
        """Executed waiver claims: what each winning bid cost and who it bought."""
        rules = {"transactions": {"filterType": {"value": ["WAIVER"]}}}
        claims, seen = [], set()
        for week in weeks:
            try:
                data = self.league("mTransactions2", week=week, headers={"x-fantasy-filter": json.dumps(rules)})
            except ESPNError:
                continue
            for transaction in data.get("transactions", []):
                key = transaction.get("id") or id(transaction)
                if transaction.get("status") != "EXECUTED" or key in seen:
                    continue
                seen.add(key)
                stamp = transaction.get("processDate")
                claims.append({
                    "week": week,
                    "team_id": transaction.get("teamId"),
                    "bid": transaction.get("bidAmount") or 0,
                    "added": [item.get("playerId") for item in transaction.get("items", [])
                              if item.get("type") == "ADD"],
                    "processed": datetime.fromtimestamp(stamp / 1000) if stamp else None,
                })
        return claims

    def waiver_runs(self, weeks):
        """When claims actually processed, as local datetimes. ESPN's settings only give an hour."""
        return sorted({claim["processed"] for claim in self.waiver_claims(weeks) if claim["processed"]})

    def _pro_schedule(self):
        if self._schedule is None:
            self._schedule = self._get(f"{READ_BASE}/seasons/{self.season}", {"view": "proTeamSchedules_wl"})
        return self._schedule

    def bye_weeks(self):
        """{pro_team_id: set of weeks with no game}."""
        data = self._pro_schedule()
        played = {t["id"]: {int(w) for w, games in t.get("proGamesByScoringPeriod", {}).items() if games}
                  for t in data["settings"]["proTeams"] if t.get("id")}
        season_weeks = set().union(*played.values()) if played else set()
        return {team: season_weeks - weeks for team, weeks in played.items()}

    def pro_games(self, week):
        """{pro_team_id: (opponent_id, local kickoff time)} for NFL teams playing this week."""
        data = self._pro_schedule()
        games = {}
        for team in data["settings"]["proTeams"]:
            for game in team.get("proGamesByScoringPeriod", {}).get(str(week), []):
                home, away = game["homeProTeamId"], game["awayProTeamId"]
                opponent = away if team["id"] == home else home
                games[team["id"]] = (opponent, datetime.fromtimestamp(game["date"] / 1000))
        return games

    def kickoffs(self):
        """Every NFL kickoff this season as local times, earliest first."""
        data = self._pro_schedule()
        return sorted({datetime.fromtimestamp(game["date"] / 1000)
                       for team in data["settings"]["proTeams"]
                       for games in team.get("proGamesByScoringPeriod", {}).values()
                       for game in games})

    def my_team(self, teams):
        """The one team owned by the SWID cookie. The bot never acts on any other team."""
        me = _norm(self.swid)
        mine = [t for t in teams if me in {_norm(o) for o in t.get("owners", [])}]
        if len(mine) != 1:
            raise ESPNError(f"Expected exactly one team owned by your SWID, found {len(mine)}. Check SWID in .env.")
        self.my_team_id = mine[0]["id"]
        return mine[0]

    def roster_entries(self, team_id, week):
        data = self.league("mRoster", week=week)
        return next(t for t in data["teams"] if t["id"] == team_id)["roster"]["entries"]

    def set_slots(self, team_id, week, moves):
        """Send one atomic lineup transaction. moves: [(player_id, from_slot, to_slot)].

        ESPN validates the whole transaction and applies all of it or none of it, and it
        only accepts the league's current scoring period.
        """
        if self.my_team_id is None:
            raise ESPNError("Ownership not confirmed yet; call my_team() before writing.")
        if team_id != self.my_team_id:
            raise ESPNError(f"Refusing to write to team {team_id}; these cookies own team {self.my_team_id}.")
        if not moves:
            raise ESPNError("No moves to send.")
        # Vacating moves first, so the transaction reads in the order a person would make it.
        ordered = sorted(moves, key=lambda m: m[2] not in (BENCH, IR))
        body = {
            "isLeagueManager": False,
            "teamId": team_id,
            "type": "ROSTER",
            "scoringPeriodId": week,
            "executionType": "EXECUTE",
            "items": [{"playerId": pid, "type": "LINEUP", "fromLineupSlotId": frm, "toLineupSlotId": to}
                      for pid, frm, to in ordered],
        }
        url = f"{WRITE_BASE}/seasons/{self.season}/segments/0/leagues/{self.league_id}/transactions/"
        resp = self.session.post(url, json=body, timeout=30)
        if resp.status_code != 200:
            raise ESPNWriteRejected(resp.status_code, *_rejection(resp))
        result = resp.json()
        if result.get("status") != "EXECUTED":
            raise ESPNWriteRejected(200, result.get("status", "UNKNOWN"), "transaction was not executed")
        return result


def _rejection(resp):
    """ESPN puts a typed reason in details[] and prose in messages[]."""
    try:
        data = resp.json()
    except ValueError:
        return "UNPARSEABLE", resp.text[:200]
    detail = (data.get("details") or [{}])[0]
    message = (data.get("messages") or ["no message"])[0]
    return detail.get("type", "UNTYPED"), message


def _norm(swid):
    return swid.strip().strip("{}").lower()


def parse_roster_entry(entry, week, season):
    """Handles both shapes ESPN uses: roster entries and free-agent search results."""
    pool = entry.get("playerPoolEntry", entry)
    player = pool["player"]
    def points_for(source):
        return next(
            (s.get("appliedTotal", 0.0) for s in player.get("stats", [])
             if s.get("seasonId") == season and s.get("scoringPeriodId") == week
             and s.get("statSourceId") == source and s.get("statSplitTypeId") != 2),
            None,
        )

    projection = points_for(1)
    season_points = next(
        (s.get("appliedTotal", 0.0) for s in player.get("stats", [])
         if s.get("seasonId") == season and s.get("scoringPeriodId") == 0 and s.get("statSourceId") == 1),
        None,
    )
    slots = player.get("eligibleSlots", [])
    return {
        "id": player["id"],
        "name": player.get("fullName", str(player["id"])),
        "position": next((POSITION_SLOTS[s] for s in slots if s in POSITION_SLOTS), "?"),
        "pro_team": player.get("proTeamId", 0),
        "slot": entry.get("lineupSlotId", BENCH),
        "eligible_slots": slots,
        "injury_status": player.get("injuryStatus", "ACTIVE"),
        "locked": pool.get("lineupLocked", False),
        "projection": projection,
        "actual": points_for(0),
        "season_projection": season_points,
        "percent_owned": round(player.get("ownership", {}).get("percentOwned", 0.0), 1),
        "percent_change": round(player.get("ownership", {}).get("percentChange", 0.0), 1),
    }
