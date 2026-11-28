"""Tests for the end-of-season check and the season a date belongs to. Run: python3 test_season.py"""
import json
import sys
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path

import espn
import season

LAST_GAME = datetime(2027, 1, 4, 20, 15)     # week 17's Monday night game


def schedule(period=17, last=LAST_GAME):
    early = int((last - timedelta(days=1)).timestamp() * 1000)
    late = int(last.timestamp() * 1000)
    return {"settings": {"proTeams": [
        {"proGamesByScoringPeriod": {str(period): [{"date": early}], "18": [{"date": late + 6 * 86400000}]}},
        {"proGamesByScoringPeriod": {str(period): [{"date": late}]}}]}}


class FakeESPN:
    def __init__(self, current=17, final=17, season=2026, broken=False):
        self.current, self.final, self.season, self.broken = current, final, season, broken

    def league(self, *views, week=None):
        if self.broken:
            raise espn.ESPNError("League 1 not found for 2027. Check LEAGUE_ID.")
        return {"scoringPeriodId": self.current, "status": {"finalScoringPeriod": self.final}}

    def _pro_schedule(self):
        return schedule(self.final)


def test_a_date_belongs_to_the_season_that_started_before_it():
    assert espn.nfl_season(date(2027, 1, 3)) == 2026, "week 17 is played in January"
    assert espn.nfl_season(date(2026, 12, 31)) == 2026
    assert espn.nfl_season(date(2027, 7, 31)) == 2026, "the off-season belongs to the finished one"
    assert espn.nfl_season(date(2027, 8, 1)) == 2027, "from August, next season's league"


def test_the_last_kickoff_of_a_week():
    assert season.last_kickoff(schedule(), 17) == LAST_GAME
    assert season.last_kickoff(schedule(), 16) is None


def test_over_once_the_last_week_is_done():
    fake = FakeESPN()
    league = fake.league()
    assert not season.is_over(league, fake, LAST_GAME - timedelta(days=2)), "week 17 is still being played"
    assert not season.is_over(league, fake, LAST_GAME + timedelta(hours=11)), "Monday night needs its grace"
    assert season.is_over(league, fake, LAST_GAME + timedelta(hours=13))
    assert season.is_over(FakeESPN(current=18).league(), fake, LAST_GAME - timedelta(days=9)), "past the last period"
    assert not season.is_over(FakeESPN(current=12).league(), fake, LAST_GAME + timedelta(days=30))
    assert not season.is_over({"scoringPeriodId": 18, "status": {}}, fake, LAST_GAME), "ESPN didn't say: not over"


def run(fake, now, path, sent=None):
    saved = season.ESPN.from_env, season.mailer.send
    season.ESPN.from_env = classmethod(lambda cls: fake)
    season.mailer.send = lambda subject, body, **kwargs: sent.append((subject, body)) or True \
        if sent is not None else False
    try:
        return season.main(now, path)
    finally:
        season.ESPN.from_env, season.mailer.send = saved


def test_the_notice_goes_out_once_a_season():
    path, sent = Path(tempfile.mkdtemp()) / "season_over.json", []
    after = LAST_GAME + timedelta(days=1)
    assert run(FakeESPN(), LAST_GAME - timedelta(days=2), path, sent) == 0 and sent == []
    assert run(FakeESPN(), after, path, sent) == season.OVER
    assert len(sent) == 1 and sent[0][0] == "The 2026 season is over: pause the timers"
    assert "python3 timers.py --pause" in sent[0][1] and "python3 timers.py --resume" in sent[0][1]
    assert run(FakeESPN(), after + timedelta(hours=1), path, sent) == season.OVER and len(sent) == 1, \
        "the next run stops quietly"
    assert json.loads(path.read_text()) == {"season": 2026}
    assert run(FakeESPN(season=2027), after + timedelta(days=365), path, sent) == season.OVER and len(sent) == 2, \
        "next season's end gets its own notice"


def test_a_notice_that_didnt_go_out_is_tried_again():
    path = Path(tempfile.mkdtemp()) / "season_over.json"
    assert run(FakeESPN(), LAST_GAME + timedelta(days=1), path, sent=None) == season.OVER
    assert not path.exists(), "not recorded, so the next run tries again"


def test_when_it_cant_tell_the_job_runs():
    path = Path(tempfile.mkdtemp()) / "season_over.json"
    assert run(FakeESPN(broken=True), LAST_GAME + timedelta(days=1), path, []) == 0


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
