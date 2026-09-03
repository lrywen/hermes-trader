#!/usr/bin/env python3
"""A/B backtest: old breakout (close-only) vs new breakout (RVOL + ATR score).

Fast vectorized version: indicators are precomputed once per coin, then we
walk the bar series with the DSL exit engine.
"""
from __future__ import annotations

import math
import os
import sys
from dataclasses import dataclass
from pathlib import Path

os.environ["HERMES_BACKTEST"] = "1"
_REPO = Path(__file__).resolve().parents[1]
_env = _REPO / ".env.local"
if _env.is_file():
    for line in _env.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            if k.strip() == "HYPERLIQUID_PRIVATE_KEY": continue
            os.environ.setdefault(k.strip(), v.strip())
sys.path.insert(0, str(_REPO))

from hermes_trader.agents.config import get_config
from hermes_trader.client.hl_client import fetch_hl_candles
from hermes_trader.client.universe import get_universe
from hermes_trader.indicators import math as ind
from hermes_trader.indicators import triggers as trig
from hermes_trader.indicators.math import candle_val

ROUND_TRIP_FEE_BPS = 5.0


def _old_breakout(candles, lookback=48):
    if len(candles) < lookback + 2:
        return {"name": "breakout", "score": 0, "reason": "flat", "fired": False}
    current = candles[-1]
    ps = len(candles) - lookback - 1; pe = len(candles) - 1
    ph = float("-inf"); pl = float("inf")
    for i in range(ps, pe):
        if candle_val(candles[i], "h") > ph: ph = candle_val(candles[i], "h")
        if candle_val(candles[i], "l") < pl: pl = candle_val(candles[i], "l")
    cc = candle_val(current, "c")
    if cc > ph:
        pct = (cc - ph) / ph * 100
        return {"name": "breakout", "score": min(10, max(0, pct)),
                "reason": f"breakout above {lookback}-bar high", "fired": True}
    if cc < pl:
        pct = (pl - cc) / pl * 100
        return {"name": "breakout", "score": min(10, max(0, pct)),
                "reason": f"breakout below {lookback}-bar low", "fired": True}
    du = ph - cc; dd = cc - pl; closest = min(du, dd); rng = ph - pl
    score = max(0, (1 - closest / rng)) * 5 if rng > 0 else 0
    return {"name": "breakout", "score": score, "reason": "inside range", "fired": False}


@dataclass
class Trade:
    coin: str; side: str; entry_bar: int; entry_px: float; notional: float
    exit_bar: int = 0; exit_px: float = 0.0; pnl_usd: float = 0.0
    exit_reason: str = ""; entry_reason: str = ""


class DSL:
    __slots__ = ("side","entry_px","entry_bar","peak_px","max_loss","protect","retrace")
    def __init__(self, side, entry_px, entry_bar, max_loss=2.5, protect=1.5, retrace=0.30):
        self.side=side; self.entry_px=entry_px; self.entry_bar=entry_bar
        self.peak_px=entry_px; self.max_loss=max_loss; self.protect=protect; self.retrace=retrace

    def check(self, idx, bar):
        is_long = self.side == "long"
        if is_long and bar.h > self.peak_px: self.peak_px = bar.h
        if not is_long and bar.l < self.peak_px: self.peak_px = bar.l
        if is_long and bar.l <= self.entry_px*(1-self.max_loss/100):
            return True, self.entry_px*(1-self.max_loss/100), "max_loss"
        if not is_long and bar.h >= self.entry_px*(1+self.max_loss/100):
            return True, self.entry_px*(1+self.max_loss/100), "max_loss"
        if is_long:
            pp = (self.peak_px-self.entry_px)/self.entry_px*100
            if pp >= self.protect:
                floor = self.entry_px + (self.peak_px-self.entry_px)*(1-self.retrace)
                if bar.l <= floor: return True, floor, "trailing_stop"
        else:
            pp = (self.entry_px-self.peak_px)/self.entry_px*100
            if pp >= self.protect:
                ceil = self.entry_px - (self.entry_px-self.peak_px)*(1-self.retrace)
                if bar.h >= ceil: return True, ceil, "trailing_stop"
        return False, 0.0, ""


