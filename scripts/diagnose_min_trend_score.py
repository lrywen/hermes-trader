#!/usr/bin/env python3
"""Diagnose 坑2: of the macro "aligned but trend_score<min" demotions, how
many were a FAKE trend (the weak EMA cross failed -> demotion was right) vs a
REAL trend the score nearly missed (demotion would have over-blocked)?

The min_trend_score overlay judges the MACRO proxy (BTC for crypto). Every
demoted scan therefore shares the same question at the BTC level: right after
the 4-state classifier said up/down while the 5-component score was < floor,
did BTC actually keep trending? We extract each distinct macro sample the gate
saw (regime + trend_score, de-duped within the regime cache TTL) from
events.jsonl gate_results.market_regime, then walk BTC 1h candles forward.

Per future sample we measure, in the regime direction (long for up):
  * mfe_fwd   best favorable % over H bars
  * mae_fwd   worst adverse % over H bars
  * exit_ret  % at window end (or at a -max_loss stop, whichever first)
Classification (conservative, fee-aware constants at top):
  real_trend : adverse never hits STOP and exit_ret >= CONFIRM or mfe>=CONFIRM*2
  fake_trend : adverse hits STOP first, or window ends |ret| < FLAT
  ambiguous  : everything in between
Read-only: fetches public candles, writes nothing.

Usage:
  python3 scripts/diagnose_min_trend_score.py \
      [--events /data/events.jsonl] [--floor 0.55] [--horizon 24]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

os.environ.setdefault("HERMES_BACKTEST", "1")
_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO))

from hermes_trader.client.hl_client import fetch_hl_candles  # noqa: E402

DEFAULT_EVENTS = os.environ.get("HERMES_EVENTS_FILE", "/data/events.jsonl")
DEDUP_TTL_MS = 5 * 60_000          # == market_regime REGIME_TTL_S
STOP_PCT = 0.8                     # adverse move that confirms a fakeout
CONFIRM_PCT = 1.0                  # end-of-window follow-through to call real
FLAT_PCT = 0.5                     # |exit| below this = chop/fake
FEE_BPS = 5.0


def _parse_iso(ts: str) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def extract_demoted_samples(events_path: str, floor: float) -> List[dict]:
    """Distinct macro (ts, regime, score) points the gate demoted.

    Weak demotion is flagged weak_trend_score=True on the counter-trend path.
    De-dupe by (regime direction bucket) within TTL so one cached BTC reading
    shared across a whole scan counts once.
    """
    samples: List[dict] = []
    last_by_regime: Dict[str, int] = {}
    try:
        fh = open(events_path, encoding="utf-8")
    except OSError as e:
        print(f"events file not readable: {events_path}: {e}")
        return samples
    with fh:
        for line in fh:
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if e.get("event") != "execute":
                continue
            p = e.get("payload") or {}
            m = (p.get("gate_results") or {}).get("market_regime")
            if not isinstance(m, dict) or not m.get("weak_trend_score"):
                continue
            regime = str(m.get("regime") or "")
            if regime not in ("up", "down"):
                continue
            ts = _parse_iso(e.get("timestamp", ""))
            if ts is None:
                continue
            ms = ts.timestamp() * 1000.0
            if ms - last_by_regime.get(regime, -1e18) < DEDUP_TTL_MS:
                continue
            last_by_regime[regime] = ms
            samples.append({
                "ts": ts, "regime": regime,
                "score": float(m.get("trend_score") or 0.0),
                "via": m.get("via"),
                "coin": p.get("coin"),
                "executed": bool(p.get("executed")),
            })
    samples.sort(key=lambda s: s["ts"])
    return samples


def _bar_idx_after(candles, after: datetime) -> int:
    for i, c in enumerate(candles):
        ct = getattr(c, "t", None)
        if ct is None:
            continue
        if datetime.fromtimestamp(ct / 1000.0, tz=timezone.utc) >= after:
            return i
    return -1


def walk_forward(candles, idx: int, regime: str,
                 horizon: int) -> Optional[Dict[str, float]]:
    """Measure follow-through in the regime direction over the next H bars."""
    if idx < 0 or idx + 2 >= len(candles):
        return None
    entry = candles[idx].c
    if not entry:
        return None
    sign = 1.0 if regime == "up" else -1.0
    end = min(idx + 1 + horizon, len(candles))
    mfe = mae = 0.0
    stop = STOP_PCT / 100.0
    for j in range(idx + 1, end):
        hi = (candles[j].h - entry) / entry * sign
        lo = (candles[j].l - entry) / entry * sign
        if hi > mfe:
            mfe = hi
        if lo < mae:
            mae = lo
        if lo * 100 <= -STOP_PCT:
            return {"mfe": mfe * 100, "mae": mae * 100, "exit_ret": -STOP_PCT,
                    "stopped": True, "bars": j - idx}
    last_ret = (candles[end - 1].c - entry) / entry * sign * 100
    return {"mfe": mfe * 100, "mae": mae * 100, "exit_ret": last_ret,
            "stopped": False, "bars": end - 1 - idx}


def classify(w: Dict[str, float]) -> str:
    fee = FEE_BPS / 100.0
    ret = w["exit_ret"] - fee
    mfe = w["mfe"] - fee
    if w["stopped"]:
        return "fake_trend"        # adverse stop before follow-through
    if ret >= CONFIRM_PCT or mfe >= 2 * CONFIRM_PCT:
        return "real_trend"
    if abs(w["exit_ret"]) < FLAT_PCT:
        return "fake_trend"        # went nowhere = the cross was noise
    return "ambiguous"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--events", default=DEFAULT_EVENTS)
    ap.add_argument("--floor", type=float, default=0.55)
    ap.add_argument("--horizon", type=int, default=24, help="forward 1h bars")
    ap.add_argument("--proxy", default="BTC")
    args = ap.parse_args()

    samples = extract_demoted_samples(args.events, args.floor)
    print(f"distinct demoted macro samples: {len(samples)} "
          f"(floor={args.floor}, dedup TTL=5min)")
    if not samples:
        return 0
    by_via: Dict[str, int] = {}
    by_reg: Dict[str, int] = {}
    for s in samples:
        by_via[s["via"]] = by_via.get(s["via"], 0) + 1
        by_reg[s["regime"]] = by_reg.get(s["regime"], 0) + 1
    print("  by regime:", by_reg, " final via:", by_via)
    executed = sum(1 for s in samples if s["executed"])
    print(f"  of these scan points, executed={executed}")

    candles = fetch_hl_candles(args.proxy, "1h", 2000)
    print(f"fetched {len(candles)} {args.proxy} 1h candles")

    buckets = {"real_trend": 0, "fake_trend": 0, "ambiguous": 0, "no_data": 0}
    rows = []
    for s in samples:
        idx = _bar_idx_after(candles, s["ts"])
        w = walk_forward(candles, idx, s["regime"], args.horizon) if idx >= 0 \
            else None
        if w is None:
            buckets["no_data"] += 1
            continue
        verdict = classify(w)
        buckets[verdict] += 1
        rows.append((s, w, verdict))

    graded = sum(v for k, v in buckets.items() if k != "no_data")
    print(f"\n=== follow-through over next {args.horizon}h on {args.proxy} ===")
    for k in ("fake_trend", "real_trend", "ambiguous", "no_data"):
        print(f"  {k:12} {buckets[k]:3d}")
    if graded:
        fr = buckets["fake_trend"] / graded * 100
        rt = buckets["real_trend"] / graded * 100
        print(f"\n  demotion was RIGHT (fake trend blocked): {fr:.1f}%")
        print(f"  demotion was OVER-BLOCK (real trend):    {rt:.1f}%")
        print(f"  ambiguous: {buckets['ambiguous']/graded*100:.1f}%")

    # score-binned: does a lower floor (0.50) rescue mostly real trends?
    print("\n=== by observed trend_score bin ===")
    bins = {"<0.40": [], "0.40-0.50": [], "0.50-0.55": []}
    for s, w, v in rows:
        sc = s["score"]
        key = "<0.40" if sc < 0.40 else "0.40-0.50" if sc < 0.50 else "0.50-0.55"
        bins[key].append(v)
    for key, vals in bins.items():
        if not vals:
            print(f"  {key:10} n=0")
            continue
        rt = vals.count("real_trend")
        fr = vals.count("fake_trend")
        print(f"  {key:10} n={len(vals):3d}  real={rt:3d} fake={fr:3d} "
              f"ambig={len(vals)-rt-fr:3d}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
