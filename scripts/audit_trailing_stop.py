#!/usr/bin/env python3
"""Audit: TREND / STRONG_TREND trailing_stop profit/hold-time analysis.

Runs the REGIME backtest, isolates TREND and STRONG_TREND trades that exited via
trailing_stop (floor_breach), and replays intra-trade candles to compute:
  - realized gross % at exit
  - MFE  (max favorable excursion, peak unrealized profit %)
  - MAE  (max adverse excursion, peak drawdown %)
  - bars held
  - peak-to-exit giveback (MFE - realized)
  - capture ratio (realized / MFE) -> is the stop too conservative?

Both labels map onto production regime "up"/"down" (trend-ride exit params).
This is the BACKTEST proxy for the question "is the trailing stop cutting
TREND winners short?" For LIVE production data, parse the `[dsl:exit_stats]`
log lines emitted by trading_loop.py with analyze_exit_stats.py.

Usage:
    python3 scripts/audit_trailing_stop.py
    python3 scripts/audit_trailing_stop.py --days 30 --coins 20
"""
from __future__ import annotations

import argparse
import os
import statistics
import sys
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Optional

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
from hermes_trader.models.types import Candle  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
from backtest_ab_compare import Trade, _simulate, ROUND_TRIP_FEE_BPS  # noqa: E402


def _mfe_mae(candles: List[Candle], t: Trade) -> Dict[str, float]:
    """Replay holding period to compute MFE/MAE in gross %."""
    is_long = t.side == "long"
    entry = t.entry_px
    mfe = 0.0
    mae = 0.0
    for j in range(t.entry_bar, t.exit_bar + 1):
        if j >= len(candles):
            break
        b = candles[j]
        if is_long:
            hi_pct = (b.h - entry) / entry * 100
            lo_pct = (b.l - entry) / entry * 100
            mfe = max(mfe, hi_pct)
            mae = min(mae, lo_pct)
        else:
            hi_pct = (entry - b.l) / entry * 100
            lo_pct = (entry - b.h) / entry * 100
            mfe = max(mfe, hi_pct)
            mae = min(mae, lo_pct)
    return {"mfe": mfe, "mae": mae}


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
        print(f"  {label:<28}: n=0")
        return
    print(f"  {label:<28}: n={len(values):>4}  "
          f"mean={fmt.format(statistics.mean(values))}  "
          f"median={fmt.format(statistics.median(values))}  "
          f"p25={fmt.format(_pctile(values, 0.25))}  "
          f"p75={fmt.format(_pctile(values, 0.75))}  "
          f"min={fmt.format(min(values))}  "
          f"max={fmt.format(max(values))}")


# pnl_usd = notional * (gross_pct - fee_pct); reconstruct gross_pct (%, fee stripped)
_FEE_PCT = ROUND_TRIP_FEE_BPS / 10000.0 * 100.0