def _precompute(candles, cfg):
    """Precompute the per-bar indicator arrays used by triggers + trend filter."""
    n = len(candles)
    closes = [c.c for c in candles]
    th = cfg["thresholds"]
    # pct_move_spike needs returns; volume_spike needs volumes; both use z over 96/20.
    # EMA(8/21) for trend, ATR(14), ADX(14)
    ema8 = ind.ema(closes, 8); ema21 = ind.ema(closes, 21)
    atr_series = ind.atr(candles, 14)
    adx_series = ind.adx(candles, 14)
    # SMA(ATR(14), 20) for the ATR-environment gate (item #7).
    atr_sma20 = [float("nan")] * n
    for i in range(19, n):
        window = atr_series[i - 19:i + 1]
        vals = [v for v in window if isinstance(v, (int, float)) and math.isfinite(v)]
        if len(vals) == 20:
            atr_sma20[i] = sum(vals) / 20
    return closes, ema8, ema21, atr_series, adx_series, atr_sma20


def _eval_bar(i, candles, closes, cfg, mode, ema8, ema21, atr_series, adx_series, atr_sma20):
    """Evaluate triggers on the window up to and including bar i.

    mode: "old" (close-only breakout), "new" (RVOL+ATR), or "new_atr"
    (RVOL+ATR + ATR-environment gate: breakout only fires when ATR(14) >
    SMA(ATR(14),20), i.e. vol is expanding vs its own recent baseline).
    """
    th = cfg["thresholds"]; w = cfg["weights"]
    window = candles[:i+1]
    if mode == "old":
        bo = _old_breakout
        bo_hit = bo(window, th["breakoutLookback"])
    else:
        bo_hit = trig.breakout(
            window, th["breakoutLookback"],
            min_rvol=th.get("breakoutMinRvol", 1.5),
            rvol_window=th.get("breakoutRvolWindow", 20),
            atr_score_mult=th.get("breakoutAtrScoreMult", 3.0),
        )
        if mode == "new_atr" and bo_hit["fired"]:
            cur_atr = atr_series[i]
            avg_atr = atr_sma20[i]
            if not (math.isfinite(cur_atr) and math.isfinite(avg_atr)
                    and avg_atr > 0 and cur_atr > avg_atr):
                # low-vol squeeze: structurally outside the range but the
                # volatility regime hasn't expanded — suppress the fire.
                bo_hit = dict(bo_hit)
                bo_hit["fired"] = False
                bo_hit["score"] = bo_hit["score"] * 0.5
                bo_hit["reason"] = bo_hit["reason"] + " [low-vol squeeze: ATR<=SMA20]"
    hits = [
        trig.pct_move_spike(window, th["sigmaThreshold"]),
        trig.volume_spike(window, th["sigmaThreshold"]),
        bo_hit,
        trig.range_compression(window, th["bbLength"], th["bbStdDev"]),
        trig.trend_strength(window, th["adxPeriod"]),
        trig.momentum_burst(window, th["momentumLookback"], th["momentumPct"]),
    ]
    score = trig.composite_score(hits, w)
    e8, e21 = ema8[i], ema21[i]
    bullish = e8 > e21 if (math.isfinite(e8) and math.isfinite(e21)) else None
    a = atr_series[i]; c = closes[i]
    atr_pct = a/c*100 if (math.isfinite(a) and c > 0) else None
    adx14 = adx_series[i] if math.isfinite(adx_series[i]) else 0
    bo_fired = any(h["name"]=="breakout" and h["fired"] for h in hits)
    burst = any(h["name"]=="momentumBurst" and h["fired"] for h in hits)
    return score, hits, bullish, atr_pct, adx14, bo_fired, burst


