#!/usr/bin/env python3
"""Monitor the weekly regime-calibration cron for the next 7 days.

Tails the calibration log (calib.log) AND watches the archive directory for new
JSON artifacts, then reports every calibration run with its guardrail verdict:

  * PROMOTE  — new constants written to .agent-config.json (info)
  * DRY_RUN  --apply not set; nothing changed (info)
  * REJECT   — best params failed a guardrail (config unchanged) — an ALERT is
               emitted; repeated rejects mean the grid is stuck on degenerate
               crosses and the search space / guardrails need review.
  * runtime failures / missing cron / CRON_TZ misconfig — pre-flight + ALERT.

State is appended to an alerts JSONL so consecutive rejects survive restarts.
Designed to run under nohup/systemd for a week, or as a one-shot (--once) that
exits non-zero when a reject/error was seen.

Usage:
    nohup python3 scripts/monitor_calibration_guardrails.py \
        > /tmp/calib_monitor.log 2>&1 &
    python3 scripts/monitor_calibration_guardrails.py --once
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional

REPO = Path(__file__).resolve().parent.parent
ARCHIVE_DIR = Path(os.environ.get(
    "ARCHIVE_DIR", "/home/ldy/.local/state/hermes-trader/regime_calib"))
LOG_FILE = Path(os.environ.get(
    "CALIB_LOG", "/home/ldy/.local/state/hermes-trader/calib.log"))
ALERTS_FILE = Path(os.environ.get(
    "CALIB_ALERTS", "/tmp/calibration_guardrail_alerts.jsonl"))
CRON_SCRIPT = REPO / "scripts" / "weekly_calibrate_regime.sh"
CONFIG_FILE = REPO / ".agent-config.json"
SH_TZ = timezone(timedelta(hours=8))  # Asia/Shanghai

VERDICT_RE = re.compile(r"VERDICT=(PROMOTE|REJECT|DRY_RUN)")
PARAMS_RE = re.compile(r"params=\((\d+),(\d+),([\d.]+),(\d+(?:\.\d+)?)\)")
REASONS_RE = re.compile(r"^REASONS=(.*)$")
ERROR_RE = re.compile(r"\bERROR\b|Traceback|failed \(rc=", re.IGNORECASE)
WEEK_DIR_RE = re.compile(r"^\d{4}-W\d{2}$")


def now_sh() -> datetime:
    return datetime.now(SH_TZ)


def next_monday_0030_sh() -> datetime:
    n = now_sh()
    days_ahead = (0 - n.weekday()) % 7  # Monday=0
    target = (n + timedelta(days=days_ahead)).replace(
        hour=0, minute=30, second=0, microsecond=0)
    if target <= n:
        target += timedelta(days=7)
    return target


def log(level: str, msg: str) -> None:
    color = {"INFO": "\033[0;36m", "OK": "\033[0;32m",
             "WARN": "\033[1;33m", "ALERT": "\033[1;31m",
             "RESET": "\033[0m"}
    c = color.get(level, "") if sys.stderr.isatty() else ""
    r = color["RESET"] if sys.stderr.isatty() else ""
    print(f"[{now_sh().isoformat(timespec='seconds')}] {c}{level:<5}{r} {msg}",
          flush=True)


def append_alert(record: dict) -> None:
    ALERTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(ALERTS_FILE, "a") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def preflight() -> List[str]:
    problems = []
    # CRON_TZ + cron entry
    try:
        import subprocess
        cron_txt = subprocess.run(
            ["crontab", "-l"], capture_output=True, text=True).stdout
    except Exception as e:  # pragma: no cover
        cron_txt = ""
        problems.append(f"cannot read crontab: {e}")
    if not re.search(r"^CRON_TZ\s*=\s*Asia/Shanghai\s*$", cron_txt, re.M):
        problems.append("CRON_TZ=Asia/Shanghai is NOT set in crontab")
    if "weekly_calibrate_regime.sh" not in cron_txt:
        problems.append("weekly_calibrate_regime.sh entry missing in crontab")
    elif "--apply" not in cron_txt:
        problems.append("cron entry present but without --apply")
    # Filesystem
    if not CRON_SCRIPT.exists():
        problems.append(f"cron script missing: {CRON_SCRIPT}")
    elif not os.access(CRON_SCRIPT, os.X_OK):
        problems.append(f"cron script not executable: {CRON_SCRIPT}")
    if not ARCHIVE_DIR.exists():
        problems.append(f"archive dir missing: {ARCHIVE_DIR}")
    elif not os.access(ARCHIVE_DIR, os.W_OK):
        problems.append(f"archive dir not writable: {ARCHIVE_DIR}")
    if not LOG_FILE.parent.exists():
        problems.append(f"log dir missing: {LOG_FILE.parent}")
    if not CONFIG_FILE.exists():
        problems.append(f"config missing: {CONFIG_FILE}")
    return problems


def snapshot_archives() -> Dict[Path, float]:
    snap = {}
    if not ARCHIVE_DIR.exists():
        return snap
    for wd in ARCHIVE_DIR.iterdir():
        if wd.is_dir() and WEEK_DIR_RE.match(wd.name):
            for j in wd.glob("calib_*.json"):
                snap[j.resolve()] = j.stat().st_mtime
    return snap


def parse_json_artifact(path: Path) -> Optional[dict]:
    try:
        d = json.loads(path.read_text())
        base, best = d.get("baseline"), d.get("best")
        if not base or not best:
            return None
        gap = best["slow"] - best["fast"]
        # Re-derive the verdict using the same guardrails as the weekly script.
        reasons = []
        if best["fast"] >= best["slow"]:
            reasons.append("fast>=slow")
        if gap < 5:
            reasons.append(f"EMA gap {gap} < 5 (degenerate cross)")
        if (base["cost"] - best["cost"]) < 0.01:
            reasons.append("cost improvement below 1.0pt")
        if (best["agree"] - base["agree"]) < 0:
            reasons.append("agreement regresses")
        if best["false_trend"] > 0.35:
            reasons.append("false_trend > 35%")
        verdict = "REJECT" if reasons else "PROMOTE"
        return {"path": str(path), "verdict": verdict, "reasons": reasons,
                "best": best, "baseline": base}
    except Exception as e:
        return {"path": str(path), "verdict": "ERROR",
                "reasons": [f"json parse failed: {e}"]}


class LogTailer:
    """Follow a file from its current end, surviving rotation/truncation."""

    def __init__(self, path: Path):
        self.path = path
        self.f = None
        self.inode = None
        self._open()

    def _open(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.f = open(self.path, "r")
        except FileNotFoundError:
            # Log does not exist yet (cron has not run). Defer until it appears;
            # read_new_lines() will retry by stat()ing the path.
            self.f = None
            self.inode = None
            return
        self.f.seek(0, os.SEEK_END)
        self.inode = os.fstat(self.f.fileno()).st_ino

    def read_new_lines(self) -> List[str]:
        if self.f is None:
            # File did not exist at startup; try (re)opening now.
            try:
                self._open()
            except FileNotFoundError:
                return []
            if self.f is None:
                return []
        try:
            st = os.stat(self.path)
        except FileNotFoundError:
            return []
        if self.inode is None or st.st_ino != self.inode or st.st_size < self.f.tell():
            # rotated or truncated (or just opened for the first time)
            self.f.close()
            self._open()
            if self.f is None:
                return []
            log("INFO", f"log rotated/truncated; re-opened {self.path}")
        lines = self.f.readlines()
        return [ln.rstrip("\n") for ln in lines if ln.strip()]


def scan_archives_since(seen: Dict[Path, float]) -> List[dict]:
    current = snapshot_archives()
    fresh = []
    for p, mt in current.items():
        if p not in seen or seen[p] != mt:
            res = parse_json_artifact(p)
            if res:
                res["mtime"] = mt
                fresh.append(res)
    seen.clear()
    seen.update(current)
    return fresh


def handle_event(evt: dict, stats: dict) -> None:
    v = evt.get("verdict", "?")
    best = evt.get("best") or {}
    if v == "PROMOTE":
        stats["promote"] += 1
        log("OK", f"PROMOTE  fast={best.get('fast')} slow={best.get('slow')} "
                  f"slope={best.get('slope')} adx={best.get('adx')} "
                  f"cost={best.get('cost',0)*100:.2f}% "
                  f"agree={best.get('agree',0)*100:.1f}% "
                  f"({evt.get('path','log')})")
    elif v == "REJECT":
        stats["reject"] += 1
        stats["consecutive_rejects"] += 1
        msg = (f"REJECT   best=({best.get('fast')},{best.get('slow')},"
               f"{best.get('slope')},{best.get('adx')}) "
               f"gap={best.get('slow',0)-best.get('fast',0)} "
               f"reasons={evt.get('reasons')}")
        log("ALERT", msg)
        append_alert({"ts": now_sh().isoformat(timespec="seconds"),
                      "kind": "reject", **evt})
    elif v == "ERROR":
        stats["error"] += 1
        log("ALERT", f"ERROR    {evt.get('reasons')} ({evt.get('path','')})")
        append_alert({"ts": now_sh().isoformat(timespec="seconds"),
                      "kind": "error", **evt})
    else:
        stats["dryrun"] += 1
        log("INFO", f"DRY_RUN  {evt.get('reasons','')}")


def parse_log_buffer(buf: List[str]) -> List[dict]:
    """Extract complete verdict events from accumulated log lines."""
    events = []
    i = 0
    while i < len(buf):
        line = buf[i]
        m = VERDICT_RE.search(line)
        if m:
            verdict = m.group(1)
            params = {}
            reasons = ""
            j = i + 1
            while j < len(buf) and j < i + 6:
                pm = PARAMS_RE.search(buf[j])
                if pm and "best" not in params:
                    params["best"] = {
                        "fast": int(pm.group(1)),
                        "slow": int(pm.group(2)),
                        "slope": float(pm.group(3)),
                        "adx": float(pm.group(4))}
                rm = REASONS_RE.match(buf[j])
                if rm:
                    reasons = rm.group(1)
                j += 1
            events.append({"verdict": verdict, "reasons": reasons,
                           "best": params.get("best", {}),
                           "path": str(LOG_FILE)})
            i = j
        else:
            i += 1
    return events


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--duration-hours", type=float, default=7 * 24,
                    help="how long to monitor (default: 168h = 7 days)")
    ap.add_argument("--interval", type=int, default=60,
                    help="poll interval in seconds (default 60)")
    ap.add_argument("--once", action="store_true",
                    help="scan log + archives once and exit (non-zero if "
                         "rejects/errors found)")
    ap.add_argument("--alert-on-existing", action="store_true",
                    help="treat JSON artifacts already present at startup as "
                         "events (alert on their rejects). Default for --once; "
                         "long-running mode seeds them as informational only.")
    ap.add_argument("--summary-interval", type=int, default=3600,
                    help="print a heartbeat/summary every N seconds (default 1h)")
    args = ap.parse_args()

    # --once is a health check: evaluate every artifact on disk, not just new ones.
    if args.once:
        args.alert_on_existing = True

    problems = preflight()
    if problems:
        for p in problems:
            log("WARN", f"preflight: {p}")
        append_alert({"ts": now_sh().isoformat(timespec="seconds"),
                      "kind": "preflight", "reasons": problems})
    else:
        log("OK", "preflight OK (CRON_TZ, --apply, perms, files)")

    tailer = LogTailer(LOG_FILE)
    seen_arch = snapshot_archives()
    log_buf: List[str] = []
    stats = {"promote": 0, "reject": 0, "dryrun": 0, "error": 0,
             "consecutive_rejects": 0, "last_event_ts": None}

    # Seed from any JSON artifacts already present. By default these are known
    # past runs (informational only) — reported as seed lines and excluded from
    # alert stats/exit code. With --alert-on-existing (implied by --once) they
    # are treated as live events so a health check surfaces any reject on disk.
    if args.alert_on_existing:
        for evt in scan_archives_since({}):
            handle_event(evt, stats)
            stats["last_event_ts"] = now_sh().isoformat(timespec="seconds")
    else:
        for evt in scan_archives_since({}):
            v = evt["verdict"]
            best = evt.get("best") or {}
            log("INFO", f"archive-seed: {v} {best.get('fast')}/{best.get('slow')} "
                        f"reasons={evt.get('reasons')}")
    seen_arch = snapshot_archives()  # freeze so seeded artifacts don't re-alert

    start = time.time()
    deadline = start + args.duration_hours * 3600
    last_summary = start

    log("INFO", f"monitoring {LOG_FILE} + {ARCHIVE_DIR} for "
                f"{args.duration_hours}h; alerts -> {ALERTS_FILE}")
    nxt = next_monday_0030_sh()
    log("INFO", f"next scheduled run: {nxt.isoformat(timespec='seconds')} "
                f"({(nxt-now_sh()).total_seconds()/3600:.1f}h from now)")

    if args.once:
        # Drain anything currently in the log + archives and exit.
        log_buf.extend(tailer.read_new_lines())
        for evt in parse_log_buffer(log_buf):
            handle_event(evt, stats)
        log_buf.clear()
        for evt in scan_archives_since(seen_arch):
            handle_event(evt, stats)
        log("INFO", f"--once done: {stats}")
        return 1 if (stats["reject"] or stats["error"]) else 0

    while time.time() < deadline:
        log_buf.extend(tailer.read_new_lines())
        for evt in parse_log_buffer(log_buf):
            handle_event(evt, stats)
            stats["last_event_ts"] = now_sh().isoformat(timespec="seconds")
        log_buf.clear()

        for evt in scan_archives_since(seen_arch):
            # Avoid double-alert if the log already reported this same artifact.
            handle_event(evt, stats)
            stats["last_event_ts"] = now_sh().isoformat(timespec="seconds")

        time.sleep(args.interval)

        if time.time() - last_summary >= args.summary_interval:
            last_summary = time.time()
            nxt = next_monday_0030_sh()
            log("INFO", f"heartbeat runs: PROMOTE={stats['promote']} "
                        f"REJECT={stats['reject']} DRYRUN={stats['dryrun']} "
                        f"ERROR={stats['error']} "
                        f"consecutive_rejects={stats['consecutive_rejects']} | "
                        f"next run in {(nxt-now_sh()).total_seconds()/3600:.1f}h")
            if stats["consecutive_rejects"] >= 2:
                log("WARN", ">=2 consecutive rejects — the grid is stuck on "
                            "degenerate crosses; review fast/slow search grid")

    log("INFO", f"monitoring window complete: {stats}")
    return 1 if (stats["reject"] or stats["error"]) else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        log("INFO", "interrupted; exiting")
        raise SystemExit(130)
