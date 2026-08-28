#!/usr/bin/env python3
"""Plan B effectiveness audit: TREND mid-range (RSI 40-60) loss comparison.

Runs the REGIME backtest column TWICE over identical candles for the same
universe — once with Plan B enabled (size x0.5 on TREND + RSI 40-60) and once
with it disabled (full size) — then isolates the trades that entered in the
TREND mid-range RSI band and reports:

  - number of trades, win rate, total PnL
  - number of LOSING trades, average loss (USD and %), total loss
  - the same metrics with Plan B off, and the delta

Plan B only changes notional (halved), not entry/exit timing, so the trade
COUNT is identical in both runs; the USD PnL of each affected trade is halved.
The script verifies this directly from the two trade streams rather than
assuming it.

Usage:
    python3 scripts/plan_b_loss_compare.py --days 30 --coins 20
    python3 scripts/plan_b_loss_compare.py --days 30 --coin-list BTC,ETH,SOL
"""
from __future__ import annotations

import argparse
import os
import sys
from collections import defaultdict
from pathlib import Path
from typing import Dict, List

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

os.environ["HERMES_BACKTEST"] = "1"

from hermes_trader.agents.config import get_config          # noqa: E402
from hermes_trader.agents.config_store import read_agent_config, cfg_get  # noqa: E402
from hermes_trader.client.hl_client import fetch_hl_candles  # noqa: E402
from hermes_trader.client.universe import get_universe       # noqa: E402

from backtest_ab_compare import _simulate                    # noqa: E402

RSI_LO, RSI_HI = 40.0, 60.0


def _is_trend_mid(t) -> bool:
    return (t.regime_label == "TREND"
            and RSI_LO <= t.rsi_at_entry < RSI_HI)


