#!/usr/bin/env python3
"""Diagnostic: CHOP/NEUTRAL RSI<25 oversold filter analysis.

Runs the REGIME backtest and cross-tabulates CHOP/NEUTRAL trades by
side x RSI bucket to locate losses.  Then simulates an RSI<25 long
filter (reject LONG entries when RSI<25 in CHOP/NEUTRAL) and reports
the PnL delta.

Usage:
    python3 scripts/diag_chop_rsi_filter.py
    python3 scripts/diag_chop_rsi_filter.py --days 30 --coins 20
"""
from __future__ import annotations

import argparse
import os
import sys
from collections import defaultdict
from pathlib import Path
from typing import List

os.environ["HERMES_BACKTEST"] = "1"

_REPO = Path(__file__).resolve().parents[1]
_env = _REPO / ".env.local"
if _env.is_file():
    for _line in _env.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _, _v = _line.partition("=")
            if _k.strip() == "HYPERLIQUID_PRIVATE_KEY":
                continue
            os.environ.setdefault(_k.strip(), _v.strip())
sys.path.insert(0, str(_REPO))

from hermes_trader.agents.config import get_config
from hermes_trader.client.hl_client import fetch_hl_candles
from hermes_trader.client.universe import get_universe

sys.path.insert(0, str(Path(__file__).resolve().parent))
from backtest_ab_compare import (
    Trade,
    _simulate,
)


