#!/usr/bin/env python3
"""Offline (read-only) backtest: BTC 1h macro-regime EMA parameter comparison.

Compares three EMA pairs under ONE identical classifier (the production
``trend_from_closes`` rule: fast>slow AND fast slope over 8 bars > +0.2% for
'up', symmetric for 'down', else neutral; ADX<20 relabels neutral->chop):

  * 20/30   - current production
  * 20/50   - most common retail trend pair
  * 50/200  - daily-grade golden/death-cross style (on 1h = very slow)

Metrics
-------
1. flips            : up<->down direction changes (more = choppier)
2. whipsaw          : a fresh 'up'/'down' that flips back to neutral/opposite
                      within W bars and the forward H-bar move in the signalled
                      direction was <= 0 (a false trend call)
3. lag vs swing     : for each confirmed direction flip, how many bars AFTER the
                      nearest Donchian swing pivot (20-bar) confirmation fires;
                      positive = lagging
4. forward alignment : mean sign-aligned forward H-bar return while in up/down
                      (does the regime actually predict the next move?)
5. time-in-regime   : fraction of bars up/down/neutral/chop

No orders, no writes. Uses in-domain hl_client (auth/rate-limit aware).
"""
from __future__ import annotations

import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hermes_trader.client.hl_client import _http_post
from hermes_trader.indicators.math import adx, ema

COIN = "BTC"
INTERVAL_H = 1
BARS = 4000                 # ~166 days of 1h
SLOPE_BARS = 8
SLOPE_UP = 0.002
ADX_CHOP = 20.0
Whipsaw_W = 24              # bars: flip must survive this to be "real"
FWD_H = 24                  # forward 24h alignment
PIVOT = 20                  # Donchian swing pivot half-window


def fetch():
    end = int(time.time() * 1000)
    start = end - BARS * 3600_000
    p = {"type": "candleSnapshot",
         "req": {"coin": COIN, "interval": "1h",
                 "startTime": start, "endTime": end}}
    raw = _http_post("/info", p)
    if not isinstance(raw, list):
        raise SystemExit(f"candle fetch failed: {raw!r}")
    out = sorted(({"t": int(c["t"]), "o": float(c["o"]), "h": float(c["h"]),
                   "l": float(c["l"]), "c": float(c["c"])} for c in raw),
                 key=lambda x: x["t"])
    # drop forming bar
    if out and time.time() * 1000 < out[-1]["t"] + 3600_000:
        out = out[:-1]
    return out


class C:  # adx() needs h/l/c attrs
    __slots__ = ("h", "l", "c")
    def __init__(s, x):
        s.h, s.l, s.c = x["h"], x["l"], x["c"]


def classify(closes, fast_p, slow_p, adx_arr):
    """Per-bar regime series via production rule. warm-up = slow_p+slope."""
    fast = ema(closes, fast_p)
    slow = ema(closes, slow_p)
    n = len(closes)
    out = ["neutral"] * n
    for i in range(n):
        if i < slow_p + SLOPE_BARS:
            continue
        f = fast[i]; s = slow[i]; f0 = fast[i - SLOPE_BARS]
        slope = (f - f0) / abs(f0) if f0 else 0.0
        if f > s and slope > SLOPE_UP:
            r = "up"
        elif f < s and slope < -SLOPE_UP:
            r = "down"
        else:
            r = "neutral"
        if r == "neutral" and adx_arr[i] is not None and adx_arr[i] < ADX_CHOP:
            r = "chop"
        out[i] = r
    return out


def swing_pivots(highs, lows):
    """Return confirmed swing-low / swing-high indices (PIVOT each side)."""
    lows_i, highs_i = [], []
    n = len(highs)
    for i in range(PIVOT, n - PIVOT):
        if lows[i] == min(lows[i - PIVOT:i + PIVOT + 1]):
            lows_i.append(i)
        if highs[i] == max(highs[i - PIVOT:i + PIVOT + 1]):
            highs_i.append(i)
    return lows_i, highs_i


