#!/usr/bin/env python3
"""Analyze production DSL exit telemetry per regime.

Consumes the per-exit telemetry emitted by trading_loop.py and reports, for the
TREND regimes (production regime "up"/"down", i.e. trend-ride exits), the
average realized profit and holding time of trailing_stop triggers — plus MFE
and the capture ratio — to judge whether the trailing stop is cutting trend
winners short (too conservative).

Two input formats are auto-detected per file/line:

  1. JSON session log  (~/.hermes-trader-session-log.jsonl, event="dsl_exit")
       Preferred. Carries realized_pnl_pct when the exchange fill parsed.
  2. Text log lines    ("[dsl:exit_stats] coin=... regime=up reason=trailing_stop ...")
       Emitted on every DSL close; parsed when piped in or pointed at a log file.

Production only records a 4-state regime (up/down/neutral/chop), so it cannot
split TREND from STRONG_TREND the way the backtest labels do. "up"/"down" here
corresponds to the UNION of backtest TREND + STRONG_TREND (both get trend-ride
exit params). For the TREND-vs-STRONG_TREND split run the backtest audit:

    python3 scripts/audit_trailing_stop.py --days 30 --coins 20

Usage:
    python3 scripts/analyze_exit_stats.py
    python3 scripts/analyze_exit_stats.py --days 7
    python3 scripts/analyze_exit_stats.py path/to/trader.log another.log
    grep '\\[dsl:exit_stats\\]' /data/trader.log | python3 scripts/analyze_exit_stats.py -
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import statistics
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Optional

# Production regime values that map onto the backtest TREND bucket.
TREND_REGIMES = {"up", "down"}

# [dsl:exit_stats] coin=BTC side=long lev=12 regime=up reason=trailing_stop \
#   hold_min=42.3 mfe_spot_pct=+3.10 exit_spot_pct=+1.05 exit_margin_pct=+12.60
_KV_RE = re.compile(r"(\w+)=(\S+)")


def _to_float(v: str) -> Optional[float]:
    try:
        return float(v.replace("%", "").replace("+", ""))
    except (TypeError, ValueError):
        return None


def _parse_text_line(line: str) -> Optional[Dict]:
    if "[dsl:exit_stats]" not in line:
        return None
    fields = dict(_KV_RE.findall(line.split("[dsl:exit_stats]", 1)[1]))
    rec = {
        "coin": fields.get("coin", ""),
        "side": fields.get("side", ""),
        "leverage": int(fields.get("lev", 1) or 1),
        "regime": (fields.get("regime") or "unknown").lower(),
        "reason": (fields.get("reason") or "").lower(),
        "hold_min": _to_float(fields.get("hold_min", "")),
        "mfe_spot_pct": _to_float(fields.get("mfe_spot_pct", "")),
        "exit_spot_pct": _to_float(fields.get("exit_spot_pct", "")),
        "exit_margin_pct": _to_float(fields.get("exit_margin_pct", "")),
        # Text line has no authoritative realized PnL; the mark-time spot % is
        # the best proxy (gross of the close fill / fees).
        "realized_spot_pct": _to_float(fields.get("exit_spot_pct", "")),
        "source": "text",
    }
    return rec if rec["reason"] else None


def _parse_json_event(obj: dict) -> Optional[Dict]:
    if obj.get("event") != "dsl_exit":
        return None
    # JSON events carry mfe_spot_pct / hold_min / entry_regime only after the
    # telemetry instrumentation landed; older records lack them.
    regime = str(obj.get("entry_regime") or "unknown").lower()
    reason = str(obj.get("exit_reason") or obj.get("reason") or "").lower()
    # exit_reason is canonical; raw reason may be "floor_breach (...)".
    if reason.startswith("floor_breach"):
        reason = "trailing_stop"
    elif reason.startswith("max_loss"):
        reason = "max_loss"
    realized = obj.get("realized_spot_pct")
    if realized is None:
        realized = obj.get("unrealized_pct")  # pre-fill proxy
    rec = {
        "coin": obj.get("coin", ""),
        "side": obj.get("side", ""),
        "leverage": int(obj.get("leverage", 1) or 1),
        "regime": regime,
        "reason": reason,
        "hold_min": _to_float(str(obj.get("hold_min", ""))) if obj.get("hold_min") is not None else None,
        "mfe_spot_pct": obj.get("mfe_spot_pct"),
        "exit_spot_pct": obj.get("unrealized_pct"),
        "exit_margin_pct": obj.get("leveraged_pct"),
        "realized_spot_pct": realized,
        "executed": obj.get("executed"),
        "source": "json",
    }
    return rec if reason else None


def _pctile(xs: List[float], p: float) -> float:
    if not xs:
        return 0.0
    s = sorted(xs)
    k = (len(s) - 1) * p
    f = int(k)
    c = min(f + 1, len(s) - 1)
    return s[f] + (s[c] - s[f]) * (k - f)


def _summarize(values: List[float], label: str, fmt: str = "{:+.2f}%") -> None:
    if not values:
        print(f"    {label:<26}: n=0")
        return
    print(f"    {label:<26}: n={len(values):>4}  "
          f"mean={fmt.format(statistics.mean(values))}  "
          f"median={fmt.format(statistics.median(values))}  "
          f"p25={fmt.format(_pctile(values, 0.25))}  "
          f"p75={fmt.format(_pctile(values, 0.75))}")


def _iter_records(paths: List[str], since_ts: float) -> List[Dict]:
    records: List[Dict] = []

    def _consider(rec: Optional[Dict]) -> None:
        if rec and rec.get("hold_min") is not None:
            records.append(rec)

    for path in paths:
        if path == "-":
            for line in sys.stdin:
                line = line.strip()
                if not line:
                    continue
                if line.startswith("{"):
                    try:
                        _consider(_parse_json_event(json.loads(line)))
                    except json.JSONDecodeError:
                        continue
                else:
                    _consider(_parse_text_line(line))
            continue
        p = Path(path)
        if not p.is_file():
            print(f"  (skip, not a file: {path})", file=sys.stderr)
            continue
        with p.open("r", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                # JSONL lines are objects; text logs may have a timestamped
                # prefix before the JSON, so only treat a line that STARTS with
                # '{' as pure JSON.
                if line.startswith("{"):
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        _consider(_parse_text_line(line))
                        continue
                    # Session-log timestamps are epoch ms; filter by --days.
                    ts = obj.get("ts") or obj.get("timestamp") or obj.get("time")
                    if isinstance(ts, (int, float)) and ts > 1e12:
                        if ts / 1000.0 < since_ts:
                            continue
                    _consider(_parse_json_event(obj))
                else:
                    _consider(_parse_text_line(line))
    return records


def _default_log_paths() -> List[str]:
    """Candidate production log locations (most likely first)."""
    cands = [
        os.path.expanduser("~/.hermes-trader-session-log.jsonl"),
        "/data/session-log.jsonl",
    ]
    cands += sorted(glob.glob(os.path.expanduser("~/hermes-trader/logs/*.log")))
    cands += sorted(glob.glob(os.path.expanduser("~/hermes-trader/logs/*.jsonl")))
    cands += sorted(glob.glob("logs/*.log")) + sorted(glob.glob("logs/*.jsonl"))
    # De-dup while preserving order, keep only files that exist.
    seen, out = set(), []
    for c in cands:
        if c not in seen and os.path.isfile(c):
            seen.add(c)
            out.append(c)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("paths", nargs="*",
                    help="Log files (.log/.jsonl) or '-' for stdin. "
                         "Defaults to the production session log if present.")
    ap.add_argument("--days", type=float, default=None,
                    help="Only include JSON events newer than N days "
                         "(text log lines have no timestamp and are always included).")
    args = ap.parse_args()

    paths = args.paths or _default_log_paths()
    if not paths:
        print("No log files found. Pass a path or pipe '[dsl:exit_stats]' lines "
              "on stdin with '-'.", file=sys.stderr)
        return 2

    since_ts = 0.0
    if args.days:
        since_ts = time.time() - args.days * 86400

    print(f"Reading {len(paths)} log source(s):")
    for p in paths:
        print(f"  - {p}")
    records = _iter_records(paths, since_ts)
    if not records:
        print("\nNo exit telemetry records found. The instrumentation emits "
              "[dsl:exit_stats] lines on every DSL close; make sure the trader "
              "has run since it was deployed.")
        return 1

    print(f"\nParsed {len(records)} exit records "
          f"(json={sum(1 for r in records if r['source']=='json')}, "
          f"text={sum(1 for r in records if r['source']=='text')}).")

    # ------------------------------------------------------------------
    # Exit-reason x regime breakdown
    # ------------------------------------------------------------------
    by: Dict[str, Dict[str, List[Dict]]] = defaultdict(lambda: defaultdict(list))
    for r in records:
        by[r["regime"]][r["reason"]].append(r)

    print("\n" + "=" * 84)
    print("  EXIT REASON x REGIME")
    print("=" * 84)
    for regime in ("up", "down", "neutral", "chop", "unknown"):
        reasons = by.get(regime)
        if not reasons:
            continue
        total = sum(len(v) for v in reasons.values())
        print(f"\n  regime={regime} ({total} exits):")
        for reason, lst in sorted(reasons.items(), key=lambda kv: -len(kv[1])):
            wins = [r for r in lst if (r.get("realized_spot_pct") or 0) > 0]
            avg_h = statistics.mean([r["hold_min"] for r in lst
                                     if r.get("hold_min") is not None])
            print(f"    {reason:<18}: n={len(lst):>4}  "
                  f"win_rate={len(wins)/len(lst)*100:5.1f}%  "
                  f"avg_hold={avg_h:6.1f}m")

    # ------------------------------------------------------------------
    # Trailing-stop focus for TREND regimes (up/down)
    # ------------------------------------------------------------------
    ts_trend = [r for r in records
                if r["regime"] in TREND_REGIMES and r["reason"] == "trailing_stop"]
    ts_nontrend = [r for r in records
                   if r["regime"] not in TREND_REGIMES
                   and r["reason"] == "trailing_stop"]

    print("\n" + "=" * 84)
    print("  TRAILING_STOP IN TREND (regime=up/down) — is the stop too conservative?")
    print("=" * 84)
    if not ts_trend:
        print("\n  No trailing_stop exits recorded in up/down regimes.")
        print("  (Need the trader to run with the exit_stats instrumentation live.)")
    else:
        holds = [r["hold_min"] for r in ts_trend if r.get("hold_min") is not None]
        mfes = [r["mfe_spot_pct"] for r in ts_trend if r.get("mfe_spot_pct") is not None]
        realized = [r["realized_spot_pct"] for r in ts_trend
                    if r.get("realized_spot_pct") is not None]
        margin = [r["exit_margin_pct"] for r in ts_trend
                  if r.get("exit_margin_pct") is not None]
        # giveback = MFE - realized (both spot %, directional for longs/shorts:
        # both are measured favorable-to-exit so a positive giveback means the
        # trade gave back peak profit).
        giveback = [r["mfe_spot_pct"] - r["realized_spot_pct"]
                    for r in ts_trend
                    if r.get("mfe_spot_pct") is not None
                    and r.get("realized_spot_pct") is not None]

        print(f"\n  Sample: {len(ts_trend)} trailing_stop exits in trend regimes "
              f"(of {sum(len(by[reg]) for reg in TREND_REGIMES)} total trend exits).")
        print("\n  --- Profit / excursion (spot %) ---")
        _summarize(realized, "Realized at exit")
        _summarize(mfes, "MFE (peak favorable)")
        _summarize(giveback, "Peak-to-exit giveback")
        if margin:
            _summarize(margin, "Exit margin (leveraged)")

        print("\n  --- Holding time ---")
        _summarize(holds, "Hold minutes", fmt="{:.1f}")

        print("\n  --- Comparison: trend vs non-trend trailing_stop ---")
        for label, lst in [("trend (up/down)", ts_trend),
                           ("non-trend", ts_nontrend)]:
            if not lst:
                continue
            h = [r["hold_min"] for r in lst if r.get("hold_min") is not None]
            m = [r["mfe_spot_pct"] for r in lst if r.get("mfe_spot_pct") is not None]
            rl = [r["realized_spot_pct"] for r in lst
                  if r.get("realized_spot_pct") is not None]
            wr = sum(1 for r in lst if (r.get("realized_spot_pct") or 0) > 0) / len(lst)
            print(f"    {label:<18}: n={len(lst):>4}  WR={wr*100:5.1f}%  "
                  f"avg_hold={statistics.mean(h):6.1f}m  "
                  f"avg_MFE={statistics.mean(m):+5.2f}%  "
                  f"avg_real={statistics.mean(rl):+5.2f}%")

        # ------------------------------------------------------------------
        # Verdict
        # ------------------------------------------------------------------
        avg_mfe = statistics.mean(mfes) if mfes else 0.0
        avg_real = statistics.mean(realized) if realized else 0.0
        avg_hold = statistics.mean(holds) if holds else 0.0
        capture = avg_real / avg_mfe * 100 if avg_mfe > 0 else 0.0
        print(f"\n  {'-'*78}")
        print(f"  CAPTURE RATIO (realized/MFE): {capture:.1f}%  "
              f"(avg MFE {avg_mfe:.2f}% -> realized {avg_real:.2f}%)")
        print(f"  AVG HOLD: {avg_hold:.1f} min")
        if capture < 40 and avg_hold < 120:
            verdict = ("TOO CONSERVATIVE: captures only "
                       f"{capture:.0f}% of peak profit with short "
                       f"{avg_hold:.0f}min holds — winners are being cut. "
                       "Consider raising trend_ride.retrace_threshold/protect_pct.")
        elif capture < 40:
            verdict = ("GIVEBACK HIGH: captures "
                       f"{capture:.0f}% of MFE but holds are long — the trail "
                       "is too LOOSE late in the move.")
        elif capture < 60:
            verdict = (f"MODERATE: captures {capture:.0f}% of peak move. "
                       "Tunable but not clearly too tight.")
        else:
            verdict = (f"HEALTHY: captures {capture:.0f}% of peak move.")
        print(f"  VERDICT: {verdict}")
        print(f"  {'-'*78}")
        print("\n  NOTE: production 4-state regime merges TREND+STRONG_TREND into "
              "'up'/'down'. For the per-label split, run "
              "scripts/audit_trailing_stop.py.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
