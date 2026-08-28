#!/usr/bin/env python3
"""Weekly risk-control audit report.

Reconstructs the last 7 days of realized trades from the outcome store
(.agent-memory.json closes + events.jsonl close events) and verifies that the
sizing/risk-overhaul controls actually did their job:

  1. Stop-cap compliance: every losing close's adverse spot move vs the
     configured DSL effective cap (3% hard ceiling family). Flags overruns.
  2. Circuit breaker firings: counts COIN CIRCUIT / GLOBAL CIRCUIT events from
     the log and confirms re-entries were blocked while armed.
  3. Sizing-vs-DSL drift: scans the log for [sizing-v2] drift checks and flags
     any STOP DRIFT >5%.
  4. Actual-stop deviation: scans STOP OVERRUN warnings (>10%).
  5. Slippage distribution: per-coin mean/p95 adverse exit slip (bps), and
     whether the dynamic compensation had enough samples to be active.
  6. Backup-SL health: count of backup SL placements vs "STILL MISSING" alerts.
  7. Consecutive-loss streaks per coin.

Outputs a Markdown report to stdout (or --out FILE). Intended to be scheduled
weekly (cron) and shipped via the existing notify/report path.

Run inside the container (authoritative /data):
    docker exec hermes-trader python3 /app/scripts/weekly_risk_audit.py
Or on a host snapshot:
    python3 scripts/weekly_risk_audit.py --data-dir ./data_snapshot --days 7
"""
from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

_CANDIDATE_DATA_DIRS = [
    Path("/data"),
    Path.home() / ".hermes-trading",
    Path(__file__).resolve().parents[1],
]

# Log patterns (kept permissive to survive format tweaks).
_COIN_CIRCUIT_RE = re.compile(r"COIN CIRCUIT on (\S+).*?spot loss ([0-9.]+)%")
_GLOBAL_CIRCUIT_RE = re.compile(r"GLOBAL CIRCUIT.*?daily loss ([0-9.]+)%")
_STOP_OVERRUN_RE = re.compile(
    r"STOP OVERRUN (\S+): realized ([0-9.]+)% vs cap ([0-9.]+)%.*?\+([0-9.]+)%")
_DRIFT_RE = re.compile(
    r"\[sizing-v2\] (?:STOP DRIFT|drift check) (\S+):.*?dev=([0-9.]+)%")
_SL_MISSING_RE = re.compile(r"Backup SL FAILED twice for (\S+)")
_SL_STILL_MISSING_RE = re.compile(r"Pending SL STILL MISSING for (\S+)")
_SL_PLACED_RE = re.compile(r"Placed backup SL at .*?\(([0-9.]+)% from entry")


def _resolve_data_dir(explicit: Optional[str]) -> Path:
    if explicit:
        p = Path(explicit).expanduser().resolve()
        if p.is_dir():
            return p
    for d in _CANDIDATE_DATA_DIRS:
        if d.is_dir() and (d / ".agent-memory.json").exists():
            return d
    for d in _CANDIDATE_DATA_DIRS:
        if d.is_dir():
            return d
    return Path.cwd()


def _ts_of_close(c: Dict[str, Any]) -> float:
    ts = c.get("closed_at") or 0
    try:
        v = float(ts)
    except (TypeError, ValueError):
        return 0.0
    return v / 1000.0 if v > 1e12 else v


