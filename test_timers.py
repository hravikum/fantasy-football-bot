"""Tests for the cron-job.org timers. Run: python3 test_timers.py"""
import contextlib
import io
import json
import os
import re
import sys
from pathlib import Path

import timers


def _jobs():
    return [timers.job(*timer, token="secret-token") for timer in timers.TIMERS]


def test_every_timer_asks_for_a_job_the_workflow_and_hosted_run_know():
    offered = re.search(r"options: \[(.*)\]", Path(".github/workflows/bot.yml").read_text()).group(1)
    known = Path("hosted_run.sh").read_text()
    for each in _jobs():
        name = json.loads(each["extendedData"]["body"])["inputs"]["job"]
        assert name in [o.strip() for o in offered.split(",")], f"bot.yml's Run workflow doesn't offer {name}"
        assert f"| {name})" in known, f"hosted_run.sh has no case for {name}"


def test_a_timer_starts_bot_yml_on_main_in_eastern_time():
    waivers = _jobs()[0]
    assert waivers["url"] == f"https://api.github.com/repos/{timers.REPO}/actions/workflows/bot.yml/dispatches"
    assert waivers["requestMethod"] == 1 and waivers["enabled"]
    assert json.loads(waivers["extendedData"]["body"]) == {"ref": "main", "inputs": {"job": "waivers"}}
    assert waivers["extendedData"]["headers"]["Authorization"] == "Bearer secret-token"
    assert waivers["schedule"]["timezone"] == "America/New_York"
    assert (waivers["schedule"]["hours"], waivers["schedule"]["minutes"]) == ([6], [7])
    sunday = next(j for j in _jobs() if j["title"].endswith("Sunday"))
    assert sunday["schedule"]["wdays"] == [0] and sunday["schedule"]["hours"] == list(range(7, 21))
    assert all(j["title"].startswith(timers.TITLE) for j in _jobs())
    assert len({j["title"] for j in _jobs()}) == len(timers.TIMERS), "titles are how a rerun finds its jobs"


def test_a_dry_run_hides_the_token_and_sends_nothing():
    saved = timers.requests.request, timers.requests.get, os.environ.get("GITHUB_DISPATCH_TOKEN")
    timers.requests.request = timers.requests.get = lambda *a, **k: (_ for _ in ()).throw(AssertionError("sent"))
    os.environ["GITHUB_DISPATCH_TOKEN"] = "secret-token"
    try:
        with contextlib.redirect_stdout(io.StringIO()) as out:
            timers.main(dry_run=True)
    finally:
        timers.requests.request, timers.requests.get = saved[:2]
        os.environ.pop("GITHUB_DISPATCH_TOKEN") if saved[2] is None else os.environ.update(GITHUB_DISPATCH_TOKEN=saved[2])
    assert "secret-token" not in out.getvalue() and "<GITHUB_DISPATCH_TOKEN>" in out.getvalue()
    assert len(json.loads(out.getvalue())) == len(timers.TIMERS)


def test_a_rerun_updates_its_own_jobs_adds_missing_ones_and_leaves_others_alone():
    """Run once, then again after TIMERS changes: no duplicates, and nobody else's jobs touched."""
    calls = []
    existing = [{"jobId": 1, "title": timers.TITLE + "waivers"},
                {"jobId": 2, "title": timers.TITLE + "old job"},
                {"jobId": 3, "title": "Someone else's job"}]

    def fake(method, path, key, body=None):
        calls.append((method, path, (body or {}).get("job", {}).get("title")))
        return {"jobs": existing} if method == "GET" else {}

    saved = (timers.cronjob, timers.check_token, timers.time.sleep, timers.load_dotenv,
             {k: os.environ.get(k) for k in ("GITHUB_DISPATCH_TOKEN", "CRONJOB_API_KEY")})
    timers.cronjob, timers.check_token = fake, lambda token: None
    timers.time.sleep, timers.load_dotenv = (lambda s: None), (lambda: None)
    os.environ.update(GITHUB_DISPATCH_TOKEN="t", CRONJOB_API_KEY="k")
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            timers.main()
    finally:
        timers.cronjob, timers.check_token, timers.time.sleep, timers.load_dotenv = saved[:4]
        for name, value in saved[4].items():
            os.environ.pop(name) if value is None else os.environ.update({name: value})
    assert ("PATCH", "/jobs/1", timers.TITLE + "waivers") in calls
    assert sum(1 for method, _, _ in calls if method == "PUT") == len(timers.TIMERS) - 1
    assert ("DELETE", "/jobs/2", None) in calls, "a job of ours TIMERS dropped is removed"
    assert not any(path == "/jobs/3" for _, path, _ in calls), "someone else's job is never touched"