def _realized_pct(t: Trade) -> float:
    return (t.pnl_usd / t.notional) * 100.0 + _FEE_PCT


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
    coin_candles: Dict[str, List[Candle]] = {}

    print(f"Running REGIME backtest: {args.days}d, {args.coins} coins")
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
        coin_candles[coin] = candles
        trades = _simulate(
            coin, candles, max_lev,
            equity=args.equity, equity_fraction=equity_fraction,
            lev_ceiling=leverage, cfg=cfg, use_new_rules=True,
            max_loss_pct=max_loss, protect_pct=protect,
            retrace_threshold=retrace, rsi_variant="regime",
        )
        all_trades.extend(trades)

    # ------------------------------------------------------------------
    # Overall exit-reason breakdown per regime
    # ------------------------------------------------------------------
    print("\n" + "=" * 90)
    print("  EXIT REASON BREAKDOWN BY REGIME")
    print("=" * 90)
    by_regime_reason: Dict[str, Dict[str, List[Trade]]] = defaultdict(
        lambda: defaultdict(list))
    for t in all_trades:
        by_regime_reason[t.regime_label][t.exit_reason].append(t)

    for regime in ("STRONG_TREND", "TREND", "NEUTRAL", "CHOP"):
        reasons = by_regime_reason.get(regime, {})
        total_r = sum(len(v) for v in reasons.values())
        if total_r == 0:
            continue
        print(f"\n  {regime} ({total_r} trades):")
        for reason, lst in sorted(reasons.items(), key=lambda kv: -len(kv[1])):
            rp = sum(t.pnl_usd for t in lst)
            rw = sum(1 for t in lst if t.pnl_usd > 0)
            print(f"    {reason:<22}: {len(lst):>4}  "
                  f"WR={rw/len(lst)*100:5.1f}%  PnL=${rp:>+9.2f}  "
                  f"avg=${rp/len(lst):>+6.2f}")

    # ------------------------------------------------------------------
    # TREND / STRONG_TREND trailing_stop deep dive
    # ------------------------------------------------------------------
    # Both label buckets map onto production regime "up"/"down" (trend-ride exit
    # params). Report each separately so we can see whether STRONG_TREND holds
    # are being cut short relative to plain TREND.
    for regime in ("STRONG_TREND", "TREND"):
        regime_trades = [t for t in all_trades if t.regime_label == regime]
        ts_trades = [t for t in regime_trades if t.exit_reason == "trailing_stop"]
        ml_trades = [t for t in regime_trades if "max_loss" in t.exit_reason]

        print("\n" + "=" * 90)
        print(f"  {regime} TRAILING_STOP DEEP DIVE  (protect={protect}%, "
              f"retrace={retrace*100:.0f}%, max_loss=0.8%)")
        print("=" * 90)

        if not ts_trades:
            print(f"  No trailing_stop exits in {regime}.")
            continue

        realized_pcts: List[float] = []
        mfes: List[float] = []
        maes: List[float] = []
        givebacks: List[float] = []
        holds: List[int] = []
        holds_win: List[int] = []
        holds_loss: List[int] = []
        by_hold_bucket: Dict[str, List[Trade]] = defaultdict(list)

        for t in ts_trades:
            candles = coin_candles[t.coin]
            exc = _mfe_mae(candles, t)
            realized = _realized_pct(t)
            realized_pcts.append(realized)
            mfes.append(exc["mfe"])
            maes.append(exc["mae"])
            givebacks.append(exc["mfe"] - realized)
            h = t.exit_bar - t.entry_bar
            holds.append(h)
            if t.pnl_usd > 0:
                holds_win.append(h)
            else:
                holds_loss.append(h)
            if h <= 3:
                by_hold_bucket["1-3h"].append(t)
            elif h <= 6:
                by_hold_bucket["4-6h"].append(t)
            elif h <= 12:
                by_hold_bucket["7-12h"].append(t)
            elif h <= 24:
                by_hold_bucket["13-24h"].append(t)
            else:
                by_hold_bucket[">24h"].append(t)

        print(f"\n  Sample size: {len(ts_trades)} trailing_stop exits "
              f"(of {len(regime_trades)} {regime} trades)")
        pnl_ts = sum(t.pnl_usd for t in ts_trades)
        wins_ts = sum(1 for t in ts_trades if t.pnl_usd > 0)
        print(f"  PnL from trailing_stop: ${pnl_ts:+.2f}  "
              f"WR={wins_ts/len(ts_trades)*100:.1f}%")

        print(f"\n  --- Profit distribution (directional %, fee stripped) ---")
        _summarize(realized_pcts, "Realized at exit")
        _summarize(mfes, "MFE (peak favorable)")
        _summarize(maes, "MAE (peak adverse)")
        _summarize(givebacks, "Peak-to-exit giveback")

        print(f"\n  --- Holding period (1h bars) ---")
        _summarize([float(h) for h in holds], "Bars held", fmt="{:.1f}")
        if holds_win:
            print(f"    winners: n={len(holds_win)}  median={statistics.median(holds_win):.1f}h  "
                  f"mean={statistics.mean(holds_win):.1f}h")
        if holds_loss:
            print(f"    losers : n={len(holds_loss)}  median={statistics.median(holds_loss):.1f}h  "
                  f"mean={statistics.mean(holds_loss):.1f}h")

        print(f"\n  --- Trailing_stop trades by hold bucket ---")
        print(f"  {'bucket':<8} {'n':>4} {'WR':>6} {'PnL':>10} {'avg MFE':>9} "
              f"{'avg real':>9} {'avg give':>9}")
        print(f"  {'-'*8} {'-'*4} {'-'*6} {'-'*10} {'-'*9} {'-'*9} {'-'*9}")
        for bucket in ("1-3h", "4-6h", "7-12h", "13-24h", ">24h"):
            lst = by_hold_bucket.get(bucket, [])
            if not lst:
                continue
            bmfe = []
            breal = []
            bgive = []
            for t in lst:
                candles = coin_candles[t.coin]
                exc = _mfe_mae(candles, t)
                r = _realized_pct(t)
                bmfe.append(exc["mfe"])
                breal.append(r)
                bgive.append(exc["mfe"] - r)
            bp = sum(t.pnl_usd for t in lst)
            bw = sum(1 for t in lst if t.pnl_usd > 0)
            print(f"  {bucket:<8} {len(lst):>4} {bw/len(lst)*100:5.1f}%  "
                  f"${bp:>+9.2f} {statistics.mean(bmfe):>+8.2f}% "
                  f"{statistics.mean(breal):>+8.2f}% {statistics.mean(bgive):>+8.2f}%")

        # ------------------------------------------------------------------
        # Comparison: trailing_stop vs max_loss in this regime
        # ------------------------------------------------------------------
        print(f"\n  --- Comparison: trailing_stop vs max_loss ({regime}) ---")
        for label, lst in [("trailing_stop", ts_trades), ("max_loss 0.8%", ml_trades)]:
            if not lst:
                continue
            p = sum(t.pnl_usd for t in lst)
            w = sum(1 for t in lst if t.pnl_usd > 0)
            avg_h = statistics.mean([t.exit_bar - t.entry_bar for t in lst])
            print(f"    {label:<18}: n={len(lst):>4}  WR={w/len(lst)*100:5.1f}%  "
                  f"PnL=${p:>+9.2f}  avg_hold={avg_h:.1f}h  "
                  f"avg_PnL=${p/len(lst):>+6.2f}")

        # ------------------------------------------------------------------
        # Verdict: is trailing stop too conservative?
        # ------------------------------------------------------------------
        print(f"\n  {'='*78}")
        avg_mfe = statistics.mean(mfes)
        avg_real = statistics.mean(realized_pcts)
        avg_give = statistics.mean(givebacks)
        capture_ratio = avg_real / avg_mfe * 100 if avg_mfe > 0 else 0
        avg_hold = statistics.mean(holds)
        print(f"  CAPTURE RATIO (realized/MFE): {capture_ratio:.1f}%  "
              f"(avg MFE {avg_mfe:.2f}% -> realized {avg_real:.2f}%, "
              f"giveback {avg_give:.2f}%)")
        print(f"  AVG HOLD: {avg_hold:.1f}h")

        if capture_ratio < 40:
            verdict = ("Trailing stop is TOO TIGHT — large MFE but only "
                       f"{capture_ratio:.0f}% captured. Consider raising retrace "
                       f"threshold or protect_pct.")
        elif capture_ratio < 60:
            verdict = ("Trailing stop is MODERATE — captures "
                       f"{capture_ratio:.0f}% of peak move. Tunable but not broken.")
        else:
            verdict = ("Trailing stop is HEALTHY — captures "
                       f"{capture_ratio:.0f}% of peak move.")
        print(f"  VERDICT: {verdict}")
        print(f"  {'='*78}\n")

    return 0


if __name__ == "__main__":
    sys.exit(main())