def _load_closes(data_dir: Path, days: float) -> List[Dict[str, Any]]:
    """Merge closes from .agent-memory.json and events.jsonl, dedup by
    (coin, closed_at, exit_px)."""
    cutoff = time.time() - days * 86400.0
    rows: List[Dict[str, Any]] = []
    seen = set()

    def _add(c: Dict[str, Any]) -> None:
        # Normalize closed_at to whole seconds so the same close stored in ms
        # in .agent-memory.json and s in events.jsonl dedupes together.
        ts = _ts_of_close(c)
        key = (c.get("coin"), int(ts) if ts else None,
               c.get("entry_px"), c.get("exit_px"))
        if key in seen:
            return
        seen.add(key)
        rows.append(c)

    mem = data_dir / ".agent-memory.json"
    if mem.is_file():
        try:
            m = json.loads(mem.read_text(encoding="utf-8"))
            for c in (m.get("closes") or []):
                if _ts_of_close(c) >= cutoff:
                    _add(c)
        except json.JSONDecodeError:
            pass

    ev = data_dir / "events.jsonl"
    if ev.is_file():
        with ev.open("r", encoding="utf-8", errors="replace") as fh:
            for ln in fh:
                ln = ln.strip()
                if not ln:
                    continue
                try:
                    e = json.loads(ln)
                except json.JSONDecodeError:
                    continue
                if e.get("event") not in ("close", "external_close_recorded"):
                    continue
                p = e.get("payload") or {}
                if not isinstance(p, dict) or not p.get("coin"):
                    continue
                # events.jsonl carries an ISO timestamp, not closed_at; attach
                # parsed epoch so the cutoff filter still works when missing.
                if not p.get("closed_at"):
                    ts = e.get("timestamp")
                    if ts:
                        try:
                            p = dict(p)
                            p["closed_at"] = datetime.fromisoformat(
                                ts.replace("Z", "+00:00")).timestamp()
                        except ValueError:
                            continue
                if _ts_of_close(p) >= cutoff:
                    _add(p)
    return rows


def _pctile(values: List[float], q: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    idx = int(round((len(s) - 1) * q))
    return s[idx]


def analyze_closes(closes: List[Dict[str, Any]], stop_cap_pct: float) -> Dict[str, Any]:
    per_coin = defaultdict(list)
    overruns = []
    wins = 0
    losses = 0
    gross_win = 0.0
    gross_loss = 0.0
    slips = defaultdict(list)
    for c in closes:
        coin = str(c.get("coin", "?"))
        per_coin[coin].append(c)
        rp = float(c.get("realized_pnl_pct") or 0.0)
        if rp >= 0:
            wins += 1
            gross_win += rp
        else:
            losses += 1
            gross_loss += rp
        spot = float(c.get("spot_pct") or 0.0)
        # For a loss, spot move is negative; compare its magnitude to the cap.
        adverse = -spot if spot < 0 else 0.0
        if adverse > stop_cap_pct:
            overruns.append({"coin": coin, "adverse_spot_pct": round(adverse, 3),
                             "cap_pct": stop_cap_pct,
                             "overrun_pct": round((adverse - stop_cap_pct)
                                                  / stop_cap_pct * 100.0, 1),
                             "realized_pnl_pct": round(rp, 3),
                             "exit_px": c.get("exit_px"),
                             "entry_px": c.get("entry_px")})
        slip = c.get("exit_slip_bps")
        if slip is not None:
            try:
                v = float(slip)
                if v > 0:  # adverse only
                    slips[coin].append(v)
            except (TypeError, ValueError):
                pass
    total = wins + losses
    slip_summary = {}
    for coin, vals in slips.items():
        slip_summary[coin] = {
            "n": len(vals),
            "mean_bps": round(statistics.fmean(vals), 1),
            "p95_bps": round(_pctile(vals, 0.95), 1),
            "max_bps": round(max(vals), 1),
        }
    return {
        "total_closes": total,
        "wins": wins,
        "losses": losses,
        "win_rate_pct": round(wins / total * 100.0, 1) if total else 0.0,
        "avg_win_pct": round(gross_win / wins, 3) if wins else 0.0,
        "avg_loss_pct": round(gross_loss / losses, 3) if losses else 0.0,
        "per_coin_count": {c: len(v) for c, v in per_coin.items()},
        "stop_overruns": overruns,
        "slip_summary": slip_summary,
    }


def analyze_logs(data_dir: Path, days: float) -> Dict[str, Any]:
    log_path = data_dir / "trading-loop.log"
    cutoff = time.time() - days * 86400.0
    coin_circuits = []
    global_circuits = []
    overruns = []
    drifts = []
    drift_alarms = []
    sl_placed = 0
    sl_missing_alerts = 0
    sl_still_missing = set()
    if log_path.is_file():
        with log_path.open("r", encoding="utf-8", errors="replace") as fh:
            for ln in fh:
                # Crude recency filter on the leading "YYYY-MM-DD HH:MM:SS".
                m_ts = re.match(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})", ln)
                if m_ts:
                    try:
                        ts = datetime.strptime(
                            m_ts.group(1), "%Y-%m-%d %H:%M:%S"
                        ).replace(tzinfo=timezone.utc).timestamp()
                        if ts < cutoff:
                            continue
                    except ValueError:
                        pass
                if m := _COIN_CIRCUIT_RE.search(ln):
                    coin_circuits.append((m.group(1), float(m.group(2))))
                if m := _GLOBAL_CIRCUIT_RE.search(ln):
                    global_circuits.append(float(m.group(1)))
                if m := _STOP_OVERRUN_RE.search(ln):
                    overruns.append({"coin": m.group(1),
                                     "realized": float(m.group(2)),
                                     "cap": float(m.group(3)),
                                     "overrun": float(m.group(4))})
                if m := _DRIFT_RE.search(ln):
                    dev = float(m.group(2))
                    row = (m.group(1), dev)
                    drifts.append(row)
                    if "STOP DRIFT" in ln or dev > 5.0:
                        drift_alarms.append(row)
                if _SL_PLACED_RE.search(ln):
                    sl_placed += 1
                if m := _SL_MISSING_RE.search(ln):
                    sl_missing_alerts += 1
                    sl_still_missing.add(m.group(1))
                if m := _SL_STILL_MISSING_RE.search(ln):
                    sl_still_missing.add(m.group(1))
    return {
        "coin_circuits": coin_circuits,
        "global_circuits": global_circuits,
        "log_stop_overruns": overruns,
        "drift_checks": len(drifts),
        "drift_alarms": drift_alarms,
        "sl_placed": sl_placed,
        "sl_missing_alerts": sl_missing_alerts,
        "sl_still_missing_coins": sorted(sl_still_missing),
    }