def _simulate(coin, candles, cfg, mode, equity=100.0, fraction=0.10, lev=10, warmup=100):
    closes, ema8, ema21, atr_series, adx_series, atr_sma20 = _precompute(candles, cfg)
    trades = []; open_t = None; open_dsl = None
    fee = ROUND_TRIP_FEE_BPS / 10000.0
    for i in range(warmup, len(candles)-1):
        bar = candles[i]; nxt = candles[i+1]
        if open_t and open_dsl:
            done, exit_px, reason = open_dsl.check(i, bar)
            if done:
                g = ((exit_px-open_t.entry_px)/open_t.entry_px if open_t.side=="long"
                     else (open_t.entry_px-exit_px)/open_t.entry_px)
                open_t.exit_bar=i; open_t.exit_px=exit_px
                open_t.pnl_usd=open_t.notional*(g-fee); open_t.exit_reason=reason
                trades.append(open_t); open_t=open_dsl=None
            else:
                continue
        score, hits, bullish, atr_pct, adx14, bo_fired, burst = _eval_bar(
            i, candles, closes, cfg, mode,
            ema8, ema21, atr_series, adx_series, atr_sma20)
        if bullish is None: continue
        # heuristic verdict
        if not (score >= 25 or (atr_pct is not None and atr_pct >= 0.4) or burst):
            continue
        # TA proxy
        ta_ok = score >= 30 or burst or (atr_pct is not None and atr_pct >= 0.5 and adx14 >= 25)
        if not ta_ok: continue
        side = "long" if bullish else "short"
        notional = equity*fraction*lev
        open_t = Trade(coin=coin, side=side, entry_bar=i+1, entry_px=nxt.o,
                       notional=notional, entry_reason="breakout" if bo_fired else "other")
        open_dsl = DSL(side=side, entry_px=nxt.o, entry_bar=i+1)
    return trades


