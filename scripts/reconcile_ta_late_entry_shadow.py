#!/usr/bin/env python3
"""Reconcile ta_late_entry SHADOW vetoes with their would-be PnL.

Reads the ta_late_entry shadow JSONL (default
~/.hermes-trading/ta_late_entry_shadow.jsonl, overridable via
HERMES_TA_LATE_ENTRY_SHADOW_FILE or --file), which contains would-block
verdicts from BOTH layers:
  * layer="gate"      — order-time ta_late_entry_gate (has entry_px + notional)
  * layer="prefilter" — pre-AI ta_filter REJECTED (no entry_px; uses bar open)

For every blocked record old enough to have matured (default >= 8h, i.e. two
4h bars have closed since the signal), this fetches 4h candles after the
signal and scores the counterfactual: what would a chase entry here have made
hold_bars x 4h later?
  * The signal fires while bar B0 is still forming; B0 is the last bar whose
    open time is <= the signal timestamp.
  * exit_px  = close of the hold_bars-th bar to CLOSE after the signal
               (hold_bars=2 -> B1.close, ~8h after the signal, matching the
               maturity window)
  * pnl_pct  = side-aware gross move minus round-trip fees
  * pnl_usd  = pnl_pct * trade_notional_usd when notional was recorded
  * mae_pct  = max adverse excursion over the holding window (worst close
               from B0.close to the exit bar; wicks ignored — conservative)
  * outcome  = win / loss (net of fees)

This is the counterfactual window the gate exists to win: a veto is "good"
when these would-be trades lose (or round-trip through a deep MAE). The
script never places orders — pure paper reconciliation.

Usage:
    python3 scripts/reconcile_ta_late_entry_shadow.py
    python3 scripts/reconcile_ta_late_entry_shadow.py --hold-bars 2 --write
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

from hermes_trader.client.hl_client import fetch_hl_candles  # noqa: E402
from hermes_trader.models.types import Candle  # noqa: E402

SHADOW_FILE = os.environ.get(
    "HERMES_TA_LATE_ENTRY_SHADOW_FILE",
    os.path.expanduser("~/.hermes-trading/ta_late_entry_shadow.jsonl"),
)
# Round-trip taker fees, bps (matches reconcile_pullback_shadow.py).
ROUND_TRIP_FEE_BPS = 5.0
HOLD_BARS = 2
FETCH_COUNT = 200


def _parse_iso(ts: str) -> Optional[datetime]:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def _find_entry_bar(candles: List[Candle], after_ts: datetime) -> int:
    """Index of the first candle whose OPEN time is >= the signal timestamp.

    The signal fired while this bar (B1) had not yet opened; the bar before it
    (``idx - 1``, B0) was the still-forming 4h bar at signal time. Returns -1
    if every bar opened before the signal.
    """
    for i, c in enumerate(candles):
        ct = getattr(c, "t", None)
        if ct is None:
            continue
        bar_dt = datetime.fromtimestamp(ct / 1000.0, tz=timezone.utc)
        if bar_dt >= after_ts:
            return i
    return -1


def _score(side: str, entry_px: float, b1_idx: int, candles: List[Candle],
           hold_bars: int, fee_pct: float) -> Tuple[float, str, float]:
    """Score the counterfactual over the bars after the signal.

    ``b1_idx`` is the index of the first bar to OPEN after the signal (B1);
    B0 = ``candles[b1_idx - 1]`` was forming at signal time. Entry is filled
    at ``entry_px`` (the recorded live price, or B0.close for prefilter
    records that carry no price) and the trade exits at the close of the
    ``hold_bars``-th bar to close after the signal. The signal fires while B0
    is forming, so B0 is the 1st bar to close, B1 the 2nd — hold_bars=2 exits
    at B1.close, ~8h after the signal, matching the default maturity window.
    MAE is the worst adverse close from B0.close through the exit
    (conservative: intra-bar wicks ignored).

    Returns (exit_px, outcome, mae_pct).
    """
    sign = 1.0 if side == "long" else -1.0
    # Bars to close after the signal, in signal order: B0 = b1_idx-1 (1st),
    # B1 = b1_idx (2nd), ... => nth close lives at index b1_idx-2+n.
    exit_idx = b1_idx - 2 + hold_bars
    exit_px = candles[exit_idx].c
    # Most adverse excursion: largest move AGAINST the position over the hold,
    # starting from B0.close (the last price known at signal time).
    mae_pct = 0.0
    for j in range(max(b1_idx - 1, 0), exit_idx + 1):
        move_pct = sign * (candles[j].c - entry_px) / entry_px * 100.0
        if move_pct < mae_pct:
            mae_pct = move_pct
    gross_pct = sign * (exit_px - entry_px) / entry_px * 100.0
    net_pct = gross_pct - fee_pct * 100.0
    return exit_px, ("win" if net_pct > 0 else "loss"), mae_pct


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--file", default=SHADOW_FILE)
    ap.add_argument("--hold-bars", type=int, default=HOLD_BARS,
                    help="4h bars to hold the counterfactual trade (default 2)")
    ap.add_argument("--window-hours", type=int, default=8,
                    help="Only reconcile records older than this many hours "
                         "(default 8 = two 4h bars)")
    ap.add_argument("--layer", choices=("all", "gate", "prefilter"),
                    default="all", help="Restrict to one decision layer")
    ap.add_argument("--write", action="store_true",
                    help="Write outcomes back into the JSONL (default: dry-run)")
    args = ap.parse_args()

    if not os.path.exists(args.file):
        print(f"shadow file not found: {args.file}")
        return 1

    with open(args.file, encoding="utf-8") as f:
        records = [json.loads(line) for line in f if line.strip()]

    # Only blocked (would-veto) records are counterfactual trades. Records
    # written before P0-4 lack a layer field; treat them as "gate".
    def _layer(r: Dict[str, Any]) -> str:
        return str(r.get("layer") or "gate")

    vetoes = [
        r for r in records
        if r.get("blocked") is True and (
            args.layer == "all" or _layer(r) == args.layer)
    ]
    pending = [r for r in vetoes if r.get("outcome") is None]
    cutoff = datetime.now(timezone.utc).timestamp() - args.window_hours * 3600
    mature = []
    for r in pending:
        dt = _parse_iso(r.get("timestamp", ""))
        if dt and dt.timestamp() <= cutoff:
            mature.append(r)

    print(f"total records: {len(records)}  vetoes: {len(vetoes)}  "
          f"pending: {len(pending)}  mature (>= {args.window_hours}h): {len(mature)}")

    fee_pct = ROUND_TRIP_FEE_BPS / 10000.0
    results: List[Dict[str, Any]] = []
    for r in mature:
        coin = r["coin"]
        side = r.get("side", "long") if r.get("side") in ("long", "short") else "long"
        after_ts = _parse_iso(r.get("timestamp"))
        if after_ts is None:
            r["outcome"] = "bad_timestamp"
            continue
        try:
            candles = fetch_hl_candles(coin, "4h", FETCH_COUNT)
        except Exception as e:  # noqa: BLE001
            print(f"  {coin}: fetch error: {e}")
            continue
        idx = _find_entry_bar(candles, after_ts)
        if idx < 1 or idx - 2 + args.hold_bars >= len(candles) \
                or idx - 2 + args.hold_bars < 0:
            r["outcome"] = "no_future_bars"
            continue
        entry_px = float(r.get("entry_px") or 0)
        if entry_px <= 0:
            # Prefilter records carry no entry px: approximate the chase fill
            # at the close of B0, the bar that was forming at signal time.
            entry_px = float(candles[idx - 1].c)
        exit_px, outcome, mae_pct = _score(
            side, entry_px, idx, candles, args.hold_bars, fee_pct)
        gross_pct = (1.0 if side == "long" else -1.0) * (exit_px - entry_px) / entry_px * 100.0
        net_pct = gross_pct - fee_pct * 100.0
        notional = r.get("trade_notional_usd")
        r["entry_px"] = round(entry_px, 6)
        r["exit_px"] = round(exit_px, 6)
        r["pnl_pct"] = round(net_pct, 4)
        r["mae_pct"] = round(mae_pct, 4)
        r["hold_bars"] = args.hold_bars
        if notional:
            r["pnl_usd"] = round(net_pct / 100.0 * float(notional), 2)
        r["outcome"] = outcome
        r.setdefault("layer", "gate")
        results.append(r)

    if results:
        wins = [r for r in results if r["outcome"] == "win"]
        losses = [r for r in results if r["outcome"] == "loss"]
        avg_w = sum(r["pnl_pct"] for r in wins) / len(wins) if wins else 0.0
        avg_l = sum(r["pnl_pct"] for r in results if r["outcome"] == "loss")
        avg_l = avg_l / len(losses) if losses else 0.0
        avg_mae = sum(r["mae_pct"] for r in results) / len(results)
        print(f"\n=== ta_late_entry counterfactual: {len(results)} mature vetoes "
              f"({args.hold_bars}x4h hold) ===")
        print(f"  vetoed-trade win rate : {len(wins)}/{len(results)} = "
              f"{len(wins)/len(results)*100:.1f}%  (LOWER = gate is good)")
        print(f"  avg win / avg loss    : {avg_w:+.2f}% / {avg_l:+.2f}%")
        print(f"  expectancy            : {sum(r['pnl_pct'] for r in results)/len(results):+.3f}%/trade")
        print(f"  avg MAE vs signal     : {avg_mae:.2f}% (deeper = veto spared drawdown)")
        for layer in ("gate", "prefilter"):
            sub = [r for r in results if r.get("layer") == layer]
            if sub:
                w = sum(1 for r in sub if r["outcome"] == "win")
                print(f"  layer={layer:9s}: {len(sub):3d} vetoes, "
                      f"would-be win rate {w/len(sub)*100:5.1f}%, "
                      f"expectancy {sum(r['pnl_pct'] for r in sub)/len(sub):+.3f}%")
        print(f"\n  per-veto detail:")
        for r in sorted(results, key=lambda x: x["timestamp"]):
            print(f"    {r['timestamp']}  {r.get('layer','gate'):9s} "
                  f"{r['coin']:8} {r.get('side','long'):5s} "
                  f"entry={r['entry_px']:.4f}  exit={r['exit_px']:.4f}  "
                  f"pnl={r['pnl_pct']:+.2f}%  mae={r['mae_pct']:+.2f}%  "
                  f"[{r['outcome']}]")

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
