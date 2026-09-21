"""P0-3 — scheduler time-window logic (scripts/scheduler.py).

The seven inlined compose shell windows become data + pure functions so
the boundaries are unit-testable instead of living in a `while true`
bash loop. Contract:

  * daily jobs match a 2-minute UTC window starting at their HH:MM;
  * the hourly macro job matches minutes [05, 07) of EVERY hour;
  * a job fires at most once per slot (day for daily jobs, hour for the
    hourly one);
  * the runner never raises on job failure and still marks the slot done
    (LOUD via the job log / error log), matching the shell behaviour.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from scripts import scheduler
from scripts.scheduler import (
    SCHEDULED_JOBS,
    ScheduledJob,
    due_jobs,
    is_within_window,
    run_due,
    slot_id,
)

UTC = timezone.utc


def _utc(y: int = 2026, mo: int = 9, d: int = 17, h: int = 0, m: int = 0) -> datetime:
    return datetime(y, mo, d, h, m, tzinfo=UTC)


def _job(name: str) -> ScheduledJob:
    return next(j for j in SCHEDULED_JOBS if j.name == name)


# ---------------------------------------------------------------------------
# Job table pins the compose schedule (regression guard against drift)
# ---------------------------------------------------------------------------

def test_job_table_has_six_jobs_and_existing_scripts() -> None:
    repo_root = Path(scheduler.__file__).resolve().parent.parent
    assert len(SCHEDULED_JOBS) == 6
    for job in SCHEDULED_JOBS:
        assert (repo_root / job.argv[0]).is_file(), job.argv[0]


def test_daily_job_table_matches_compose_windows() -> None:
    table = {
        "daily_report": (10, ("scripts/daily_report.py", "--push"), "daily-report.log", {}),
        "pullback_shadow_daily": (
            20,
            ("scripts/pullback_shadow_daily.py", "--window-hours", "24", "--push"),
            "pullback-shadow-report.log",
            {},
        ),
        "reconcile_ta_late_entry_shadow": (
            30,
            ("scripts/reconcile_ta_late_entry_shadow.py", "--window-hours", "8", "--write"),
            "ta-late-reconcile.log",
            {},
        ),
        "reconcile_xs_reversal_shadow": (
            40,
            ("scripts/reconcile_xs_reversal_shadow.py", "--write"),
            "xs-reversal-reconcile.log",
            {},
        ),
        "reconcile_relax_tier_shadow": (
            50,
            ("scripts/reconcile_relax_tier_shadow.py", "--write"),
            "relax-tier-reconcile.log",
            {},
        ),
    }
    for name, (minute, argv, log_name, env) in table.items():
        job = _job(name)
        assert job.minute_of_day == minute
        assert job.minute_of_hour is None
        assert tuple(job.argv) == argv
        assert job.log_name == log_name
        assert dict(job.extra_env) == env
        assert job.window_minutes == 2


def test_hourly_job_table_matches_compose_window() -> None:
    job = _job("macro_regime_watch")
    assert job.minute_of_day is None
    assert job.minute_of_hour == 5
    assert tuple(job.argv) == ("scripts/macro_regime_watch.py", "--push")
    assert job.log_name == "macro-regime-watch.log"


# ---------------------------------------------------------------------------
# Pure window boundaries
# ---------------------------------------------------------------------------

def test_daily_window_boundaries() -> None:
    job = _job("daily_report")  # 00:10, window [10, 12)
    assert is_within_window(job, _utc(h=0, m=9)) is False
    assert is_within_window(job, _utc(h=0, m=10)) is True
    assert is_within_window(job, _utc(h=0, m=11)) is True
    assert is_within_window(job, _utc(h=0, m=12)) is False
    # 23:59 must not match a 00:10 job
    assert is_within_window(job, _utc(h=23, m=59)) is False


def test_each_daily_job_only_open_at_its_own_minute() -> None:
    daily = [j for j in SCHEDULED_JOBS if j.minute_of_day is not None]
    for job in daily:
        h, m = divmod(job.minute_of_day, 60)
        assert is_within_window(job, _utc(h=h, m=m)) is True
        assert is_within_window(job, _utc(h=h, m=m + 2)) is False


def test_hourly_window_reopens_every_hour() -> None:
    job = _job("macro_regime_watch")
    assert is_within_window(job, _utc(h=0, m=4)) is False
    assert is_within_window(job, _utc(h=0, m=5)) is True
    assert is_within_window(job, _utc(h=0, m=6)) is True
    assert is_within_window(job, _utc(h=0, m=7)) is False
    assert is_within_window(job, _utc(h=13, m=5)) is True
    assert is_within_window(job, _utc(h=23, m=5)) is True


def test_due_jobs_dedup_within_slot_and_reopens_next_day() -> None:
    state: dict = {}
    due = due_jobs(_utc(h=0, m=10), state)
    assert [j.name for j in due] == ["daily_report"]

    # Runner marks the slot after execution.
    state = run_due(_utc(h=0, m=10), state, executor=lambda job, **kw: None)
    assert due_jobs(_utc(h=0, m=11), state) == []  # window still open, deduped
    assert due_jobs(_utc(h=0, m=12), state) == []  # window closed
    # Next day reopens.
    (due_tomorrow,) = due_jobs(_utc(d=18, h=0, m=10), state)
    assert due_tomorrow.name == "daily_report"


def test_hourly_job_dedup_per_hour_but_fires_each_hour() -> None:
    state: dict = {}
    state = run_due(_utc(h=0, m=5), state, executor=lambda job, **kw: None)
    assert due_jobs(_utc(h=0, m=6), state) == []
    (job,) = due_jobs(_utc(h=1, m=5), state)
    assert job.name == "macro_regime_watch"
    assert slot_id(job, _utc(h=1, m=5)) == "2026-09-17T01"


def test_hourly_and_daily_jobs_never_share_a_window() -> None:
    # The compose schedule relies on the 05-07 hourly window never
    # overlapping any daily window (10/20/30/40/55-57).
    for minute in range(60):
        names = {j.name for j in SCHEDULED_JOBS if is_within_window(j, _utc(h=0, m=minute))}
        assert len(names) <= 1, (minute, names)


# ---------------------------------------------------------------------------
# Runner shell
# ---------------------------------------------------------------------------

def test_runner_invokes_executor_with_job_and_env_and_returns_state() -> None:
    executed: list[ScheduledJob] = []

    def executor(job: ScheduledJob, **kwargs) -> int:
        executed.append(job)
        return 0

    state = run_due(_utc(h=0, m=50), {}, executor=executor, data_dir="/tmp/fake-data")

    job = executed[0]
    assert job.name == "reconcile_relax_tier_shadow"
    assert state == {"reconcile_relax_tier_shadow": "2026-09-17"}


def test_runner_survives_failing_job_and_still_marks_slot() -> None:
    def boom(job, **kwargs):
        raise RuntimeError("job exploded")

    state = run_due(_utc(h=0, m=10), {}, executor=boom)
    # Slot is marked so the loop does not hot-retry a broken job every 60s.
    assert state == {"daily_report": "2026-09-17"}
    assert due_jobs(_utc(h=0, m=11), state) == []


def test_runner_nothing_due_runs_nothing() -> None:
    calls = []
    state = run_due(_utc(h=12, m=0), {}, executor=lambda job, **kw: calls.append(job))
    assert calls == []
    assert state == {}
