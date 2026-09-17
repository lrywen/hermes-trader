#!/usr/bin/env python3
"""P0-3 — single home for the nightly/hourly job schedule.

Replaces the inlined ``while true`` bash window block in docker-compose
with testable data + pure functions. The schedule (UTC, mirrors the
compose block exactly):

  00:10  daily_report --push
  00:20  pullback_shadow_daily --window-hours 24 --push
  00:30  reconcile_ta_late_entry_shadow --window-hours 8 --write
  00:40  reconcile_xs_reversal_shadow --write
  00:50  reconcile_relax_tier_shadow --write
  00:55  reconcile_short_only_shadow --window-hours 24 --write
  HH:05  macro_regime_watch --push (every hour)

Each job is eligible for a 2-minute window; the runner dedups by slot id
(UTC day for daily jobs, UTC hour for the hourly one), so a 60s poll
cadence fires each job at most once per slot even if the job itself is
fast.
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

UTC = timezone.utc
REPO_ROOT = Path(__file__).resolve().parent.parent

logger = logging.getLogger("scheduler")


@dataclass(frozen=True)
class ScheduledJob:
    name: str
    argv: tuple[str, ...]
    log_name: str
    # Exactly one of the two schedules is set.
    minute_of_day: Optional[int] = None  # daily job, 0..1439 UTC
    minute_of_hour: Optional[int] = None  # hourly job, 0..59
    extra_env: tuple[tuple[str, str], ...] = ()
    window_minutes: int = 2


def _daily(
    name: str,
    hour: int,
    minute: int,
    argv: tuple[str, ...],
    log_name: str,
    extra_env: tuple[tuple[str, str], ...] = (),
) -> ScheduledJob:
    return ScheduledJob(
        name=name,
        argv=argv,
        log_name=log_name,
        minute_of_day=hour * 60 + minute,
        extra_env=extra_env,
    )


SCHEDULED_JOBS: tuple[ScheduledJob, ...] = (
    _daily(
        "daily_report", 0, 10,
        ("scripts/daily_report.py", "--push"),
        "daily-report.log",
    ),
    _daily(
        "pullback_shadow_daily", 0, 20,
        ("scripts/pullback_shadow_daily.py", "--window-hours", "24", "--push"),
        "pullback-shadow-report.log",
    ),
    _daily(
        "reconcile_ta_late_entry_shadow", 0, 30,
        ("scripts/reconcile_ta_late_entry_shadow.py", "--window-hours", "8", "--write"),
        "ta-late-reconcile.log",
    ),
    _daily(
        "reconcile_xs_reversal_shadow", 0, 40,
        ("scripts/reconcile_xs_reversal_shadow.py", "--write"),
        "xs-reversal-reconcile.log",
    ),
    _daily(
        "reconcile_relax_tier_shadow", 0, 50,
        ("scripts/reconcile_relax_tier_shadow.py", "--write"),
        "relax-tier-reconcile.log",
    ),
    _daily(
        "reconcile_short_only_shadow", 0, 55,
        ("scripts/reconcile_short_only_shadow.py", "--window-hours", "24", "--write"),
        "short-only-reconcile.log",
        extra_env=(("HERMES_SHORT_ONLY_SHADOW_FILE", "/data/short_only_shadow.jsonl"),),
    ),
    ScheduledJob(
        name="macro_regime_watch",
        argv=("scripts/macro_regime_watch.py", "--push"),
        log_name="macro-regime-watch.log",
        minute_of_hour=5,
    ),
)


def is_within_window(job: ScheduledJob, now: datetime) -> bool:
    """Pure: True when ``now`` (UTC) falls in the job's firing window."""
    if job.minute_of_hour is not None:
        start = job.minute_of_hour
        return start <= now.minute < start + job.window_minutes
    if job.minute_of_day is not None:
        elapsed = now.hour * 60 + now.minute - job.minute_of_day
        return 0 <= elapsed < job.window_minutes
    return False


def slot_id(job: ScheduledJob, now: datetime) -> str:
    """Dedup key: UTC day for daily jobs, UTC hour for hourly jobs."""
    if job.minute_of_hour is not None:
        return now.strftime("%Y-%m-%dT%H")
    return now.strftime("%Y-%m-%d")


def due_jobs(now: datetime, last_runs: dict[str, str]) -> list[ScheduledJob]:
    """Pure: jobs whose window is open and whose slot has not run."""
    return [
        job
        for job in SCHEDULED_JOBS
        if is_within_window(job, now) and last_runs.get(job.name) != slot_id(job, now)
    ]


def _run_job(job: ScheduledJob, data_dir: str, repo_root: Path) -> int:
    """Run one job foreground, appending output to its /data log."""
    log_path = Path(data_dir) / job.log_name
    cmd = [sys.executable, *job.argv]
    env = {**os.environ, **dict(job.extra_env)}
    with open(log_path, "a") as log_fh:
        log_fh.write(
            f"\n[scheduler] {datetime.now(UTC).isoformat()} run {job.name}: "
            f"{' '.join(cmd)}\n"
        )
        log_fh.flush()
        result = subprocess.run(
            cmd,
            cwd=str(repo_root),
            env=env,
            stdout=log_fh,
            stderr=subprocess.STDOUT,
            check=False,
        )
        log_fh.write(f"[scheduler] {job.name} exit={result.returncode}\n")
        return result.returncode


def run_due(
    now: datetime,
    last_runs: dict[str, str],
    executor: Optional[Callable[..., int]] = None,
    data_dir: str = "/data",
    repo_root: Optional[Path] = None,
) -> dict[str, str]:
    """Run every due job sequentially; return the updated slot state.

    A failing job is logged LOUD but never tears down the loop, and its
    slot is still marked so a broken script cannot hot-retry every 60s
    (it gets a fresh chance at the next slot, matching the old shell
    block's post-run sleep 120 dedup).
    """
    run = executor or _run_job
    root = repo_root or REPO_ROOT
    state = dict(last_runs)
    for job in due_jobs(now, state):
        logger.info("[scheduler] firing %s", job.name)
        try:
            rc = run(job, data_dir=data_dir, repo_root=root)
            if rc:
                logger.error("[scheduler] %s exited rc=%s (see /data/%s)",
                             job.name, rc, job.log_name)
        except Exception:
            logger.exception("[scheduler] %s raised", job.name)
        state[job.name] = slot_id(job, now)
    return state


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    state: dict[str, str] = {}
    while True:
        try:
            state = run_due(datetime.now(UTC), state)
        except Exception:
            logger.exception("[scheduler] tick failed")
        time.sleep(60)


if __name__ == "__main__":
    raise SystemExit(main())