def test_without_keys_it_stops_before_calling_anyone():
    saved = timers.load_dotenv, timers.cronjob, {k: os.environ.pop(k, None) for k in ("GITHUB_DISPATCH_TOKEN", "CRONJOB_API_KEY")}
    timers.load_dotenv = lambda: None
    timers.cronjob = lambda *a, **k: (_ for _ in ()).throw(AssertionError("called cron-job.org"))
    try:
        timers.main()
        raise AssertionError("expected it to stop")
    except SystemExit as stop:
        assert "GITHUB_DISPATCH_TOKEN and CRONJOB_API_KEY" in str(stop)
    finally:
        timers.load_dotenv, timers.cronjob = saved[:2]
        os.environ.update({k: v for k, v in saved[2].items() if v is not None})


def _switch(enabled, existing, key="k"):
    calls = []

    def fake(method, path, key, body=None):
        calls.append((method, path, body))
        return {"jobs": existing} if method == "GET" else {}

    saved = timers.cronjob, timers.load_dotenv, os.environ.get("CRONJOB_API_KEY")
    timers.cronjob, timers.load_dotenv = fake, (lambda: None)
    os.environ.pop("CRONJOB_API_KEY", None) if key is None else os.environ.update(CRONJOB_API_KEY=key)
    try:
        with contextlib.redirect_stdout(io.StringIO()) as out:
            timers.switch(enabled)
    finally:
        timers.cronjob, timers.load_dotenv = saved[:2]
        os.environ.pop("CRONJOB_API_KEY", None) if saved[2] is None else os.environ.update(CRONJOB_API_KEY=saved[2])
    return calls, out.getvalue()


def test_pause_and_resume_switch_only_our_jobs():
    existing = [{"jobId": 1, "title": timers.TITLE + "waivers"}, {"jobId": 2, "title": timers.TITLE + "lineup"},
                {"jobId": 3, "title": "Someone else's job"}]
    calls, said = _switch(False, existing)
    assert [c for c in calls if c[0] == "PATCH"] == [("PATCH", "/jobs/1", {"job": {"enabled": False}}),
                                                     ("PATCH", "/jobs/2", {"job": {"enabled": False}})]
    assert "timers.py --resume" in said
    calls, _ = _switch(True, existing)
    assert {c[2]["job"]["enabled"] for c in calls if c[0] == "PATCH"} == {True}
    assert not any(path == "/jobs/3" for _, path, _ in calls), "someone else's job is never touched"


def test_pause_says_so_when_there_is_nothing_to_pause():
    for existing, key, expected in (([], "k", "run timers.py with no flags"), ([], None, "CRONJOB_API_KEY")):
        try:
            _switch(False, existing, key)
            raise AssertionError("expected it to stop")
        except SystemExit as stop:
            assert expected in str(stop), stop


if __name__ == "__main__":
    tests = [(name, fn) for name, fn in sorted(vars().items()) if name.startswith("test_")]
    failures = 0
    for name, fn in tests:
        try:
            fn()
            print(f"pass  {name}")
        except AssertionError as e:
            failures += 1
            print(f"FAIL  {name}: {e}")
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    sys.exit(1 if failures else 0)
