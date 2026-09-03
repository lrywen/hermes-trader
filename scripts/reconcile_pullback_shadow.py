#!/usr/bin/env python3
"""Reconcile pullback-long SHADOW signals with their would-be PnL.

Reads ~/.hermes-trading/pullback_shadow.jsonl (path overridable via
HERMES_PULLBACK_SHADOW_FILE), fetches 1h candles after each signal's
timestamp, simulates the same DSL two-phase trailing stop used in live
trading, and writes outcome/exit_px/pnl_usd back to each record.

Run this after the 48h shadow window has elapsed (or periodically to
watch results accrue). The script never places orders — it is pure
paper reconciliation.

Usage:
    python3 scripts/reconcile_pullback_shadow.py
    python3 scripts/reconcile_pullback_shadow.py --window-hours 48
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

os.environ["HERMES_BACKTEST"] = "1"
_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO))

from hermes_trader.agents.config_store import cfg_get, read_agent_config
from hermes_trader.client.hl_client import fetch_hl_candles
from hermes_trader.models.types import Candle

SHADOW_FILE = os.environ.get(
    "HERMES_PULLBACK_SHADOW_FILE",
    os.path.expanduser("~/.hermes-trading/pullback_shadow.jsonl"),
)
ROUND_TRIP_FEE_BPS = 5.0


def _parse_iso(ts: str) -> Optional[datetime]:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def _find_entry_bar(candles: List[Candle], after_ts: datetime) -> int:
    """Index of the first candle whose open time >= after_ts."""
    for i, c in enumerate(candles):
        # Candle.t is open time in ms (Hyperliquid convention).
        ct = getattr(c, "t", None)
        if ct is None:
            continue
        bar_dt = datetime.fromtimestamp(ct / 1000.0, tz=timezone.utc)
        if bar_dt >= after_ts:
            return i
    return -1


def _simulate_exit(entry_px: float, entry_idx: int,
                   candles: List[Candle], dsl_cfg: Dict[str, Any]
                   ) -> Tuple[float, str, int]:
    """Walk DSL two-phase stop from entry_idx+1. Return (exit_px, reason, exit_idx)."""
    max_loss = float(cfg_get("dsl_exit.max_loss_pct", config=dsl_cfg))
    protect = float(cfg_get("dsl_exit.protect_pct", config=dsl_cfg))
    retrace = float(cfg_get("dsl_exit.retrace_threshold", config=dsl_cfg))
    hard_timeout = 180
    peak = entry_px
    for j in range(entry_idx + 1, min(entry_idx + 1 + hard_timeout, len(candles))):
        bar = candles[j]
        if j - entry_idx >= hard_timeout:
            return bar.c, "hard_timeout", j
        stop_px = entry_px * (1 - max_loss / 100)
        if bar.l <= stop_px:
            return min(stop_px, bar.o), f"max_loss {max_loss}%", j
        profit_pct = (peak - entry_px) / entry_px * 100
        if profit_pct >= protect:
            profit_range = peak - entry_px
            floor = entry_px + profit_range * (1 - retrace)
            if bar.l <= floor:
                return min(floor, bar.o), "trailing_stop", j
        if bar.h > peak:
            peak = bar.h
    last = candles[min(entry_idx + hard_timeout, len(candles) - 1)]
    return last.c, "window_end", min(entry_idx + hard_timeout, len(candles) - 1)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--file", default=SHADOW_FILE)
    ap.add_argument("--window-hours", type=int, default=48,
                    help="Only reconcile signals older than this many hours")
    ap.add_argument("--write", action="store_true",
                    help="Write outcomes back into the JSONL (default: dry-run report)")
    args = ap.parse_args()

    if not os.path.exists(args.file):
        print(f"shadow file not found: {args.file}")
        return 1

    with open(args.file, encoding="utf-8") as f:
        records = [json.loads(line) for line in f if line.strip()]

    pending = [r for r in records if r.get("outcome") is None]
    cutoff = datetime.now(timezone.utc).timestamp() - args.window_hours * 3600
    # Reconcile signals whose timestamp is old enough to have matured.
    mature = []
    for r in pending:
        dt = _parse_iso(r.get("timestamp", ""))
        if dt and dt.timestamp() <= cutoff:
            mature.append(r)

    print(f"total records: {len(records)}  pending: {len(pending)}  "
          f"mature (>= {args.window_hours}h): {len(mature)}")

    live = read_agent_config()
    dsl_cfg = live.get("dsl_exit", {}) or {}
    fee_pct = ROUND_TRIP_FEE_BPS / 10000.0

    results: List[Dict[str, Any]] = []
    for r in mature:
        coin = r["coin"]
        entry_px = float(r.get("entry_px") or 0)
        if entry_px <= 0:
            r["outcome"] = "no_entry_px"
            continue
        after_ts = _parse_iso(r["timestamp"])
        if after_ts is None:
            r["outcome"] = "bad_timestamp"
            continue
        try:
            candles = fetch_hl_candles(coin, "1h", hard_timeout_bars := 300)
        except Exception as e:
            print(f"  {coin}: fetch error: {e}")
            continue
        idx = _find_entry_bar(candles, after_ts)
        if idx < 0 or idx >= len(candles) - 2:
            r["outcome"] = "no_future_bars"
            continue
        # Use the actual recorded entry_px (mid at signal time); if zero, use bar open.
        ep = entry_px if entry_px > 0 else candles[idx].o
        exit_px, reason, exit_idx = _simulate_exit(ep, idx, candles, dsl_cfg)
        gross_pct = (exit_px - ep) / ep  # long only
        pnl_pct = gross_pct - fee_pct
        r["exit_px"] = round(exit_px, 6)
        r["pnl_pct"] = round(pnl_pct * 100, 4)
        r["exit_reason"] = reason
        r["outcome"] = "win" if pnl_pct > 0 else "loss"
        results.append(r)

    if results:
        wins = [r for r in results if r["outcome"] == "win"]
        losses = [r for r in results if r["outcome"] == "loss"]
        avg_w = sum(r["pnl_pct"] for r in wins) / len(wins) if wins else 0
        avg_l = sum(r["pnl_pct"] for r in losses) / len(losses) if losses else 0
        payoff = abs(avg_w / avg_l) if avg_l else float("inf")
        print(f"\n=== Shadow reconciliation: {len(results)} mature signals ===")
        print(f"  win rate   : {len(wins)}/{len(results)} = {len(wins)/len(results)*100:.1f}%")
        print(f"  avg win    : {avg_w:+.2f}%")
        print(f"  avg loss   : {avg_l:+.2f}%")
        print(f"  payoff     : {payoff:.2f}")
        print(f"  expectancy : {sum(r['pnl_pct'] for r in results)/len(results):+.3f}%/trade")
        by_reason: Dict[str, int] = {}
        for r in results:
            by_reason[r["exit_reason"]] = by_reason.get(r["exit_reason"], 0) + 1
        print(f"  exits      : {by_reason}")
        print("\n  per-signal detail:")
        for r in sorted(results, key=lambda x: x["timestamp"]):
            print(f"    {r['timestamp']}  {r['coin']:8}  "
                  f"entry={r.get('entry_px',0):.4f}  exit={r['exit_px']:.4f}  "
                  f"pnl={r['pnl_pct']:+.2f}%  [{r['outcome']}/{r['exit_reason']}]")

    if args.write:
        with open(args.file, "w", encoding="utf-8") as f:
            for r in records:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        print(f"\nOutcomes written back to {args.file}")
    else:
        print("\n(dry-run; pass --write to persist outcomes into the JSONL)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
