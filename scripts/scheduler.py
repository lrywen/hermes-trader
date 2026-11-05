#!/usr/bin/env python3
"""P0-3 — single home for the nightly/hourly job schedule.

Replaces the inlined ``while true`` bash window block in docker-compose
with testable data + pure functions. The schedule (UTC):

  00:00  clean_trades_raw
  00:10  daily_report --push
  00:15  reconcile_selection_attribution --write
  00:20  pullback_shadow_daily --window-hours 24 --push
  00:25  reconcile_gate_outcomes --write
  00:30  reconcile_ta_late_entry_shadow --window-hours 8 --write
  00:35  reconcile_fills --window-hours 26 --alert-on-orphan
  00:40  reconcile_xs_reversal_shadow --write
  00:50  reconcile_relax_tier_shadow --write
  00:55  prepare_canary_promotion --write
  HH:05  macro_regime_watch --push (every hour)
  HH:45  xs_reversal_sweep (every hour; universe-wide shadow scan)

P0 (2026-11-06): xs_reversal_sweep. The oversold-bounce shadow arm was only
evaluated when a coin happened to be an entry candidate, so it went 3.3 days
without a trigger while its heartbeat stayed fresh. The sweep evaluates the
whole universe once per hour, off the trade path (record-only).

P1 (2026-11-01): reconcile_fills joins the daily table at 00:35. The script
existed but was never scheduled, so local↔exchange fill drift (the "measurement
debt" in the fusion doc) was never measured. Window 26h covers a UTC day plus
boundary slack; exit code 2 (orphan) surfaces as a scheduler error log line.

Each job is eligible for a 2-minute window; the runner dedups by slot id
(UTC day for daily jobs, UTC hour for the hourly one), so a 60s poll
cadence fires each job at most once per slot even if the job itself is
fast.
"""
from __future__ import annotations

import json
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
    # Optional weekly gate: when set (0=Mon .. 6=Sun, UTC), a minute_of_day job
    # only fires on that weekday. None = every day (unchanged default).
    weekday: Optional[int] = None
    extra_env: tuple[tuple[str, str], ...] = ()
    window_minutes: int = 2
    # P0-7 (recheck 2026-10-18): 单 job 硬超时。此前 _run_job 的 subprocess.run
    # 无 timeout，且主循环单线程串行 —— 一个 job 挂住会卡死整个循环，后续所有
    # job 全部错过窗口、活性信号冻结、且无任何告警（只是日志少了 exit 行）。
    # 给每个 job 一个默认 900s 上限；超时按退出码 124（类似 kill -TERM）处理，
    # 循环继续推进下一个 job，不再被单个挂死 job 拖垮。
    timeout_s: int = 900


def _daily(
    name: str,
    hour: int,
    minute: int,
    argv: tuple[str, ...],
    log_name: str,
    extra_env: tuple[tuple[str, str], ...] = (),
    timeout_s: Optional[int] = None,
) -> ScheduledJob:
    return ScheduledJob(
        name=name,
        argv=argv,
        log_name=log_name,
        minute_of_day=hour * 60 + minute,
        extra_env=extra_env,
        timeout_s=timeout_s if timeout_s is not None else 900,
    )


SCHEDULED_JOBS: tuple[ScheduledJob, ...] = (
    _daily(
        "clean_trades_raw", 0, 0,
        ("scripts/clean_trades_raw.py",),
        "clean-trades-raw.log",
    ),
    _daily(
        "reconcile_gate_outcomes", 0, 25,
        ("scripts/reconcile_gate_outcomes.py", "--write"),
        "gate-outcomes-reconcile.log",
    ),
    _daily(
        "daily_report", 0, 10,
        ("scripts/daily_report.py", "--push"),
        "daily-report.log",
    ),
    _daily(
        "reconcile_selection_attribution", 0, 15,
        ("scripts/reconcile_selection_attribution.py", "--write"),
        "selection-attribution-verdict.log",
        # P0-E (recheck-4)：默认 900s 超时 > 每日任务 5min 间隔，reconcile 的
        # 大积压一跑就吞掉 00:20/00:25 的相邻 job。降到 240s（< 间隔），让本 job
        # 即使跑不完也不会卡住整批；配合脚本内 max_per_sweep 在窗口内诚实完成。
        timeout_s=240,
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
        "reconcile_fills", 0, 35,
        ("scripts/reconcile_fills.py", "--window-hours", "26", "--alert-on-orphan"),
        "reconcile-fills.log",
        # 26h look-back over the whole day's fills can exceed the 5-min gap to
        # the 00:40 job on a large backlog; 300s keeps it inside its slot.
        timeout_s=300,
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
        "prepare_canary_promotion", 0, 55,
        ("scripts/prepare_canary_promotion.py", "--write"),
        "prepare-canary-promotion.log",
    ),
    ScheduledJob(
        name="macro_regime_watch",
        argv=("scripts/macro_regime_watch.py", "--push"),
        log_name="macro-regime-watch.log",
        minute_of_hour=5,
    ),
    # P0 (2026-11-06): universe-wide xs_reversal shadow sweep. Runs at :45 so
    # it never shares a window with macro_regime_watch (:05) or the 00:xx
    # daily batch; the 44-coin serial walk is budget-capped well inside the
    # 900s job timeout.
    ScheduledJob(
        name="xs_reversal_sweep",
        argv=("scripts/xs_reversal_sweep.py",),
        log_name="xs-reversal-sweep.log",
        minute_of_hour=45,
    ),
    # P1 (2026-11-01): weekly refresh of the production 1d candle cache so the
    # 400-day history every gate reconcile depends on never goes stale again.
    # Sunday 01:10 UTC — clear of the 00:xx daily batch.
    # Intervals 1d 4h 1h: the "intraday maintained by their own paths" comment
    # was wrong — grep shows flush_disk_cache is only called by research/backfill
    # scripts, and collect_candles.py is in NO scheduler/cron. Without this job
    # the 1h/4h cache sat stale at 10-30 22:00 (40/40), forcing every 4h ATR call
    # to lazy-backfill ~48 bars over the network (a rate-limit there would size
    # stops off a 2-day-old ATR). One job covers all three intervals.
    ScheduledJob(
        name="backfill_hist_candles",
        argv=("scripts/backfill_hist_candles.py",
              "--intervals", "1d", "4h", "1h",
              "--days-map", "1d:400", "4h:14", "1h:14"),
        log_name="backfill-hist-candles.log",
        minute_of_day=1 * 60 + 10,
        weekday=6,  # Sunday (UTC)
        timeout_s=1800,  # 44 coins x 3 intervals, incremental; ~5-8 min typical
    ),
)


