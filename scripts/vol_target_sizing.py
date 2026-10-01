#!/usr/bin/env python3
"""M-9 pre-registered: volatility-target sizing on the 1h LONG signal.

Builds the 1h long momentum signal, holds a fixed H=8h for every arm (to
isolate sizing as the only variable), and compares unscaled vs vol-target
weights on one equity curve. Reports cumulative return, vol, Sharpe, max
drawdown, ES. sigma uses the trailing 30d of 1h returns only (no look-ahead).

Protocol: docs/research/2026-09/m9_vol_target_sizing_prereg_2026-10-01.md
"""
from __future__ import annotations

import argparse

import numpy as np

import scripts.signal_significance as S

TF_MS = S.TF_MS["1h"]
FEE = 9.0 / 1e4
H = 8
SIGMA_LOOKBACK = 720          # ~30d of 1h bars
CAP = 2.0
ARMS = {"U": None, "V20": 0.20, "V40": 0.40}


def build_trades(coin, start, end, k_ret):
    """Rows: (entry_index, day, period_return_fraction) for non-overlap long signals."""
    bars = S.build_bars(coin, start, end, TF_MS)
    closes = np.array([b[2] for b in bars])
    rets = np.diff(closes) / closes[:-1]
    vol_hist = []
    out = []
    last_entry = -10**9
    for i in range(len(bars)):
        day, opn, cls, vol = bars[i]
        vol_hist.append(vol)
        if len(vol_hist) > 20:
            vol_hist.pop(0)
        if len(vol_hist) < 20 or i + H >= len(bars):
            continue
        avg = sum(vol_hist[:-1]) / 19.0
        if avg <= 0 or vol < S.K_VOL * avg or cls / opn - 1.0 < k_ret:
            continue
        if i - last_entry < H:      # non-overlap within coin
            continue
        last_entry = i
        # trailing realised vol: prior SIGMA_LOOKBACK 1h returns (annualised, 8760 h/yr)
        lo = max(0, i - SIGMA_LOOKBACK)
        sigma = rets[lo:i].std() * np.sqrt(8760) if i - lo > 20 else np.nan
        period_ret = (bars[i + H][2] / cls - 1.0) - 2 * FEE
        out.append((i, day, sigma, period_ret))
    return out


def equity_curve(trades, target):
    """Trades already sorted by entry index. w=1 if target None else clip(target/sig)."""
    eq = 1.0
    curve = [1.0]
    days = []
    for (_, day, sigma, ret) in trades:
        if target is None:
            w = 1.0
        else:
            w = CAP if np.isnan(sigma) or sigma <= 0 else min(CAP, target / sigma)
        eq += w * ret
        curve.append(eq)
        days.append(day)
    return np.array(curve), days


def metrics(curve):
    rets = np.diff(curve) / curve[:-1]
    cum = curve[-1] - 1.0
    dd = (curve / np.maximum.accumulate(curve) - 1.0).min()
    es = np.percentile(rets, 5)
    sharpe = rets.mean() / rets.std() * np.sqrt(len(rets)) if rets.std() > 0 else np.nan
    return cum, sharpe, dd, es


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2026-01-01")
    ap.add_argument("--end", default="2026-09-28")
    ap.add_argument("--split", default="2026-03-31")
    ap.add_argument("--coins", default="BTCUSDT,ETHUSDT")
    args = ap.parse_args()

    trades = []
    for coin in args.coins.split(","):
        k = S.estimate_kret(coin, args.start, args.split, TF_MS)
        rows = build_trades(coin, args.start, args.end, k)
        print(f"{coin} k={k:.5f} trades={len(rows)}")
        trades.extend(rows)
    trades.sort(key=lambda r: r[0])
    test_trades = [t for t in trades if t[1] >= args.split]

    print("=" * 78)
    print(f"M-9 vol-target sizing (1h long, H={H}h) — test trades={len(test_trades)}")
    print("=" * 78)
    print(f"{'arm':>4} | {'cum net':>9} | {'Sharpe':>7} | {'maxDD':>8} | {'5% ES':>8}")
    for name, target in ARMS.items():
        curve, _ = equity_curve(test_trades, target)
        cum, sharpe, dd, es = metrics(curve)
        print(f"{name:>4} | {cum*100:>+8.1f}% | {sharpe:>7.2f} | {dd*100:>+7.1f}% | {es*100:>+7.2f}%")
    print("-" * 78)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