def _rsi_bucket(rsi: float) -> str:
    if rsi < 25:
        return "<25"
    if rsi < 35:
        return "25-35"
    if rsi < 50:
        return "35-50"
    if rsi < 65:
        return "50-65"
    if rsi < 75:
        return "65-75"
    return ">=75"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--coins", type=int, default=20)
    ap.add_argument("--equity", type=float, default=200.0)
    args = ap.parse_args()

    cfg = get_config()
    universe = get_universe()
    perps = [m for m in universe
             if m.get("type") == "perp" and not m["coin"].startswith("@")]
    top = sorted(perps, key=lambda m: m.get("dayNtlVlm", 0), reverse=True)[: args.coins]

    # Read live config for sizing.
    try:
        from hermes_trader.agents.config_store import read_agent_config
        live = read_agent_config() or {}
    except Exception:
        live = {}
    equity_fraction = float(live.get("equity_fraction_per_trade", 0.20))
    leverage = int(live.get("leverage", 12))
    dsl = live.get("dsl_exit", {}) or {}
    max_loss = float(dsl.get("max_loss_pct", 0.4))
    protect = float(dsl.get("protect_pct", 1.25))
    retrace = float(dsl.get("retrace_threshold", 0.20))

    total_bars = args.days * 24 + 200
    all_trades: List[Trade] = []

    print(f"Running REGIME backtest: {args.days}d, {args.coins} coins, "
          f"max_loss={max_loss}%, protect={protect}%, retrace={retrace}")
    print(f"{'coin':>10}  {'n':>4}  {'PnL':>10}")
    print(f"{'-'*10}  {'-'*4}  {'-'*10}")

    for m in top:
        coin = m["coin"]
        max_lev = int(m.get("maxLeverage", 5))
        try:
            candles = fetch_hl_candles(coin, "1h", total_bars)
        except Exception as e:
            print(f"  {coin:10s} fetch error: {e}")
            continue
        if not candles or len(candles) < 150:
            continue
        trades = _simulate(
            coin, candles, max_lev,
            equity=args.equity, equity_fraction=equity_fraction,
            lev_ceiling=leverage, cfg=cfg, use_new_rules=True,
            max_loss_pct=max_loss, protect_pct=protect,
            retrace_threshold=retrace, rsi_variant="regime",
        )
        cp = sum(t.pnl_usd for t in trades)
        print(f"  {coin:10s}  {len(trades):>4}  ${cp:>+9.2f}")
        all_trades.extend(trades)

    # ------------------------------------------------------------------
    # Cross-tab: regime x side x RSI bucket
    # ------------------------------------------------------------------
    print("\n" + "=" * 90)
    print("  CHOP/NEUTRAL ATTRIBUTION BY side x RSI BUCKET")
    print("=" * 90)

    focus_regimes = {"CHOP", "NEUTRAL"}
    buckets = ["<25", "25-35", "35-50", "50-65", "65-75", ">=75"]

    grid: dict = defaultdict(lambda: {"n": 0, "wins": 0, "pnl": 0.0})
    for t in all_trades:
        if t.regime_label not in focus_regimes:
            continue
        key = (t.regime_label, t.side, _rsi_bucket(t.rsi_at_entry))
        g = grid[key]
        g["n"] += 1
        g["wins"] += 1 if t.pnl_usd > 0 else 0
        g["pnl"] += t.pnl_usd

    for regime in ("CHOP", "NEUTRAL"):
        print(f"\n  --- {regime} ---")
        print(f"  {'side':<6} {'RSI':<8} {'n':>4} {'WR':>6} {'PnL':>10} {'avg':>8}")
        print(f"  {'-'*6} {'-'*8} {'-'*4} {'-'*6} {'-'*10} {'-'*8}")
        for side in ("long", "short"):
            for b in buckets:
                g = grid.get((regime, side, b))
                if not g or g["n"] == 0:
                    continue
                wr = g["wins"] / g["n"] * 100
                avg = g["pnl"] / g["n"]
                print(f"  {side:<6} {b:<8} {g['n']:>4} {wr:>5.1f}% "
                      f"${g['pnl']:>+9.2f} ${avg:>+7.2f}")

    # ------------------------------------------------------------------
    # Totals + filter simulation
    # ------------------------------------------------------------------
    print("\n" + "=" * 90)
    print("  RSI<25 LONG FILTER SIMULATION (CHOP + NEUTRAL)")
    print("=" * 90)

    baseline = [t for t in all_trades if t.regime_label in focus_regimes]
    baseline_pnl = sum(t.pnl_usd for t in baseline)
    baseline_n = len(baseline)

    # Trades that would be removed: LONG with RSI<25 in CHOP/NEUTRAL
    removed = [t for t in baseline
               if t.side == "long" and t.rsi_at_entry < 25]
    removed_pnl = sum(t.pnl_usd for t in removed)
    removed_wins = sum(1 for t in removed if t.pnl_usd > 0)

    # Also test the symmetric filter: SHORT with RSI>75
    removed_s = [t for t in baseline
                 if t.side == "short" and t.rsi_at_entry >= 75]
    removed_s_pnl = sum(t.pnl_usd for t in removed_s)
    removed_s_wins = sum(1 for t in removed_s if t.pnl_usd > 0)

    print("\n  Baseline (CHOP+NEUTRAL):")
    print(f"    trades : {baseline_n}")
    print(f"    PnL    : ${baseline_pnl:+.2f}")
    print(f"    WR     : {sum(1 for t in baseline if t.pnl_usd>0)/baseline_n*100:.1f}%"
          if baseline_n else "    WR     : n/a")

    print("\n  Filter A: reject LONG when RSI<25")
    print(f"    removed: {len(removed)} trades, "
          f"WR={removed_wins/len(removed)*100:.1f}%" if removed else "    removed: 0")
    print(f"    PnL of removed: ${removed_pnl:+.2f}")
    if removed:
        print(f"    -> After filter PnL: ${baseline_pnl - removed_pnl:+.2f} "
              f"(delta ${-removed_pnl:+.2f})")
        print(f"    -> After filter trades: {baseline_n - len(removed)}")

    print("\n  Filter B: reject SHORT when RSI>=75")
    print(f"    removed: {len(removed_s)} trades, "
          f"WR={removed_s_wins/len(removed_s)*100:.1f}%" if removed_s else "    removed: 0")
    print(f"    PnL of removed: ${removed_s_pnl:+.2f}")
    if removed_s:
        print(f"    -> After filter PnL: ${baseline_pnl - removed_s_pnl:+.2f} "
              f"(delta ${-removed_s_pnl:+.2f})")

    # Combined filter
    if removed or removed_s:
        combined_removed = len(removed) + len(removed_s)
        combined_pnl = removed_pnl + removed_s_pnl
        print("\n  Combined (A+B): reject LONG RSI<25 AND SHORT RSI>=75")
        print(f"    removed: {combined_removed} trades")
        print(f"    PnL of removed: ${combined_pnl:+.2f}")
        print(f"    -> After filter PnL: ${baseline_pnl - combined_pnl:+.2f} "
              f"(delta ${-combined_pnl:+.2f})")

    # Per-regime breakdown of the RSI<25 longs
    print("\n  RSI<25 LONG trades by regime:")
    for regime in ("CHOP", "NEUTRAL"):
        sub = [t for t in removed if t.regime_label == regime]
        if not sub:
            print(f"    {regime:>10}: 0 trades")
            continue
        sp = sum(t.pnl_usd for t in sub)
        sw = sum(1 for t in sub if t.pnl_usd > 0)
        print(f"    {regime:>10}: {len(sub):>3} trades  "
              f"WR={sw/len(sub)*100:5.1f}%  PnL=${sp:+.2f}  "
              f"avg=${sp/len(sub):+.2f}")

    # Exit reason breakdown for removed trades
    if removed:
        print("\n  Exit reasons of RSI<25 LONG trades (CHOP+NEUTRAL):")
        reasons: dict = defaultdict(lambda: {"n": 0, "pnl": 0.0})
        for t in removed:
            r = reasons[t.exit_reason]
            r["n"] += 1
            r["pnl"] += t.pnl_usd
        for reason, r in sorted(reasons.items(), key=lambda kv: -kv[1]["n"]):
            print(f"    {reason:<20}: {r['n']:>3} trades  PnL=${r['pnl']:+.2f}")

    print("\n" + "=" * 90)
    return 0


if __name__ == "__main__":
    sys.exit(main())