def _summarize(label, trades, equity):
    n = len(trades)
    print(f"\n{'='*64}\n  {label}\n{'='*64}")
    if n == 0:
        print("  no trades"); return {"n":0,"winrate":0,"pnl":0,"bo":0,"bo_wr":0}
    wins = [t for t in trades if t.pnl_usd > 0]
    losses = [t for t in trades if t.pnl_usd < 0]
    pnl = sum(t.pnl_usd for t in trades)
    aw = sum(t.pnl_usd for t in wins)/len(wins) if wins else 0
    al = sum(t.pnl_usd for t in losses)/len(losses) if losses else 0
    bo = [t for t in trades if t.entry_reason=="breakout"]
    bo_wins = [t for t in bo if t.pnl_usd > 0]
    other = [t for t in trades if t.entry_reason=="other"]
    other_wins = [t for t in other if t.pnl_usd > 0]
    print(f"  total trades        : {n}")
    print(f"  win rate            : {len(wins)}/{n} = {len(wins)/n*100:.1f}%")
    print(f"  avg win / avg loss  : ${aw:+.2f} / ${al:+.2f}")
    print(f"  total PnL           : ${pnl:+.2f}  ({pnl/equity*100:+.1f}% on ${equity:.0f})")
    print(f"  expectancy/trade    : ${pnl/n:+.3f}")
    if bo:
        print(f"  breakout trades     : {len(bo)}  win {len(bo_wins)}/{len(bo)} "
              f"= {len(bo_wins)/len(bo)*100:.1f}%  PnL ${sum(t.pnl_usd for t in bo):+.2f}")
    if other:
        print(f"  other-trigger trades: {len(other)}  win {len(other_wins)}/{len(other)} "
              f"= {len(other_wins)/len(other)*100:.1f}%  PnL ${sum(t.pnl_usd for t in other):+.2f}")
    reasons = {}
    for t in trades: reasons[t.exit_reason] = reasons.get(t.exit_reason,0)+1
    print(f"  exit reasons        : {reasons}")
    return {"n":n,"winrate":len(wins)/n*100,"pnl":pnl,
            "bo":len(bo),"bo_wr":(len(bo_wins)/len(bo)*100 if bo else 0)}


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--coins", type=int, default=10)
    ap.add_argument("--interval", default="5m")
    ap.add_argument("--equity", type=float, default=100.0)
    args = ap.parse_args()

    bpd = {"5m":288,"15m":96,"1h":24}[args.interval]
    total_bars = args.days*bpd + 120
    cfg = get_config()
    universe = get_universe()
    perps = [m for m in universe if m["type"]=="perp" and not m["coin"].startswith("@")]
    coins = sorted(perps, key=lambda m: m.get("dayNtlVlm",0), reverse=True)[:args.coins]

    print("=== hermes-trader A/B/C breakout backtest ===")
    print(f"period: {args.days}d  interval: {args.interval}  coins: top-{args.coins}")
    print("A = OLD breakout (close-only, pct score)")
    print("B = NEW breakout (RVOL>=1.5 + ATR-normalized score)")
    print("C = NEW + ATR-env gate (breakout only fires when ATR(14)>SMA(ATR,20))\n")

    old_all, new_all, atr_all = [], [], []
    for m in coins:
        coin = m["coin"]
        try:
            candles = fetch_hl_candles(coin, args.interval, total_bars)
            if len(candles) < 120:
                print(f"  {coin:8} skip ({len(candles)} bars)"); continue
            ot = _simulate(coin, candles, cfg, "old", args.equity)
            nt = _simulate(coin, candles, cfg, "new", args.equity)
            ct = _simulate(coin, candles, cfg, "new_atr", args.equity)
            op = sum(t.pnl_usd for t in ot)
            npp = sum(t.pnl_usd for t in nt)
            cpp = sum(t.pnl_usd for t in ct)
            print(f"  {coin:8} OLD:{len(ot):3}t/${op:+6.2f}  "
                  f"NEW:{len(nt):3}t/${npp:+6.2f}  "
                  f"ATR-gate:{len(ct):3}t/${cpp:+6.2f}")
            old_all.extend(ot); new_all.extend(nt); atr_all.extend(ct)
        except Exception as e:
            import traceback; traceback.print_exc()  # noqa: I001  (P1-2 baseline: inline debug import in analysis script)
            print(f"  {coin:8} error: {e}")

    a = _summarize("A: OLD breakout (close-only)", old_all, args.equity)
    b = _summarize("B: NEW breakout (RVOL + ATR)", new_all, args.equity)
    c = _summarize("C: NEW + ATR-environment gate", atr_all, args.equity)

    print(f"\n{'='*64}\n  DELTA\n{'='*64}")
    print("  B - A (RVOL+ATR vs old):")
    print(f"    trades         : {b['n']-a['n']:+d}  ({a['n']} -> {b['n']})")
    if a['n'] and b['n']:
        print(f"    win rate       : {b['winrate']-a['winrate']:+.1f} pp  "
              f"({a['winrate']:.1f}% -> {b['winrate']:.1f}%)")
    print(f"    PnL            : ${b['pnl']-a['pnl']:+.2f}  "
          f"(${a['pnl']:+.2f} -> ${b['pnl']:+.2f})")
    print(f"    breakout trades: {b['bo']-a['bo']:+d}  ({a['bo']} -> {b['bo']})")
    print("  C - B (ATR-env gate vs RVOL+ATR):")
    print(f"    trades         : {c['n']-b['n']:+d}  ({b['n']} -> {c['n']})")
    if b['n'] and c['n']:
        print(f"    win rate       : {c['winrate']-b['winrate']:+.1f} pp  "
              f"({b['winrate']:.1f}% -> {c['winrate']:.1f}%)")
    print(f"    PnL            : ${c['pnl']-b['pnl']:+.2f}  "
          f"(${b['pnl']:+.2f} -> ${c['pnl']:+.2f})")
    print(f"    breakout trades: {c['bo']-b['bo']:+d}  ({b['bo']} -> {c['bo']})")
    print("  C - A (ATR-env gate vs old):")
    print(f"    PnL            : ${c['pnl']-a['pnl']:+.2f}  "
          f"(${a['pnl']:+.2f} -> ${c['pnl']:+.2f})")
    print(f"\nCaveats: AI verdict substituted by heuristic; no funding/slippage beyond "
          f"{ROUND_TRIP_FEE_BPS}bps; 1h triggers omitted; {args.days}-day sample.")


if __name__ == "__main__":
    main()
