"""One-off check: does the Claude API stop connecting after a Jev pass on this Mac?

Run: python3 diagnose_network.py

Makes the same twenty Jev calls a `--ranker jev` report makes, probing the Claude API
from Python before and after, and from curl (outside Python) right after. It costs a
fraction of a cent. Delete it once we have the answer.
"""
import subprocess
import time
from pathlib import Path

import requests
from dotenv import load_dotenv

import jev

API = "https://api.anthropic.com/v1/messages"


def from_python(label):
    try:
        reply = requests.post(API, json={}, timeout=20,
                              headers={"x-api-key": "not-a-key", "anthropic-version": "2023-06-01"})
        print(f"{label}: connected (HTTP {reply.status_code})")
    except requests.exceptions.RequestException as problem:
        print(f"{label}: FAILED  {type(problem).__name__}: {str(problem)[:120]}")


def from_curl(label):
    result = subprocess.run(["curl", "-sS", "-o", "/dev/null", "-w", "%{http_code}", API],
                            capture_output=True, text=True, timeout=30)
    print(f"{label}: {result.stdout.strip() or result.stderr.strip()}")


def main():
    load_dotenv(Path(__file__).with_name(".env"))
    from typesafe_sdk import TypeSafeClient

    from_python("1. Python, before any Jev calls")
    client = TypeSafeClient(timeout=30.0)
    questions = jev.questions()
    state = {"player": {"name": "Network check", "position": "WR"}, "my_roster": {}, "league": {}}
    started, failures = time.time(), 0
    for _ in range(20):
        try:
            client.system_one(state=state, questions=questions)
        except Exception as problem:  # report it, keep probing
            failures += 1
            last = type(problem).__name__
    note = f", {failures} failed ({last})" if failures else ""
    print(f"   twenty Jev calls in {time.time() - started:.1f}s{note}")
    from_python("2. Python, right after")
    from_curl("3. curl, right after")
    time.sleep(15)
    from_python("4. Python, 15 seconds later")


if __name__ == "__main__":
    main()
