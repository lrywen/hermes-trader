#!/usr/bin/env python3
"""Reconcile relax_tier SHADOW probe verdicts with forward returns.

The relax_tier probe (ta_filter.relax_tier_check, mounted in
risk_gates.ta_late_entry_gate) records three stricter, trend-strength-tiered
counterfactuals on EVERY order-time ta_late evaluation, observation only:

  * rt_relax45_would_block  — pass admitted solely because the ADX>=35 relax
                              band switched on in the weak 35-45 zone (a 45
                              floor + STRICT limits would have vetoed);
  * rt_weak_rsi70_would_block — weak trend ADX<35 with long RSI>=70 / short
                              RSI<=30;
  * rt_no_adx20_would_block — no measurable trend at all (ADX<20).

The motivating read-only replays (16 real fills + a 12,429-row graded sample)
scored these on FIXED forward windows, with the discriminating horizon at 72h.
This grader preserves that geometry: for each PASSED ta_late decision
(blocked=False) that carries at least one rt_* verdict, it fetches 4h candles
after the signal and records the side-aware forward move (net of round-trip
fees) at 6h / 24h / 72h, then buckets flagged vs not-flagged for each arm so
the real selection of each hypothetical veto can be measured BEFORE any
enforcement. A veto is "good" when its flagged cells lose and its spared cells
win.

Only passed trades are counterfactual entries here; live-blocked records are
handled by reconcile_ta_late_entry_shadow.py and are skipped. The grader reads
the live file AND its size-rotated siblings (.1/.2/...), skips records already
graded (unless --force), and is a dry run unless --write. It never places an
order and never touches a live decision; market access is read-only.

Usage:
    python3 scripts/reconcile_relax_tier_shadow.py
    python3 scripts/reconcile_relax_tier_shadow.py --write
    python3 scripts/reconcile_relax_tier_shadow.py --file /data/ta_late_entry_shadow.jsonl
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

os.environ.setdefault("HERMES_BACKTEST", "1")
_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO))

from hermes_trader.client.hl_client import _http_post

DEFAULT_FILE = "/data/ta_late_entry_shadow.jsonl"
FILE = os.environ.get("HERMES_TA_LATE_ENTRY_SHADOW_FILE", DEFAULT_FILE)
# Side-aware forward checkpoints, in hours (4h bars => 6h is ~bar 2).
FWD_HOURS = (6, 24, 72)
PRIMARY_HOURS = 72
# A primary (72h) forward return must exist to count as mature.
MIN_BARS_AFTER = 24
# Round-trip taker fees, bps (matches the other reconcile arms).
ROUND_TRIP_FEE_BPS = 5.0

# rt arm key -> (would_block field, human label).
ARMS = (
    ("rt_relax45", "rt_relax45_would_block", "relax 35->45 floor"),
    ("rt_weak_rsi70", "rt_weak_rsi70_would_block", "weak ADX<35 high RSI"),
    ("rt_no_adx20", "rt_no_adx20_would_block", "no-trend ADX<20"),
)
GRADED_FLAG = "rt_graded"
MATURE_OUTCOMES = ("win", "loss")


def _signal_ms(rec: dict[str, Any]) -> Optional[int]:
    ts = rec.get("timestamp")
    if isinstance(ts, (int, float)):
        v = int(ts)
        return v if v > 10_000_000_000 else v * 1000
    if isinstance(ts, str):
        try:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            return int(dt.timestamp() * 1000)
        except ValueError:
            return None
    return None


def _forward_closes(coin: str, start_ms: int, hours: int) -> Optional[list[float]]:
    """4h closes from one bar before the signal through start+hours.

    Index 0 is the 4h bar whose open is at/just before t0 (B0, forming at
    signal); the n-hours forward close lives at index n//4 + 1.
    """
    bar_ms = 4 * 3600_000
    end = start_ms + (hours + 8) * 3600_000
    payload = {"type": "candleSnapshot", "req": {
        "coin": coin, "interval": "4h",
        "startTime": start_ms - bar_ms, "endTime": end}}
    raw = _http_post("/info", payload)
    if not isinstance(raw, list) or not raw:
        return None
    rows = sorted(raw, key=lambda c: int(c["t"]))
    return [float(c["c"]) for c in rows]


def grade(rec: dict[str, Any]) -> str:
    """Return outcome bucket; mutate rec with the rt_forward grid on success."""
    coin = rec.get("coin")
    if not coin:
        return "no_coin"
    side = rec.get("side") if rec.get("side") in ("long", "short") else "long"
    try:
        entry = float(rec.get("entry_px") or 0)
    except (TypeError, ValueError):
        return "no_entry_px"
    if entry <= 0:
        return "no_entry_px"
    t0 = _signal_ms(rec)
    if not t0:
        return "bad_timestamp"

    closes = _forward_closes(coin, t0, max(FWD_HOURS))
    if not closes:
        return "no_future_bars"

    sign = 1.0 if side == "long" else -1.0
    fee_pct = ROUND_TRIP_FEE_BPS / 100.0
    grid: dict[str, Any] = {}
    # closes[0] = B0 (forming at signal); n-hours close = index n//4 + 1.
    for h in FWD_HOURS:
        idx = h // 4 + 1
        if idx >= len(closes):
            grid[f"fwd{h}h_pct"] = None
            continue
        px = closes[idx]
        net = sign * (px - entry) / entry * 100.0 - fee_pct
        grid[f"fwd{h}h_pct"] = round(net, 4)
        grid[f"fwd{h}h_px"] = px
    # Worst adverse side-aware close across the whole matured window.
    grid["mae_pct"] = round(
        min(sign * (c - entry) / entry * 100.0 - fee_pct for c in closes), 4)

    rec["rt_forward"] = grid
    primary = grid.get(f"fwd{PRIMARY_HOURS}h_pct")
    if primary is None:
        return "immature"
    rec["rt_pnl_pct"] = primary
    return "win" if primary > 0 else "loss"


def _iter_files(primary: str) -> list[str]:
    files = sorted(glob.glob(primary + ".*"), reverse=True)
    if os.path.exists(primary):
        files.append(primary)
    seen, out = set(), []
    for f in files:
        if f not in seen:
            seen.add(f)
            out.append(f)
    return out


def _eligible(r: dict[str, Any]) -> bool:
    """Passed order-time decision carrying at least one rt verdict."""
    if r.get("layer", "gate") != "gate" or r.get("blocked") is not False:
        return False
    return any(r.get(fld) is not None for _, fld, _ in ARMS)


def _new_tally() -> dict[bool, list[float]]:
    # flagged-state -> list of primary forward pnl
    return {True: [], False: []}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--file", default=FILE)
    ap.add_argument("--write", action="store_true",
                    help="persist rt outcomes into each JSONL (default dry-run)")
    ap.add_argument("--force", action="store_true",
                    help="re-grade records already stamped by this grader")
    ap.add_argument("--max-age-hours", type=float, default=None,
                    help="only grade records younger than N hours")
    args = ap.parse_args()

    files = _iter_files(args.file)
    if not files:
        print(f"no shadow files for {args.file}")
        return 1

    total = eligible = graded = 0
    arms: dict[str, dict[bool, list[float]]] = {k: _new_tally() for k, _, _ in ARMS}
    cutoff = (time.time() - args.max_age_hours * 3600) if args.max_age_hours else None

    for path in files:
        recs = []
        changed = 0
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue
                total += 1
                if not _eligible(r):
                    recs.append(r)
                    continue
                eligible += 1
                if r.get(GRADED_FLAG) and not args.force:
                    _tally_arms(r, arms)
                    recs.append(r)
                    continue
                t0 = _signal_ms(r)
                if cutoff and t0 and t0 / 1000 < cutoff:
                    recs.append(r)
                    continue
                outcome = grade(r)
                r["rt_outcome"] = outcome
                if outcome in MATURE_OUTCOMES:
                    r[GRADED_FLAG] = True
                    changed += 1
                    graded += 1
                    _tally_arms(r, arms)
                recs.append(r)
        if args.write and changed:
            with open(path, "w", encoding="utf-8") as f:
                for r in recs:
                    f.write(json.dumps(r, ensure_ascii=False) + "\n")
        print(f"  {path}: records={len(recs)} eligible_passes={eligible} "
              f"newly_graded={changed}"
              f"{' [WRITTEN]' if args.write and changed else ''}")

    print("\n=== relax_tier shadow reconciliation (passed trades, "
          f"{PRIMARY_HOURS}h forward net) ===")
    print(f"records scanned: {total}   eligible passes: {eligible}   "
          f"mature+graded: {graded}")
    for key, _fld, label in ARMS:
        t = arms[key]
        _print_cell(key, label, True, t[True])
        _print_cell(key, label, False, t[False])
    if not args.write:
        print("\n(dry-run; pass --write to persist rt outcomes into the JSONL)")
    return 0


def _tally_arms(r: dict[str, Any], arms: dict[str, dict[bool, list[float]]]) -> None:
    v = r.get("rt_pnl_pct")
    if not isinstance(v, (int, float)):
        return
    for key, fld, _ in ARMS:
        flag = r.get(fld)
        if flag is True:
            arms[key][True].append(float(v))
        elif flag is False:
            arms[key][False].append(float(v))


def _print_cell(key: str, label: str, flagged: bool, xs: list[float]) -> None:
    state = "FLAGGED (veto would block)" if flagged else "spared (veto allows)"
    if not xs:
        print(f"  {key:14s} {label:24s} {state:28s} n=0")
        return
    wins = sum(1 for x in xs if x > 0)
    exp = sum(xs) / len(xs)
    print(f"  {key:14s} {label:24s} {state:28s} "
          f"n={len(xs):3d}  WR={100*wins/len(xs):5.1f}%  exp={exp:+.3f}%/trade")


if __name__ == "__main__":
    raise SystemExit(main())
