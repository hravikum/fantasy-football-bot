"""Can the bot run somewhere other than your Mac? Checks every outside service it needs, and changes nothing.

Run by .github/workflows/hosted-check.yml on GitHub's machines, with your .env values supplied
as repository secrets. It reads from ESPN, asks Anthropic to count the tokens in one word (which
is free), logs in to Gmail and asks Slack about the channel without sending anything. It never
writes to ESPN, never sends an email or a Slack message, and never prints a secret.

Exits 1 if anything the bot can't do without is unreachable. Works the same on your Mac:
    python3 hosted_check.py
"""
import importlib
import os
import smtplib
import socket
import sys
import time
from datetime import datetime
from pathlib import Path

import requests
from dotenv import load_dotenv

import mailer
import slack
from espn import ESPN, WRITE_BASE
from snaps import BASE as NFLVERSE
from waivers import MODEL

NEEDED = ("LEAGUE_ID", "ESPN_S2", "SWID", "ANTHROPIC_API_KEY", "GMAIL_ADDRESS", "GMAIL_APP_PASSWORD")
OPTIONAL = ("TYPESAFE_API_KEY",)     # only the Jev shadow ranking uses it


def check(name, test, results, essential=True):
    """Run one check, print what it found, and record whether it passed."""
    started = time.time()
    try:
        detail = test()
        passed = True
    except Exception as problem:  # broad on purpose: every check runs and reports, whatever breaks
        detail, passed = f"{type(problem).__name__}: {problem}", False
    mark = "ok  " if passed else ("FAIL" if essential else "warn")
    print(f"{mark} {name:<34} {time.time() - started:5.1f}s  {detail}")
    results.append((name, passed, essential))


def settings():
    missing = [key for key in NEEDED if not os.getenv(key)]
    if missing:
        raise RuntimeError(f"not set: {', '.join(missing)}")
    absent = [key for key in OPTIONAL if not os.getenv(key)]
    return "all set" + (f" (no {', '.join(absent)}, so Jev would be skipped)" if absent else "")


def espn_reads(espn, found):
    league = espn.league("mSettings", "mTeam")
    team = espn.my_team(league["teams"])
    found["week"] = league["scoringPeriodId"]
    return f"week {found['week']}, your team found (id {team['id']}) among {len(league['teams'])}"


def espn_players(espn, found):
    free_agents = espn.free_agents(found["week"], 5)
    cards = espn.player_cards([p["id"] for p in free_agents], 18)
    return f"{len(free_agents)} free agents and {len(cards)} player cards"


def espn_schedule(espn, found):
    games = espn.pro_games(found["week"])
    return f"{len(games)} NFL teams play in week {found['week']}"


def espn_write_host():
    """Only that the host answers. A lineup change is the real test, and this check makes none."""
    host = WRITE_BASE.split("/")[2]
    socket.create_connection((host, 443), timeout=15).close()
    return f"{host} accepts connections"


def anthropic():
    resp = requests.post("https://api.anthropic.com/v1/messages/count_tokens", timeout=30,
                         headers={"x-api-key": os.environ["ANTHROPIC_API_KEY"], "anthropic-version": "2023-06-01",
                                  "content-type": "application/json"},
                         json={"model": MODEL, "messages": [{"role": "user", "content": "ping"}]})
    if resp.status_code != 200:
        raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:200]}")
    return f"key accepted for {MODEL}"


def gmail():
    address, password = os.environ["GMAIL_ADDRESS"], os.environ["GMAIL_APP_PASSWORD"].replace(" ", "")
    with smtplib.SMTP("smtp.gmail.com", 587, timeout=30) as smtp:
        smtp.starttls(context=mailer._tls_context())
        smtp.login(address, password)
    return "logged in, nothing sent"


def slack_channel():
    if not slack.configured():
        raise RuntimeError("SLACK_BOT_TOKEN or SLACK_CHANNEL not set, so messages go by email only")
    bot = slack.call("auth.test", {}, form=True)
    channel = slack.call("conversations.info", {"channel": os.environ["SLACK_CHANNEL"]}, form=True)["channel"]
    if not channel.get("is_member"):
        raise RuntimeError(f"the bot isn't in #{channel.get('name')}; type /invite @{bot.get('user')} there")
    return f"signed in as {bot.get('user')}, a member of #{channel.get('name')}; nothing posted"


def nflverse():
    resp = requests.head(f"{NFLVERSE}/players/players.csv", timeout=30, allow_redirects=True)
    resp.raise_for_status()
    return "snap counts and player ids can be downloaded"


def jev():
    importlib.import_module("typesafe_sdk")     # the call itself is left to the first shadow run
    if not os.getenv("TYPESAFE_API_KEY"):
        raise RuntimeError("no TYPESAFE_API_KEY")
    return "SDK installed and key set (not called)"


def main():
    load_dotenv(Path(__file__).with_name(".env"))     # on GitHub there is no .env; the secrets are the environment
    print(f"Hosted check, {datetime.now():%a %b %d %H:%M} local time ({time.tzname[0]}), Python {sys.version.split()[0]}\n")
    results, found = [], {}
    check("settings from .env or secrets", settings, results)
    espn = ESPN.from_env() if all(os.getenv(key) for key in ("LEAGUE_ID", "ESPN_S2", "SWID")) else None
    if espn:
        check("ESPN: league and your team", lambda: espn_reads(espn, found), results)
        if "week" in found:
            check("ESPN: free agents and player cards", lambda: espn_players(espn, found), results)
            check("ESPN: NFL schedule", lambda: espn_schedule(espn, found), results)
    check("ESPN: lineup host", espn_write_host, results)
    check("Anthropic API", anthropic, results)
    check("Gmail", gmail, results)
    check("Slack", slack_channel, results, essential=slack.configured())
    check("nflverse downloads", nflverse, results)
    check("Jev (shadow ranking only)", jev, results, essential=False)
    failed = [name for name, passed, essential in results if essential and not passed]
    print("\n" + (f"Not ready: {', '.join(failed)}." if failed else "Everything the bot needs is reachable from here."))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
