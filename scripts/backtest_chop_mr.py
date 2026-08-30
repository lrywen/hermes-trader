#!/usr/bin/env python3
"""Standalone backtest: chop-regime mean-reversion LONG strategy.

Entry (all on 1h, coin-self):
  - ADX(14) < adx_max          (default 20 — ranging market)
  - RSI(14) < rsi_long         (default 30 — oversold)
  - close > EMA21 * ema_floor  (default 0.98 — not in freefall)

Exit (fixed TP/SL/timeout):
  - target_pct take-profit     (default 1.5%)
  - stop_pct stop-loss         (default 1.0%)
  - timeout_bars hard timeout  (default 24h)

Sizing matches live config: equity_fraction * leverage * equity, with a
5 bps round-trip fee.

This is intentionally independent of backtest_ab_compare.py so the MR
alpha can be validated in isolation without trend-entry coupling.

Usage:
    python3 scripts/backtest_chop_mr.py
    python3 scripts/backtest_chop_mr.py --days 30 --coins 25
    python3 scripts/backtest_chop_mr.py --rsi-long 25 --target 2.0 --stop 1.0
"""
from __future__ import annotations

import argparse
import math
import os
import sys
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

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

from hermes_trader.agents.config import get_config  # noqa: E402
from hermes_trader.client.hl_client import fetch_hl_candles  # noqa: E402
from hermes_trader.client.universe import get_universe  # noqa: E402
from hermes_trader.indicators import math as ind  # noqa: E402
from hermes_trader.models.types import Candle  # noqa: E402

# Canonical regime classifier shared with backtest_ab_compare.py — single
# source of truth for _REGIME_TABLE / weights / OBV slope.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from backtest_ab_compare import (  # noqa: E402
    RegimeParams,
    _REGIME_TABLE,
    _obv_slope,
    _regime_score,
)

ROUND_TRIP_FEE_BPS = 5.0


# ---------------------------------------------------------------------------
# Indicator helpers (local copies to keep script self-contained)
# ---------------------------------------------------------------------------

def _ema_val(closes: List[float], period: int) -> Optional[float]:
    if len(closes) < period:
        return None
    arr = ind.ema(closes, period)
    v = arr[-1]
    return v if math.isfinite(v) else None


def _atr_val(window: List[Candle], period: int = 14) -> Optional[float]:
    if len(window) < period + 1:
        return None
    arr = ind.atr(window, period)
    v = arr[-1]
    return v if math.isfinite(v) else None


def _adx_val(window: List[Candle], period: int = 14) -> Optional[float]:
    if len(window) < period * 2:
        return None
    arr = ind.adx(window, period)
    for v in reversed(arr):
        if v == v and v != float("inf"):
            return v
    return None


# ---------------------------------------------------------------------------
# Trade record + fixed TP/SL exit simulation
# ---------------------------------------------------------------------------

@dataclass
class MRTrade:
    coin: str
    entry_bar: int
    entry_px: float
    rsi: float
    adx: float
    ema_dist_pct: float
    atr_pct: float
    notional: float
    exit_bar: int = 0
    exit_px: float = 0.0
    pnl_usd: float = 0.0
    gross_pct: float = 0.0
    exit_reason: str = ""
    bars_held: int = 0
    regime_label: str = ""
    regime_score: float = 0.0