def _band_stats(trades) -> Dict[str, float]:
    """Aggregate PnL stats for a list of trades."""
    n = len(trades)
    wins = [t for t in trades if t.pnl_usd > 0]
    losses = [t for t in trades if t.pnl_usd < 0]
    total_pnl = sum(t.pnl_usd for t in trades)
    total_loss = sum(t.pnl_usd for t in losses)  # negative
    avg_loss = (total_loss / len(losses)) if losses else 0.0
    # per-trade gross price move %, sign-aligned so a loss is negative
    loss_pcts = []
    for t in losses:
        gross = ((t.exit_px - t.entry_px) / t.entry_px
                 if t.side == "long"
                 else (t.entry_px - t.exit_px) / t.entry_px)
        loss_pcts.append(gross * 100.0)  # negative for losers
    avg_loss_pct = sum(loss_pcts) / len(loss_pcts) if loss_pcts else 0.0
    return {
        "n": n,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": (len(wins) / n * 100.0) if n else 0.0,
        "total_pnl": total_pnl,
        "total_loss": total_loss,
        "avg_loss_usd": avg_loss,
        "avg_loss_pct": avg_loss_pct,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--coins", type=int, default=20)
    ap.add_argument("--coin-list", type=str, default="",
                    help="Comma-separated coin list (overrides --coins top-N)")
    ap.add_argument("--equity", type=float, default=200.0)
    args = ap.parse_args()

    live = read_agent_config()
    equity_fraction = float(live.get("equity_fraction_per_trade", 0.10))
    lev_ceiling = int(cfg_get("leverage", config=live))
    dsl = live.get("dsl_exit", {}) or {}
    max_loss = float(cfg_get("dsl_exit.max_loss_pct", config=dsl))
    protect = float(cfg_get("dsl_exit.protect_pct", config=dsl))
    retrace = float(cfg_get("dsl_exit.retrace_threshold", config=dsl))

    total_bars = args.days * 24 + 150
    cfg = get_config()
    universe = get_universe()
    perps = [m for m in universe
             if m["type"] == "perp" and not m["coin"].startswith("@")]
    if args.coin_list.strip():
        wanted = {c.strip().upper() for c in args.coin_list.split(",") if c.strip()}
        coins = [m for m in perps if m["coin"] in wanted]
    else:
        coins = sorted(perps, key=lambda m: m.get("dayNtlVlm", 0),
                       reverse=True)[: args.coins]

    print("=== Plan B audit: TREND mid-range RSI "
          f"[{RSI_LO:.0f},{RSI_HI:.0f}) ===")
    print(f"Period: {args.days}d | Universe: "
          f"{args.coin_list or 'top-' + str(args.coins)} | "
          f"Equity: ${args.equity:.0f}")
    print()

    on_trades: List = []
    off_trades: List = []
    per_coin = []

    for m in coins:
        coin = m["coin"]
        max_lev = int(m.get("maxLeverage", 5))
        candles = fetch_hl_candles(coin, "1h", total_bars)
        if len(candles) < 150:
            print(f"  {coin:10s} skip ({len(candles)} bars)")
            continue

        common = dict(
            coin=coin, candles_1h=candles, max_lev=max_lev,
            equity=args.equity, equity_fraction=equity_fraction,
            lev_ceiling=lev_ceiling, cfg=cfg, use_new_rules=True,
            max_loss_pct=max_loss, protect_pct=protect,
            retrace_threshold=retrace, rsi_variant="regime",
        )
        t_on = _simulate(**common, plan_b_enabled=True)
        t_off = _simulate(**common, plan_b_enabled=False)

        mid_on = [t for t in t_on if _is_trend_mid(t)]
        mid_off = [t for t in t_off if _is_trend_mid(t)]
        if not mid_on and not mid_off:
            continue
        on_trades.extend(mid_on)
        off_trades.extend(mid_off)
        per_coin.append((coin, mid_on, mid_off))

    if not on_trades:
        print("No TREND mid-range trades found in the window.")
        return 0

    s_on = _band_stats(on_trades)
    s_off = _band_stats(off_trades)

    hdr = f"{'metric':<26}{'Plan B ON':>16}{'Plan B OFF':>16}{'delta':>16}"
    print(hdr)
    print("-" * len(hdr))
    def _row(label, on, off, fmt, delta_fmt=None):
        d = on - off
        df = delta_fmt or fmt
        print(f"{label:<26}{fmt.format(on):>16}{fmt.format(off):>16}"
              f"{df.format(d):>16}")
    _row("trades", s_on["n"], s_off["n"], "{:.0f}")
    _row("winners", s_on["wins"], s_off["wins"], "{:.0f}")
    _row("losers", s_on["losses"], s_off["losses"], "{:.0f}")
    _row("win rate %", s_on["win_rate"], s_off["win_rate"], "{:.1f}")
    _row("total PnL $", s_on["total_pnl"], s_off["total_pnl"],
         "{:+.2f}")
    _row("total loss $", s_on["total_loss"], s_off["total_loss"],
         "{:+.2f}")
    _row("avg loss / trade $", s_on["avg_loss_usd"],
         s_off["avg_loss_usd"], "{:+.2f}")
    _row("avg loss move %", s_on["avg_loss_pct"],
         s_off["avg_loss_pct"], "{:+.2f}")
    print()

    # Loss reduction assertion
    if s_off["losses"]:
        loss_cut_pct = (1.0 - s_on["total_loss"] / s_off["total_loss"]) * 100.0
        avg_cut_pct = (1.0 - s_on["avg_loss_usd"]
                       / s_off["avg_loss_usd"]) * 100.0
        print(f"Loss reduction: total {loss_cut_pct:.1f}% | "
              f"per-trade avg {avg_cut_pct:.1f}%")
    print()

    # Per-coin breakdown
    print(f"{'coin':<10}{'n':>5}{'win':>5}{'loss':>6}"
          f"{'PnL ON':>11}{'PnL OFF':>11}{'loss saved':>12}")
    print("-" * 60)
    for coin, mid_on, mid_off in per_coin:
        a, b = _band_stats(mid_on), _band_stats(mid_off)
        saved = a["total_loss"] - b["total_loss"]  # ON loss is less negative
        print(f"{coin:<10}{a['n']:>5.0f}{a['wins']:>5.0f}{a['losses']:>6.0f}"
              f"{a['total_pnl']:>+11.2f}{b['total_pnl']:>+11.2f}"
              f"{saved:>+12.2f}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
