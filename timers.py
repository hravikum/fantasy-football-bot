"""Starts the bot's GitHub runs from cron-job.org, so they start on time.

GitHub's own schedule started every job 7 1/2 to 9 hours late in its first two days (Nov 18-19,
2026): the 6:07 waiver report at 2:21pm, the 9:05 lineup pass at 4:43pm. A run started through
GitHub's API (workflow_dispatch, the same as Run workflow on the Actions page) starts within
seconds, so cron-job.org sends that request at each time below, and GitHub's schedule is retired.

Run it on the Mac, once, and again whenever TIMERS changes. It creates the jobs, or updates them
in place, and removes any it made that TIMERS no longer lists:

    .venv/bin/python3 timers.py --dry-run     # what it would send, with the GitHub token hidden
    .venv/bin/python3 timers.py

Between seasons (season.py says when, in Slack), switch its jobs off and back on. Nothing else
about them changes, and a plain run turns them on as well:

    .venv/bin/python3 timers.py --pause
    .venv/bin/python3 timers.py --resume

It reads two keys, from .env or the environment:
    CRONJOB_API_KEY         cron-job.org > Console > Settings > API
    GITHUB_DISPATCH_TOKEN   a fine-grained GitHub token for this repository only, with Actions:
                            read and write. cron-job.org keeps it and sends it with each request.
"""
import argparse
import json
import os
import sys
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv

REPO = "hravikum/fantasy-football-bot"
WORKFLOW = f"https://api.github.com/repos/{REPO}/actions/workflows/bot.yml"
CRONJOB = "https://api.cron-job.org"
TITLE = "Fantasy bot: "          # every job this script owns starts with this, and only those are touched
EASTERN = "America/New_York"
GAME_DAY = [2, 17, 32, 47]
EVERY = [-1]

# (title, job for hosted_run.sh, weekdays 0 = Sunday, hours, minutes), Eastern time: bot.yml's schedule.
TIMERS = [
    ("waivers", "waivers", EVERY, [6], [7]),
    ("lineup", "lineup", EVERY, [9], [5]),
    ("game day, Sunday", "gameday", [0], list(range(7, 21)), GAME_DAY),
    ("game day, Monday night", "gameday", [1], list(range(17, 21)), GAME_DAY),
    ("game day, Thursday to Saturday", "gameday", [4, 5, 6], list(range(11, 21)), GAME_DAY),
    ("trades", "trades", EVERY, [8, 23], [3]),
]


def job(title, name, wdays, hours, minutes, token):
    """One cron-job.org job: at these times, ask GitHub to run bot.yml's `name` job on main."""
    return {
        "title": TITLE + title,
        "url": f"{WORKFLOW}/dispatches",
        "enabled": True,
        "saveResponses": True,
        "requestMethod": 1,  # POST
        "schedule": {"timezone": EASTERN, "expiresAt": 0, "months": EVERY, "mdays": EVERY,
                     "wdays": wdays, "hours": hours, "minutes": minutes},
        "extendedData": {
            "headers": {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                        "X-GitHub-Api-Version": "2022-11-28", "Content-Type": "application/json"},
            "body": json.dumps({"ref": "main", "inputs": {"job": name}}),
        },
        # An email at the first failed request, when one succeeds again, and if cron-job.org gives up.
        "notification": {"onFailure": True, "onFailureCount": 1, "onSuccess": True, "onDisable": True},
    }


def cronjob(method, path, key, body=None):
    response = requests.request(method, CRONJOB + path, json=body, timeout=30,
                                headers={"Authorization": f"Bearer {key}"})
    if response.status_code != 200:
        sys.exit(f"cron-job.org {method} {path} answered {response.status_code}: {response.text[:300]}")
    return response.json()


def check_token(token):
    """Fail before touching cron-job.org if the token can't see the workflow it will start."""
    response = requests.get(WORKFLOW, timeout=30, headers={"Authorization": f"Bearer {token}",
                                                           "Accept": "application/vnd.github+json"})
    if response.status_code != 200:
        sys.exit(f"GitHub answered {response.status_code} for bot.yml with GITHUB_DISPATCH_TOKEN: is it for "
                 f"{REPO}, with Actions: read and write, and not expired?")


def switch(enabled):
    """Turn this script's jobs on or off, and only those."""
    load_dotenv()
    key = os.getenv("CRONJOB_API_KEY")
    if not key:
        sys.exit("Set CRONJOB_API_KEY in .env first.")
    ours = [j for j in cronjob("GET", "/jobs", key)["jobs"] if j["title"].startswith(TITLE)]
    if not ours:
        sys.exit(f"No jobs titled '{TITLE}...' on cron-job.org: run timers.py with no flags to create them.")
    for each in ours:
        cronjob("PATCH", f"/jobs/{each['jobId']}", key, {"job": {"enabled": enabled}})
        print(f"{'on ' if enabled else 'off'}  {each['title']}")
    print("Runs start again at their usual times." if enabled
          else "Nothing starts until timers.py --resume (or a plain timers.py).")


def main(dry_run=False):
    load_dotenv()
    token, key = os.getenv("GITHUB_DISPATCH_TOKEN"), os.getenv("CRONJOB_API_KEY")
    wanted = [job(*timer, token=token or "") for timer in TIMERS]
    if dry_run:
        for each in wanted:
            each["extendedData"]["headers"]["Authorization"] = "Bearer <GITHUB_DISPATCH_TOKEN>"
        print(json.dumps(wanted, indent=2))
        return
    missing = [name for name, value in (("GITHUB_DISPATCH_TOKEN", token), ("CRONJOB_API_KEY", key)) if not value]
    if missing:
        sys.exit(f"Set {' and '.join(missing)} in .env first.")
    check_token(token)

    ours = {j["title"]: j["jobId"] for j in cronjob("GET", "/jobs", key)["jobs"] if j["title"].startswith(TITLE)}
    created = 0
    for each in wanted:
        if each["title"] in ours:
            cronjob("PATCH", f"/jobs/{ours.pop(each['title'])}", key, {"job": each})
            print(f"updated  {each['title']}")
        else:
            if created:
                time.sleep(13)  # cron-job.org takes at most 5 new jobs a minute
            cronjob("PUT", "/jobs", key, {"job": each})
            created += 1
            print(f"created  {each['title']}")
    for title, job_id in ours.items():
        cronjob("DELETE", f"/jobs/{job_id}", key)
        print(f"removed  {title} (no longer in TIMERS)")

    print("\nNext runs (Eastern):")
    for each in sorted((j for j in cronjob("GET", "/jobs", key)["jobs"] if j["title"].startswith(TITLE)),
                       key=lambda j: j.get("nextExecution") or 0):
        when = each.get("nextExecution")
        when = datetime.fromtimestamp(when, ZoneInfo(EASTERN)).strftime("%a %b %d %I:%M %p") if when else "-"
        print(f"  {when}  {each['title']}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Set up the cron-job.org timers that start the bot's GitHub runs.")
    parser.add_argument("--dry-run", action="store_true", help="print the jobs it would send, and change nothing")
    which = parser.add_mutually_exclusive_group()
    which.add_argument("--pause", action="store_true", help="switch the jobs off for the off-season")
    which.add_argument("--resume", action="store_true", help="switch them back on")
    args = parser.parse_args()
    if args.pause or args.resume:
        switch(enabled=args.resume)
    else:
        main(args.dry_run)
