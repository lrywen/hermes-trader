#!/usr/bin/env python3
"""M-10 pre-registered: integrated candidate vs baseline OOS comparison.

Signal: 1h long momentum (M-7), long-only, non-overlap per coin.
Arms:
  BASELINE - fixed H=8h exit, w=1
  TRAIL    - Chandelier trailing exit (fixed params), w=1
  FULL     - same trailing exit + V20 vol-target sizing

Path simulation uses 1h bar high/low (adverse-first = conservative). Reports
cum net, Sharpe, max drawdown, ES, win rate, avg hold.

Protocol: docs/research/2026-09/m10_integrated_candidate_prereg_2026-10-01.md
"""
from __future__ import annotations
import argparse
import glob

import numpy as np
import pandas as pd

import scripts.signal_significance as S

TF_MS = S.TF_MS["1h"]
FEE = 9.0 / 1e4
H_FIX = 8
HARD_STOP = -0.10
MIN_PROFIT = 0.02
TRAIL_GIVEBACK = 0.05
MAX_HOLD = 120
TARGET_VOL, CAP = 0.20, 2.0
SIGMA_LOOKBACK = 720
ANNUAL = 8760


def build_bars_hl(coin, start, end):
    rows = []
    for day, path in S.day_files(coin, start, end):
        df = pd.read_parquet(path, columns=["time", "price", "qty"])
        t = S.norm_ms(df["time"].to_numpy())
        p = df["price"].to_numpy()
        q = df["qty"].to_numpy()
        ub, ist = np.unique(t // TF_MS, return_index=True)
        ien = np.append(ist[1:], len(t))
        for a, b in zip(ist, ien):
            if b > a:
                seg = p[a:b]
                rows.append((day, seg[0], seg.max(), seg.min(), seg[-1], float(q[a:b].sum())))
    return rows


def simulate(bars, mode, k):
    """Forward single-position state machine (strictly non-overlapping).

    While a position is open, advance its exit on each bar; only when flat
    evaluate a new signal. BASELINE is a timed position (exit at entry+H_FIX);
    TRAIL/FULL use hard-stop + Chandelier trailing. Returns per-trade increments.
    """
    closes = np.array([b[4] for b in bars])
    rets = np.diff(closes) / closes[:-1]
    vol_hist = []
    out = []
    pos = None
    n = len(bars)
    for i in range(n):
        day, opn, hi, lo, cls, vol = bars[i]
        vol_hist.append(vol)
        if len(vol_hist) > 20:
            vol_hist.pop(0)

        if pos is not None:
            exit_px = None
            if pos.get("timed"):
                if i >= pos["exit_i"]:
                    exit_px = cls
            else:
                # adverse-first hard stop before trailing is armed
                if not pos["armed"] and (lo / pos["entry"] - 1.0) <= HARD_STOP:
                    exit_px = pos["entry"] * (1 + HARD_STOP)
                pos["peak"] = max(pos["peak"], hi)
                if not pos["armed"] and (pos["peak"] / pos["entry"] - 1.0) >= MIN_PROFIT:
                    pos["armed"] = True
                if exit_px is None and pos["armed"]:
                    floor = pos["peak"] * (1 - TRAIL_GIVEBACK)
                    if lo <= floor:
                        exit_px = floor
                if exit_px is None and i - pos["entry_i"] >= MAX_HOLD:
                    exit_px = cls
            if exit_px is not None:
                r = (exit_px / pos["entry"] - 1.0) - 2 * FEE
                out.append((pos["day"], pos["w"] * r))
                pos = None
            continue

        # flat: evaluate a new signal at this bar's close
        if len(vol_hist) < 20:
            continue
        avg = sum(vol_hist[:-1]) / 19.0
        if not (avg > 0 and vol >= S.K_VOL * avg and cls / opn - 1.0 >= k):
            continue

        if mode == "BASELINE":
            exit_i = i + H_FIX
            if exit_i >= n:
                continue
            pos = {"entry": cls, "w": 1.0, "exit_i": exit_i, "timed": True, "day": day}
        else:
            if mode == "FULL":
                lox = max(0, i - SIGMA_LOOKBACK)
                sig = rets[lox:i].std() * np.sqrt(ANNUAL) if i - lox > 20 else np.nan
                w = CAP if np.isnan(sig) or sig <= 0 else min(CAP, TARGET_VOL / sig)
            else:
                w = 1.0
            pos = {"entry": cls, "w": w, "peak": cls, "armed": False,
                   "entry_i": i, "day": day}
    return out


def curve(trades):
    eq = 1.0; c = [1.0]
    for _, r in trades:
        eq += r; c.append(eq)
    return np.array(c)


def stats(c, trades):
    rets = np.diff(c) / c[:-1]
    dd = (c / np.maximum.accumulate(c) - 1).min()
    sh = rets.mean() / rets.std() * np.sqrt(len(rets)) if rets.std() > 0 else np.nan
    wins = np.mean([r > 0 for _, r in trades])
    return c[-1] - 1, sh, dd, np.percentile(rets, 5), wins


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2026-01-01")
    ap.add_argument("--end", default="2026-09-28")
    ap.add_argument("--split", default="2026-03-31")
    ap.add_argument("--coins", default="BTCUSDT,ETHUSDT")
    args = ap.parse_args()

    results = {m: [] for m in ("BASELINE", "TRAIL", "FULL")}
    for coin in args.coins.split(","):
        k = S.estimate_kret(coin, args.start, args.split, TF_MS)
        bars = build_bars_hl(coin, args.start, args.end)
        print(f"{coin} k={k:.5f} bars={len(bars)}")
        for m in results:
            results[m].extend(simulate(bars, m, k))
    for m in results:
        results[m].sort(key=lambda x: x[0])
        results[m] = [t for t in results[m] if t[0] >= args.split]

    print("=" * 80)
    print("M-10 integrated candidate — TEST")
    print("=" * 80)
    print(f"{'arm':>9} | {'cum net':>8} | {'Sharpe':>6} | {'maxDD':>7} | {'5%ES':>6} | win%")
    for m in ("BASELINE", "TRAIL", "FULL"):
        c = curve(results[m]); v = stats(c, results[m])
        print(f"{m:>9} | {v[0]*100:>+7.1f}% | {v[1]:>6.2f} | {v[2]*100:>+6.1f}% | {v[3]*100:>+5.1f}% | {v[4]*100:>4.0f}")
    print("-" * 80)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
