#!/usr/bin/env python3
"""Reconcile stop_confirm SHADOW verdicts (audit 2026-10-11).

Reads ~/.hermes-trading/stop_confirm_shadow.jsonl (override via
HERMES_STOP_CONFIRM_SHADOW_FILE). Each row is recorded at a LIVE floor_breach
exit and compares the live confirmation gate (2 breaches / 4s / index) with a
tighter candidate gate (default 1 breach / 1.0s, index still required).

This is a self-contained distribution summary — both instants and prices are
captured at exit time, so no candle fetch is needed to answer the decision
question: how much earlier would the candidate have exited and how much of the
adverse excursion would it have avoided?

Pure paper reconciliation: never places orders and never writes config.

Usage:
    python3 scripts/reconcile_stop_confirm_shadow.py
    python3 scripts/reconcile_stop_confirm_shadow.py --window-hours 24
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
from datetime import datetime, timezone
from pathlib import Path

# U8 (2026-10-23): prefer /data unconditionally (the writer uses /data inside
# the container); only fall back to the host path when /data is absent, same
# as reconcile_pullback_shadow.py.
SHADOW_FILE = os.environ.get(
    "HERMES_STOP_CONFIRM_SHADOW_FILE", "/data/stop_confirm_shadow.jsonl")
if not os.environ.get("HERMES_STOP_CONFIRM_SHADOW_FILE") and not os.path.isfile(SHADOW_FILE):
    _host = os.path.expanduser("~/.hermes-trading/stop_confirm_shadow.jsonl")
    if os.path.isfile(_host):
        print(f"[reconcile] WARNING: {SHADOW_FILE} not found, falling back to {_host}")
        SHADOW_FILE = _host


def _parse_ts(s: str) -> float:
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=timezone.utc).timestamp()


def _quantiles(xs):
    if not xs:
        return {}
    s = sorted(xs)
    n = len(s)
    return {"p50": s[n // 2], "p90": s[min(n - 1, int(n * 0.9))],
            "max": s[-1]}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--window-hours", type=float, default=None)
    ap.add_argument("--file", default=SHADOW_FILE)
    args = ap.parse_args(argv)

    p = Path(args.file)
    if not p.exists():
        print(f"no shadow file at {p}")
        return 0

    cutoff = None
    if args.window_hours is not None:
        cutoff = datetime.now(timezone.utc).timestamp() - args.window_hours * 3600

    dwell, overshoot, live_exc, cand_exc = [], [], [], []
    earlier = same = 0
    by_side = {"long": [0, 0.0], "short": [0, 0.0]}
    n = 0
    with p.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if r.get("rule") != "stop_confirm":
                continue
            if cutoff is not None and _parse_ts(r["timestamp"]) < cutoff:
                continue
            d = r.get("detail", {})
            n += 1
            if r.get("would") == "earlier_exit":
                earlier += 1
            else:
                same += 1
            dwell.append(float(d.get("confirmation_dwell_delta_sec", 0.0)))
            overshoot.append(float(d.get("overshoot_delta_spot_pct", 0.0)))
            live_exc.append(float(d.get("live_exit_excursion_spot_pct", 0.0)))
            cand_exc.append(float(d.get("candidate_excursion_spot_pct", 0.0)))
            side = r.get("side", "long")
            if side in by_side:
                by_side[side][0] += 1
                by_side[side][1] += float(d.get("overshoot_delta_spot_pct", 0.0))

    print(f"stop_confirm shadow rows: {n} ({p.name})")
    if not n:
        return 0
    print(f"  would exit earlier: {earlier} ({earlier/n*100:.0f}%)   same: {same}")

    def _show(label, xs, unit, better=None):
        q = _quantiles(xs)
        mean = statistics.mean(xs)
        extra = ""
        if better == "negative":
            extra = f"  (candidate avoids {mean:+.3f}{unit}/trade)"
        print(f"  {label:34s} mean={mean:+.3f}{unit} p50={q['p50']:+.3f} "
              f"p90={q['p90']:+.3f} max={q['max']:+.3f}{extra}")

    _show("confirmation dwell saved (s)", dwell, "s")
    _show("overshoot delta (spot pp)", overshoot, "pp", better="negative")
    _show("live exit excursion (spot %)", live_exc, "%")
    _show("candidate excursion (spot %)", cand_exc, "%")
    for side, (cnt, tot) in by_side.items():
        if cnt:
            print(f"  {side:5s} n={cnt} mean overshoot delta={tot/cnt:+.3f}pp")
    print("\nverdict rule: a tighter stop-confirmation is worth promoting only "
          "if the overshoot avoided is persistently negative (less adverse "
          "fill) without a rise in noise-stop churn (cross-check with the "
          "live floor_breach false-positive rate).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