def _simulate_exit(
    candles: List[Candle],
    entry_bar: int,
    entry_px: float,
    *,
    target_pct: float,
    stop_pct: float,
    timeout_bars: int,
    min_hold_bars: int = 0,
) -> Tuple[float, str, int]:
    """Return (gross_pct_after_fee, exit_reason, bars_held).

    Intra-bar priority: take-profit is checked first (optimistic), then
    stop-loss. During the first ``min_hold_bars`` bars the stop-loss is
    evaluated at the bar *close* rather than the intra-bar low — this
    filters out wick-outs that typically happen in the first 1-3 hours
    after a mean-reversion entry. Take-profit remains active intra-bar
    throughout.
    """
    fee = ROUND_TRIP_FEE_BPS / 10000.0
    tp_px = entry_px * (1 + target_pct / 100)
    sl_px = entry_px * (1 - stop_pct / 100)
    end = min(entry_bar + timeout_bars, len(candles) - 1)

    for j in range(entry_bar + 1, end + 1):
        b = candles[j]
        bars_held = j - entry_bar
        in_grace = bars_held <= min_hold_bars

        # Take-profit always active intra-bar.
        if b.h >= tp_px:
            return (tp_px - entry_px) / entry_px - fee, "target", bars_held

        # Stop-loss: close-only during grace period, intra-bar low after.
        if in_grace:
            if b.c <= sl_px:
                return (b.c - entry_px) / entry_px - fee, "stop_grace", bars_held
        else:
            if b.l <= sl_px:
                return (sl_px - entry_px) / entry_px - fee, "stop", bars_held

    # Timeout: exit at close of the last bar.
    last = candles[end]
    return (last.c - entry_px) / entry_px - fee, "timeout", end - entry_bar


# ---------------------------------------------------------------------------
# Per-coin backtest
# ---------------------------------------------------------------------------