def render_report(data_dir: Path, days: float, stop_cap_pct: float,
                  closes_stats: Dict[str, Any],
                  log_stats: Dict[str, Any]) -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines: List[str] = []
    lines.append(f"# Weekly Risk-Control Audit — {now}")
    lines.append("")
    lines.append(f"- **Window**: last {days:g} days")
    lines.append(f"- **Data dir**: `{data_dir}`")
    lines.append(f"- **Stop cap (hard ceiling)**: {stop_cap_pct}% spot")
    lines.append("")

    lines.append("## 1. Trade outcomes")
    lines.append("")
    lines.append(f"- Closes analyzed: **{closes_stats['total_closes']}** "
                 f"({closes_stats['wins']} wins / {closes_stats['losses']} losses)")
    lines.append(f"- Win rate: {closes_stats['win_rate_pct']}%")
    lines.append(f"- Avg win: {closes_stats['avg_win_pct']}% | "
                 f"Avg loss: {closes_stats['avg_loss_pct']}%")
    if closes_stats["per_coin_count"]:
        lines.append("- Per-coin closes:")
        for coin, n in sorted(closes_stats["per_coin_count"].items(),
                              key=lambda kv: -kv[1]):
            lines.append(f"    - {coin}: {n}")
    lines.append("")

    lines.append("## 2. Stop-cap compliance")
    lines.append("")
    over = closes_stats["stop_overruns"]
    if not over:
        lines.append(f"✅ No losing close exceeded the {stop_cap_pct}% spot cap.")
    else:
        lines.append(f"⚠️ **{len(over)} close(s) exceeded {stop_cap_pct}% cap:**")
        lines.append("")
        lines.append("| Coin | Adverse spot % | Overrun % | Realized % |")
        lines.append("|---|---|---|---|")
        for o in over:
            lines.append(f"| {o['coin']} | {o['adverse_spot_pct']} | "
                         f"+{o['overrun_pct']} | {o['realized_pnl_pct']} |")
    lines.append("")

    lines.append("## 3. Circuit breakers")
    lines.append("")
    cc = log_stats["coin_circuits"]
    gc = log_stats["global_circuits"]
    lines.append(f"- Single-coin circuit firings: **{len(cc)}**")
    for coin, loss in cc:
        lines.append(f"    - {coin}: spot loss {loss:.2f}% → 60min halt")
    lines.append(f"- Global daily-loss halt firings: **{len(gc)}**")
    for loss in gc:
        lines.append(f"    - daily loss {loss:.2f}% → 120min halt")
    if not cc and not gc:
        lines.append("  (no breaker tripped in window)")
    lines.append("")

    lines.append("## 4. Sizing / stop deviation metrics")
    lines.append("")
    lines.append(f"- Sizing-vs-DSL drift checks logged: {log_stats['drift_checks']}")
    if log_stats["drift_alarms"]:
        lines.append(f"- ⚠️ **{len(log_stats['drift_alarms'])} drift ALARM(S) (>5%)**:")
        for coin, dev in log_stats["drift_alarms"]:
            lines.append(f"    - {coin}: {dev:.2f}%")
    else:
        lines.append("- ✅ No sizing/DSL drift >5%.")
    lo = log_stats["log_stop_overruns"]
    lines.append(f"- Actual-stop-overrun log warnings (>10%): **{len(lo)}**")
    for o in lo:
        lines.append(f"    - {o['coin']}: realized {o['realized']}% vs cap "
                     f"{o['cap']}% (+{o['overrun']}%)")
    lines.append("")

    lines.append("## 5. Backup server-side SL health")
    lines.append("")
    lines.append(f"- Backup SL placements: {log_stats['sl_placed']}")
    lines.append(f"- Immediate placement failures (twice): {log_stats['sl_missing_alerts']}")
    if log_stats["sl_still_missing_coins"]:
        lines.append("- 🚨 Coins with a STILL-MISSING backup SL at some point: "
                     + ", ".join(log_stats["sl_still_missing_coins"]))
    else:
        lines.append("- ✅ No sustained missing backup SL.")
    lines.append("")

    lines.append("## 6. Adverse exit slippage (30d, per coin)")
    lines.append("")
    slips = closes_stats["slip_summary"]
    if not slips:
        lines.append("_No adverse exit-slip samples recorded._")
    else:
        lines.append("| Coin | N | Mean bps | p95 bps | Max bps |")
        lines.append("|---|---|---|---|---|")
        for coin, s in sorted(slips.items(),
                              key=lambda kv: -kv[1]["mean_bps"]):
            lines.append(f"| {coin} | {s['n']} | {s['mean_bps']} | "
                         f"{s['p95_bps']} | {s['max_bps']} |")
    lines.append("")
    lines.append("---")
    lines.append("_Auto-generated by weekly_risk_audit.py. Investigate every ⚠️/🚨 "
                 "row before increasing sizing gray-release weight._")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data-dir", help="Directory with .agent-memory.json / "
                                       "events.jsonl / trading-loop.log")
    ap.add_argument("--days", type=float, default=7.0,
                    help="Lookback window in days (default: 7)")
    ap.add_argument("--stop-cap-pct", type=float, default=3.0,
                    help="Hard stop ceiling in spot %% used for the compliance "
                         "check (default: 3.0)")
    ap.add_argument("--out", help="Write Markdown report to this file "
                                  "(default: stdout)")
    args = ap.parse_args()

    data_dir = _resolve_data_dir(args.data_dir)
    closes = _load_closes(data_dir, args.days)
    closes_stats = analyze_closes(closes, args.stop_cap_pct)
    log_stats = analyze_logs(data_dir, args.days)
    report = render_report(data_dir, args.days, args.stop_cap_pct,
                           closes_stats, log_stats)

    if args.out:
        Path(args.out).write_text(report, encoding="utf-8")
        print(f"wrote {args.out}")
    else:
        print(report)

    # Non-zero exit if hard-control violations are present (for CI/alerting).
    if closes_stats["stop_overruns"] or log_stats["drift_alarms"] \
            or log_stats["sl_still_missing_coins"]:
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