def evaluate(name, regime, closes, lows_i, highs_i):
    n = len(closes)
    warm = 200  # evaluate only after all EMAs (incl 200) warmed
    # --- flips + whipsaw + forward alignment ---
    flips = 0
    whipsaw = 0
    real_calls = 0
    align_vals = []
    lag = {"up": [], "down": []}
    prev = "neutral"
    for i in range(warm, n):
        r = regime[i]
        if r in ("up", "down") and r != prev and prev not in ("up", "down" if r == "up" else "down",):
            pass
        # count directional flips: entering up/down from a non-same-direction
        if r in ("up", "down") and prev != r:
            # is this a fresh DIRECTION entry (vs just re-entering same dir)?
            flips += 1
            # forward alignment
            if i + FWD_H < n:
                ret = (closes[i + FWD_H] - closes[i]) / closes[i]
                sret = ret if r == "up" else -ret
                align_vals.append(sret)
                real_calls += 1
                # whipsaw: within W bars regime leaves the direction AND fwd move<=0
                seg = regime[i:i + Whipsaw_W + 1]
                leaves = any(x != r for x in seg[1:])
                if leaves and sret <= 0:
                    whipsaw += 1
            # lag vs nearest swing pivot
            piv = lows_i if r == "up" else highs_i
            # most recent pivot strictly before i
            prior = [p for p in piv if p <= i]
            if prior:
                lag[r].append(i - prior[-1])
        prev = r

    # time in regime
    seg = regime[warm:]
    tot = len(seg)
    frac = {k: 100 * seg.count(k) / tot for k in ("up", "down", "neutral", "chop")}
    aligned = 100 * sum(1 for x in align_vals if x > 0) / len(align_vals) if align_vals else 0
    mean_align = statistics.mean(align_vals) if align_vals else 0
    wr = 100 * whipsaw / real_calls if real_calls else 0
    med_lag_up = statistics.median(lag["up"]) if lag["up"] else float("nan")
    med_lag_dn = statistics.median(lag["down"]) if lag["down"] else float("nan")
    return {
        "name": name, "flips": flips, "calls": real_calls,
        "whipsaw": whipsaw, "whipsaw_pct": wr,
        "align_pct": aligned, "mean_fwd": mean_align,
        "lag_up": med_lag_up, "lag_dn": med_lag_dn,
        "frac": frac,
    }


def main():
    cs = fetch()
    print(f"{COIN} 1h closed bars fetched: {len(cs)}  "
          f"({datetime.fromtimestamp(cs[0]['t']/1000,timezone.utc):%Y-%m-%d} -> "
          f"{datetime.fromtimestamp(cs[-1]['t']/1000,timezone.utc):%Y-%m-%d})")
    closes = [x["c"] for x in cs]
    highs = [x["h"] for x in cs]
    lows = [x["l"] for x in cs]
    adx_raw = adx([C(x) for x in cs], 14)
    adx_arr = [(v if v == v and v != float("inf") else None) for v in adx_raw]
    lows_i, highs_i = swing_pivots(highs, lows)

    params = [("20/30 (当前生产)", 20, 30),
              ("20/50 (常见主流)", 20, 50),
              ("50/200 (金叉/死叉)", 50, 200)]
    rows = []
    for name, f, s in params:
        reg = classify(closes, f, s, adx_arr)
        rows.append(evaluate(name, reg, closes, lows_i, highs_i))

    print("\n%-22s %6s %6s %8s %9s %10s %9s %9s" % (
        "参数组", "方向翻转", "有效信号", "假信号", "假信号率", "24h方向准确率", "做多滞后h", "做空滞后h"))
    for r in rows:
        print("%-22s %6d %6d %8d %8.1f%% %9.1f%% %9.0f %9.0f" % (
            r["name"], r["flips"], r["calls"], r["whipsaw"],
            r["whipsaw_pct"], r["align_pct"], r["lag_up"], r["lag_dn"]))

    print("\n%-22s %s  %s" % ("参数组", "平均顺势24h收益", "时间占比 up/down/neutral/chop"))
    for r in rows:
        f = r["frac"]
        print("%-22s   %+.3f%%        %.0f/%.0f/%.0f/%.0f" % (
            r["name"], r["mean_fwd"], f["up"], f["down"], f["neutral"], f["chop"]))

    # 额外：不同前向窗口的方向准确率曲线（看短/中期谁更准）
    print("\n不同前向窗口的方向预测准确率 (%) / 平均顺势收益:")
    print("%-22s %s" % ("参数组", "  ".join(f"{h}h" for h in (6, 12, 24, 48, 96))))
    for name, fp, sp in params:
        reg = classify(closes, fp, sp, adx_arr)
        cells = []
        for H in (6, 12, 24, 48, 96):
            vals = []
            for i in range(200, len(closes) - H):
                r = reg[i]
                if r in ("up", "down"):
                    ret = (closes[i + H] - closes[i]) / closes[i]
                    vals.append(ret if r == "up" else -ret)
            if vals:
                acc = 100 * sum(1 for v in vals if v > 0) / len(vals)
                cells.append(f"{acc:.0f}/{statistics.mean(vals)*100:+.2f}")
            else:
                cells.append("-")
        print("%-22s %s" % (name, "  ".join(f"{c:>11}" for c in cells)))
    print("\n(每格 = 准确率%/平均顺势收益%)")


if __name__ == "__main__":
    main()