def is_within_window(job: ScheduledJob, now: datetime) -> bool:
    """Pure: True when ``now`` (UTC) falls in the job's firing window."""
    if job.minute_of_hour is not None:
        start = job.minute_of_hour
        return start <= now.minute < start + job.window_minutes
    if job.minute_of_day is not None:
        if job.weekday is not None and now.weekday() != job.weekday:
            return False
        elapsed = now.hour * 60 + now.minute - job.minute_of_day
        return 0 <= elapsed < job.window_minutes
    return False


def slot_id(job: ScheduledJob, now: datetime) -> str:
    """Dedup key: UTC day for daily jobs, UTC hour for hourly jobs.

    Weekly jobs key on the ISO week so the single weekly firing is deduped
    across the whole week (not re-armed each day)."""
    if job.minute_of_hour is not None:
        return now.strftime("%Y-%m-%dT%H")
    if job.weekday is not None:
        iso = now.isocalendar()
        return f"{iso[0]}-W{iso[1]:02d}"
    return now.strftime("%Y-%m-%d")


def due_jobs(now: datetime, last_runs: dict[str, str]) -> list[ScheduledJob]:
    """Pure: jobs whose window is open and whose slot has not run."""
    return [
        job
        for job in SCHEDULED_JOBS
        if is_within_window(job, now) and last_runs.get(job.name) != slot_id(job, now)
    ]


# ── P0-3: last_ok 活性信号 + 状态持久化 ────────────────────────────────────
# 「已运行」≠「已成功」。仅当 job 以 rc==0 结束时才登记 last_ok，并把状态
# 持久化到 /data/scheduler_state.json，使调度器活性可观测、可回滚。
def _scheduler_state_path(data_dir: str) -> Path:
    return Path(data_dir) / "scheduler_state.json"


def _load_scheduler_state(data_dir: str) -> dict[str, str]:
    """Load persisted slot/last_ok state, tolerating a missing/corrupt file."""
    try:
        with open(_scheduler_state_path(data_dir), encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, ValueError, OSError):
        return {}


def _save_scheduler_state(data_dir: str, state: dict[str, str]) -> None:
    """Persist slot + last_ok state in place.

    scheduler_state.json is a single-file bind mount (/data → host runtime/).
    The former atomic ``tmp + os.replace`` pattern broke here: the tmp file landed
    in the named volume (/data) while the target sat on a different device (the
    host bind mount), so ``os.replace`` raised EXDEV on every tick and the state
    was never persisted (scheduler logged "failed to persist" every 60s, watchdog
    stayed STALE). The scheduler is the single writer, so in-place truncate+write
    is safe; a torn write is tolerated by ``_load_scheduler_state`` (returns {}).
    """
    try:
        path = _scheduler_state_path(data_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(state, f)
    except OSError:
        logger.warning("[scheduler] failed to persist state to %s", data_dir)


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
        try:
            result = subprocess.run(
                cmd,
                cwd=str(repo_root),
                env=env,
                stdout=log_fh,
                stderr=subprocess.STDOUT,
                check=False,
                timeout=job.timeout_s,
            )
        except subprocess.TimeoutExpired as e:
            # P0-7: 单 job 超时不再拖垮整个调度器。按退出码 124（=kill -TERM）
            # 处理；run_due 据此视为「已运行但未成功」，不写 last_ok。
            log_fh.write(
                f"[scheduler] {job.name} TIMEOUT after {job.timeout_s}s"
                f" (output truncated; stdout above)\n"
            )
            log_fh.flush()
            return 124
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

    P0-3: the returned state carries two signals per job:
      * ``state[job.name] = slot``            — "last ran in slot" (marked
        regardless of outcome, to keep the anti-hot-retry contract);
      * ``state["last_ok:" + job.name] = slot`` — "last SUCCEEDED in slot",
        written ONLY when the job exits rc==0. A job that fails is marked as
        "ran" but never as "succeeded", so the panel can answer "when did
        this last succeed" and a watchdog can flag a healthy loop that is
        silently failing every job.
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
            else:
                state[f"last_ok:{job.name}"] = slot_id(job, now)
        except Exception:
            logger.exception("[scheduler] %s raised", job.name)
        state[job.name] = slot_id(job, now)
    return state


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    data_dir = os.environ.get("HERMES_DATA_DIR", "/data")
    state = _load_scheduler_state(data_dir)
    while True:
        try:
            state = run_due(
                datetime.now(UTC),
                state,
                data_dir=data_dir,
            )
            _save_scheduler_state(data_dir, state)
        except Exception:
            logger.exception("[scheduler] tick failed")
        time.sleep(60)


if __name__ == "__main__":
    raise SystemExit(main())