def _backtest_coin(
    coin: str,
    candles: List[Candle],
    *,
    equity: float,
    equity_fraction: float,
    lev_ceiling: int,
    max_lev: int,
    adx_max: float,
    rsi_long: float,
    rsi_floor: float,
    ema_floor: float,
    target_pct: float,
    stop_pct: float,
    timeout_bars: int,
    min_hold_bars: int,
    warmup: int = 120,
    one_position: bool = True,
    use_regime: bool = False,
    mr_regimes: Optional[set] = None,
    require_bullish: bool = False,
) -> List[MRTrade]:
    trades: List[MRTrade] = []
    base_notional = equity * equity_fraction * min(lev_ceiling, max_lev)
    next_entry_bar = warmup  # used when one_position=True
    if mr_regimes is None:
        mr_regimes = {"CHOP"}

    for i in range(warmup, len(candles) - 1):
        if one_position and i < next_entry_bar:
            continue

        w = candles[: i + 1]
        closes = [c.c for c in w]

        adx_v = _adx_val(w)
        if adx_v is None:
            continue

        rsi_v = ind.rsi_last(closes, 14)
        if rsi_v is None or rsi_v >= rsi_long or rsi_v < rsi_floor:
            continue

        e8 = _ema_val(closes, 8)
        e21 = _ema_val(closes, 21)
        atr_v = _atr_val(w)
        if e21 is None or atr_v is None or atr_v <= 0:
            continue

        px = closes[-1]
        if px <= e21 * ema_floor:
            continue

        # --- Plan D: bullish trend confirmation (EMA8 > EMA21) ---
        # Dip-buying in an established uptrend has the best MR edge;
        # reject longs when the fast MA is below the slow MA.
        if require_bullish and not (e8 is not None and e8 > e21):
            continue

        # --- Regime gating ---
        # In regime mode the per-coin, per-bar data-driven regime label
        # replaces both the static coin blacklist and the hard ADX<adx_max
        # gate.  The MR overlay only fires when the coin is in one of the
        # configured mr_regimes (CHOP by default); trend regimes are
        # skipped because mean-reversion is counter-trend there.
        regime_label = "LEGACY"
        regime_score = 0.0
        size_mult = 1.0
        if use_regime:
            bullish = e8 is not None and e8 > e21
            obv_dir = _obv_slope(w)
            regime_score, regime_label = _regime_score(
                w, closes, e8, e21, atr_v, adx_v, obv_dir, bullish,
            )
            if regime_label not in mr_regimes:
                continue
            size_mult = _REGIME_TABLE[regime_label].size_mult
        else:
            # Legacy hard ADX cap (independent backtest).
            if adx_v >= adx_max:
                continue

        notional = base_notional * size_mult

        # Signal confirmed at close; enter on next bar's open.
        entry_bar = i + 1
        if entry_bar >= len(candles):
            continue
        entry_px = candles[entry_bar].o
        if entry_px <= 0:
            continue

        gross, reason, bars_held = _simulate_exit(
            candles, entry_bar, entry_px,
            target_pct=target_pct, stop_pct=stop_pct,
            timeout_bars=timeout_bars, min_hold_bars=min_hold_bars,
        )
        ema_dist = (px - e21) / e21 * 100
        atr_pct = atr_v / px * 100

        t = MRTrade(
            coin=coin, entry_bar=entry_bar, entry_px=entry_px,
            rsi=rsi_v, adx=adx_v, ema_dist_pct=ema_dist, atr_pct=atr_pct,
            notional=notional, exit_bar=entry_bar + bars_held,
            exit_px=entry_px * (1 + gross + ROUND_TRIP_FEE_BPS / 10000.0),
            pnl_usd=notional * gross, gross_pct=gross * 100,
            exit_reason=reason, bars_held=bars_held,
            regime_label=regime_label, regime_score=regime_score,
        )
        trades.append(t)

        if one_position:
            next_entry_bar = entry_bar + bars_held + 1

    return trades


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def _print_report(
    trades: List[MRTrade],
    args: argparse.Namespace,
    coins_used: int,
) -> None:
    n = len(trades)
    days = args.days

    print("\n" + "=" * 78)
    if getattr(args, "regime", False):
        rsi_band = f"RSI {args.rsi_floor:g}-{args.rsi_long:g}"
        print(f"  CHOP MR LONG BACKTEST — REGIME mode (MR active in: "
              f"{', '.join(sorted(args.mr_regimes.split(',')))})")
        print(f"  Filters: {rsi_band}, px>EMA21*{args.ema_floor}, "
              f"regime-score gate")
    else:
        rsi_band = f"RSI {args.rsi_floor:g}-{args.rsi_long:g}"
        print(f"  CHOP MR LONG BACKTEST — ADX<{args.adx_max}, {rsi_band}, "
              f"px>EMA21*{args.ema_floor}")
    print(f"  Exit: {args.target}% TP / {args.stop}% SL / "
          f"{args.timeout}h timeout / min-hold {args.min_hold}h "
          f"| Fee: {ROUND_TRIP_FEE_BPS} bps round-trip")
    if args.exclude:
        print(f"  Excluded coins: {', '.join(args.exclude)}")
    print("=" * 78)

    if n == 0:
        print("  No trades generated.")
        return

    wins = [t for t in trades if t.pnl_usd > 0]
    losses = [t for t in trades if t.pnl_usd < 0]
    scratch = [t for t in trades if t.pnl_usd == 0]
    pnl = sum(t.pnl_usd for t in trades)
    wr = len(wins) / n * 100
    avg_win = sum(t.pnl_usd for t in wins) / len(wins) if wins else 0.0
    avg_loss = sum(t.pnl_usd for t in losses) / len(losses) if losses else 0.0
    payoff = abs(avg_win / avg_loss) if avg_loss != 0 else float("inf")
    expectancy = pnl / n
    notional = trades[0].notional
    roi_on_equity = pnl / args.equity * 100

    # Per-day stats
    trades_per_day = n / days
    pnl_per_day = pnl / days

    print(f"\n  Universe       : top-{coins_used} perp by volume")
    print(f"  Period         : {days} days ({n} signals, "
          f"{trades_per_day:.1f}/day)")
    print(f"  Equity/Notional: ${args.equity:.0f} / ${notional:.0f} "
          f"({args.equity_fraction:.0%} x {args.leverage}x)")
    print()
    print(f"  Total PnL      : ${pnl:+,.2f}  ({roi_on_equity:+.1f}% on equity)")
    print(f"  Avg PnL/day    : ${pnl_per_day:+,.2f}")
    print(f"  Win rate       : {wr:.1f}%  ({len(wins)}W / {len(losses)}L"
          + (f" / {len(scratch)} scratch)" if scratch else ")"))
    print(f"  Avg win        : ${avg_win:+,.2f}")
    print(f"  Avg loss       : ${avg_loss:+,.2f}")
    print(f"  Payoff ratio   : {payoff:.2f}")
    print(f"  Expectancy     : ${expectancy:+,.2f}/trade")

    # --- Distribution of gross returns ---
    gross_pcts = sorted(t.gross_pct for t in trades)
    median_pct = gross_pcts[n // 2]
    print(f"\n  Gross return   : min {gross_pcts[0]:+.2f}% | "
          f"median {median_pct:+.2f}% | max {gross_pcts[-1]:+.2f}%")

    # --- Exit reasons ---
    by_reason: Dict[str, List[MRTrade]] = defaultdict(list)
    for t in trades:
        by_reason[t.exit_reason].append(t)
    print(f"\n  Exit reasons:")
    for reason in ("target", "stop", "stop_grace", "timeout"):
        lst = by_reason.get(reason, [])
        if not lst:
            continue
        rw = sum(1 for t in lst if t.pnl_usd > 0)
        rp = sum(t.pnl_usd for t in lst)
        label = reason.replace("_", " ")
        print(f"    {label:>12}: {len(lst):>4} trades  "
              f"WR={rw / len(lst) * 100:5.1f}%  PnL=${rp:+8.2f}  "
              f"avg=${rp / len(lst):+6.2f}")

    # --- RSI buckets ---
    print(f"\n  RSI at entry buckets:")
    rsi_edges = [(0, 15, "<15"), (15, 20, "15-20"),
                 (20, 25, "20-25"), (25, 30, "25-30")]
    for lo, hi, label in rsi_edges:
        lst = [t for t in trades if lo <= t.rsi < hi]
        if not lst:
            continue
        w = sum(1 for t in lst if t.pnl_usd > 0)
        p = sum(t.pnl_usd for t in lst)
        print(f"    RSI {label:>6}: n={len(lst):>4}  WR={w / len(lst) * 100:5.1f}%  "
              f"PnL=${p:+8.2f}")

    # --- ADX buckets ---
    print(f"\n  ADX at entry buckets:")
    adx_edges = [(0, 10, "<10"), (10, 15, "10-15"),
                 (15, 20, "15-20")]
    for lo, hi, label in adx_edges:
        lst = [t for t in trades if lo <= t.adx < hi]
        if not lst:
            continue
        w = sum(1 for t in lst if t.pnl_usd > 0)
        p = sum(t.pnl_usd for t in lst)
        print(f"    ADX {label:>6}: n={len(lst):>4}  WR={w / len(lst) * 100:5.1f}%  "
              f"PnL=${p:+8.2f}")

    # --- Regime buckets (only when regime mode was used) ---
    regime_labels = {t.regime_label for t in trades if t.regime_label}
    if regime_labels and "LEGACY" not in regime_labels:
        print(f"\n  Regime label at entry:")
        for rl in ("CHOP", "NEUTRAL", "TREND", "STRONG_TREND"):
            lst = [t for t in trades if t.regime_label == rl]
            if not lst:
                continue
            w = sum(1 for t in lst if t.pnl_usd > 0)
            p = sum(t.pnl_usd for t in lst)
            avg_s = sum(t.regime_score for t in lst) / len(lst)
            print(f"    {rl:>14}: n={len(lst):>4}  WR={w / len(lst) * 100:5.1f}%  "
                  f"PnL=${p:+8.2f}  avg_score={avg_s:.3f}")

    # --- Hold-time buckets ---
    print(f"\n  Holding period (bars):")
    hold_edges = [(1, 3, "1-3h"), (4, 6, "4-6h"),
                  (7, 12, "7-12h"), (13, 24, "13-24h")]
    for lo, hi, label in hold_edges:
        lst = [t for t in trades if lo <= t.bars_held <= hi]
        if not lst:
            continue
        w = sum(1 for t in lst if t.pnl_usd > 0)
        p = sum(t.pnl_usd for t in lst)
        print(f"    {label:>6}: n={len(lst):>4}  WR={w / len(lst) * 100:5.1f}%  "
              f"PnL=${p:+8.2f}")

    # --- Per-coin breakdown (sorted by PnL desc) ---
    by_coin: Dict[str, List[MRTrade]] = defaultdict(list)
    for t in trades:
        by_coin[t.coin].append(t)
    ranked = sorted(by_coin.items(), key=lambda kv: -sum(t.pnl_usd for t in kv[1]))
    print(f"\n  Per-coin breakdown (sorted by PnL):")
    print(f"  {'coin':>10}  {'n':>4}  {'WR':>6}  {'PnL':>10}  {'avg':>8}")
    print(f"  {'-'*10}  {'-'*4}  {'-'*6}  {'-'*10}  {'-'*8}")
    for coin, lst in ranked:
        w = sum(1 for t in lst if t.pnl_usd > 0)
        p = sum(t.pnl_usd for t in lst)
        print(f"  {coin:>10}  {len(lst):>4}  {w / len(lst) * 100:>5.0f}%  "
              f"${p:>+9.2f}  ${p / len(lst):>+7.2f}")

    # --- Consecutive loss streaks (risk read) ---
    max_loss_streak = 0
    cur = 0
    for t in sorted(trades, key=lambda x: (x.coin, x.entry_bar)):
        if t.pnl_usd < 0:
            cur += 1
            max_loss_streak = max(max_loss_streak, cur)
        else:
            cur = 0
    print(f"\n  Max consecutive losses: {max_loss_streak}")

    # --- Verdict ---
    print(f"\n  {'='*74}")
    if pnl > 0 and wr >= 55 and payoff >= 0.8:
        print("  VERDICT: Strategy has positive expectancy in chop regime.")
    elif pnl > 0:
        print("  VERDICT: Marginal positive edge — consider tightening filters.")
    else:
        print("  VERDICT: No positive edge in current configuration.")
    print(f"  {'='*74}\n")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description="Chop-regime MR long backtest")
    ap.add_argument("--days", type=int, default=21)
    ap.add_argument("--coins", type=int, default=20)
    ap.add_argument("--equity", type=float, default=200.0)
    ap.add_argument("--equity-fraction", type=float, default=None,
                    help="Fraction of equity per trade (default from live config)")
    ap.add_argument("--leverage", type=int, default=None,
                    help="Leverage ceiling (default from live config)")
    ap.add_argument("--adx-max", type=float, default=20.0,
                    help="Only enter when ADX < this (default 20)")
    ap.add_argument("--rsi-long", type=float, default=30.0,
                    help="Only enter long when RSI < this (default 30)")
    ap.add_argument("--rsi-floor", type=float, default=0.0,
                    help="Minimum RSI for entry; rejects extreme-oversold "
                         "free-falls (default 0 = disabled)")
    ap.add_argument("--ema-floor", type=float, default=0.98,
                    help="Minimum close/EMA21 ratio (default 0.98)")
    ap.add_argument("--target", type=float, default=1.5,
                    help="Take-profit %% (default 1.5)")
    ap.add_argument("--stop", type=float, default=1.0,
                    help="Stop-loss %% (default 1.0)")
    ap.add_argument("--timeout", type=int, default=24,
                    help="Hard timeout in 1h bars (default 24)")
    ap.add_argument("--min-hold", type=int, default=0,
                    help="For the first N 1h bars after entry, stop-loss is "
                         "checked only at bar close (filters wick-outs). "
                         "Take-profit remains active. Default 0 (disabled).")
    ap.add_argument("--exclude", type=str, default="",
                    help="Comma-separated coin symbols to exclude "
                         "(e.g. XRP,NEAR,CRV) — only used in legacy mode")
    ap.add_argument("--regime", action="store_true",
                    help="Use data-driven per-coin regime score instead of "
                         "hard ADX<adx_max. Replaces static --exclude.")
    ap.add_argument("--mr-regimes", type=str, default="CHOP",
                    help="Comma-separated regime labels where MR overlay is "
                         "active (default CHOP; e.g. CHOP,NEUTRAL)")
    ap.add_argument("--allow-overlap", action="store_true",
                    help="Allow overlapping positions (default: one position per coin)")
    ap.add_argument("--bullish", action="store_true",
                    help="Plan D: only enter MR long when EMA8 > EMA21 "
                         "(bullish trend confirmation)")
    args = ap.parse_args()

    # Read live config for sizing (matches main backtest).
    try:
        from hermes_trader.agents.config_store import read_agent_config
        live = read_agent_config() or {}
    except Exception:
        live = {}

    if args.equity_fraction is None:
        args.equity_fraction = float(live.get("equity_fraction_per_trade", 0.20))
    if args.leverage is None:
        args.leverage = int(live.get("leverage", 12))

    cfg = get_config()  # noqa: F841 — ensures thresholds module loads cleanly
    universe = get_universe()
    excluded = {s.strip().upper() for s in args.exclude.split(",") if s.strip()}
    perps = [m for m in universe
             if m.get("type") == "perp" and not m["coin"].startswith("@")
             and m["coin"].upper() not in excluded]
    top = sorted(perps, key=lambda m: m.get("dayNtlVlm", 0), reverse=True)[: args.coins]

    mr_regimes = {s.strip().upper() for s in args.mr_regimes.split(",") if s.strip()}
    if args.regime:
        unknown = mr_regimes - set(_REGIME_TABLE.keys())
        if unknown:
            print(f"  ERROR: unknown regime label(s): {', '.join(sorted(unknown))}")
            print(f"         valid labels: {', '.join(_REGIME_TABLE.keys())}")
            return 2

    total_bars = args.days * 24 + 150
    all_trades: List[MRTrade] = []
    skipped = 0

    for m in top:
        coin = m["coin"]
        max_lev = int(m.get("maxLeverage", 5))
        try:
            candles = fetch_hl_candles(coin, "1h", total_bars)
        except Exception as e:
            print(f"  {coin:10s} fetch error: {e}")
            skipped += 1
            continue
        if not candles or len(candles) < 150:
            print(f"  {coin:10s} skip ({len(candles) if candles else 0} bars)")
            skipped += 1
            continue

        ct = _backtest_coin(
            coin, candles,
            equity=args.equity,
            equity_fraction=args.equity_fraction,
            lev_ceiling=args.leverage,
            max_lev=max_lev,
            adx_max=args.adx_max,
            rsi_long=args.rsi_long,
            rsi_floor=args.rsi_floor,
            ema_floor=args.ema_floor,
            target_pct=args.target,
            stop_pct=args.stop,
            timeout_bars=args.timeout,
            min_hold_bars=args.min_hold,
            one_position=not args.allow_overlap,
            use_regime=args.regime,
            mr_regimes=mr_regimes,
            require_bullish=args.bullish,
        )
        cp = sum(t.pnl_usd for t in ct)
        cw = sum(1 for t in ct if t.pnl_usd > 0)
        print(f"  {coin:10s}  {len(ct):>3} trades  {cw:>3}W  "
              f"WR={(cw / len(ct) * 100) if ct else 0:5.1f}%  PnL=${cp:+8.2f}")
        all_trades.extend(ct)

    _print_report(all_trades, args, coins_used=len(top) - skipped)
    return 0


if __name__ == "__main__":
    sys.exit(main())
