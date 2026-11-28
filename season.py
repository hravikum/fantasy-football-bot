"""Whether the fantasy season is over, so hosted_run.sh can skip every job once it is.

ESPN's league status names the last scoring period (finalScoringPeriod). The season is over once
the current period is past it, or once the last game of that period is GRACE gone (ESPN may leave
the current period on the last one). The first run to find it over says so in Slack, once a
season, with the command that pauses cron-job.org's timers. Until they're paused, every run starts,
finds the season over, and stops: a GitHub minute each, and nothing sent.

Exit codes: 0 in season, or when it can't tell (the job then runs and reports its own trouble);
3 when the season is over.

Usage: python3 season.py
"""
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

import requests

import mailer
from espn import ESPN, ESPNError

OVER = 3
GRACE = timedelta(hours=12)       # past Monday night's game
NOTICE = Path(__file__).with_name("cache") / "season_over.json"   # the season the notice went out for


def last_kickoff(schedule, period):
    """The last NFL kickoff in that scoring period, from ESPN's pro schedule, or None."""
    times = [game["date"] for team in schedule["settings"]["proTeams"]
             for game in team.get("proGamesByScoringPeriod", {}).get(str(period), [])]
    return datetime.fromtimestamp(max(times) / 1000) if times else None


def is_over(league, espn, now):
    status = league.get("status") or {}
    final, current = status.get("finalScoringPeriod") or 0, league.get("scoringPeriodId") or 0
    if not final or current < final:
        return False
    if current > final:
        return True
    last = last_kickoff(espn._pro_schedule(), final)
    return last is not None and now > last + GRACE


def notice(season):
    return (f"The {season} season is over: the last week's games are done, so the bot has nothing left to do. "
            "Every scheduled run now stops as soon as it starts. Pause the timers from the Mac until next season:\n\n"
            "    python3 timers.py --pause\n\n"
            "and turn them back on before next season's draft:\n\n"
            "    python3 timers.py --resume")


def main(now=None, path=NOTICE):
    now = now or datetime.now()
    try:
        espn = ESPN.from_env()
        over = is_over(espn.league("mSettings"), espn, now)
    except (ESPNError, requests.exceptions.RequestException, KeyError) as problem:
        print(f"(couldn't tell whether the season is over: {type(problem).__name__}: {problem}); the job runs")
        return 0
    if not over:
        return 0
    try:
        told = json.loads(path.read_text()).get("season")
    except (OSError, ValueError):
        told = None
    if told != espn.season:
        print(f"The {espn.season} season is over. Every run from now on stops here.")
        if mailer.send(f"The {espn.season} season is over: pause the timers", notice(espn.season)):
            path.parent.mkdir(exist_ok=True)
            path.write_text(json.dumps({"season": espn.season}))
    return OVER


if __name__ == "__main__":
    sys.exit(main())
