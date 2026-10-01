#!/usr/bin/env python3
"""M-8 pre-registered: exit-horizon conditional-mean curve for the 1h LONG signal.

Builds the 1h long momentum signal (the only net-positive cell from M-7), then
for every frozen horizon H closes at the H-th future 1h bar close. Reports test
net bps vs H with stationary-bootstrap CI. Long-only, no stop, no in-sample H
selection.

Protocol: docs/research/2026-09/m8_h1_long_exit_curve_prereg_2026-10-01.md
"""
from __future__ import annotations

import argparse

import numpy as np

import scripts.signal_significance as S

TF_MS = S.TF_MS["1h"]
FEE = 9.0
H_LIST_H = [1, 2, 4, 8, 16, 24, 48]
N_BOOT = 2000
Q_BLOCK = 0.1


def long_signals_multi_h(coin, start, end, k_ret):
    """Return rows (day, [fwd_bps per H]) for 1h long signals.

    Uses the same bar construction/signal rule as M-7; forward return to the
    H-th future bar close, aligned long, before cost.
    """
    bars = S.build_bars(coin, start, end, TF_MS)
    vol_hist = []
    out = []
    for i in range(len(bars)):
        day, opn, cls, vol = bars[i]
        vol_hist.append(vol)
        if len(vol_hist) > 20:
            vol_hist.pop(0)
        if len(vol_hist) < 20:
            continue
        avg = sum(vol_hist[:-1]) / 19.0
        if avg <= 0 or vol < S.K_VOL * avg:
            continue
        if cls / opn - 1.0 < k_ret:
            continue
        row = []
        for H in H_LIST_H:
            j = i + H
            row.append((bars[j][2] / cls - 1.0) * 1e4 if j < len(bars) else np.nan)
        out.append((day, row))
    return out


def boot_ci(x, seed):
    n = len(x)
    rng = np.random.default_rng(seed)
    m = x.mean()
    bs = np.empty(N_BOOT)
    for b in range(N_BOOT):
        idx = np.empty(n, dtype=int)
        pos = 0
        while pos < n:
            start = rng.integers(0, n)
            blen = int(rng.geometric(Q_BLOCK))
            take = min(blen, n - pos)
            idx[pos:pos + take] = (start + np.arange(take)) % n
            pos += take
        bs[b] = x[idx].mean()
    return m, np.percentile(bs, 2.5), np.percentile(bs, 97.5)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2026-01-01")
    ap.add_argument("--end", default="2026-09-28")
    ap.add_argument("--split", default="2026-03-31")
    ap.add_argument("--coins", default="BTCUSDT,ETHUSDT")
    ap.add_argument("--seed", type=int, default=20261001)
    args = ap.parse_args()

    recs = []
    for coin in args.coins.split(","):
        k = S.estimate_kret(coin, args.start, args.split, TF_MS)
        print(f"{coin} k_ret={k:.5f}")
        recs.extend(long_signals_multi_h(coin, args.start, args.end, k))

    test = [r for r in recs if r[0] >= args.split]
    print("=" * 80)
    print(f"M-8 1h LONG exit horizon curve — rows={len(test)}")
    print("=" * 80)
    print(f"{'H(h)':>5} | {'net bps':>9} | {'95% CI':>24} | Δ vs prev")
    prev = None
    for k, H in enumerate(H_LIST_H):
        vals = np.array([r[1][k] for r in test])
        vals = vals[~np.isnan(vals)]
        m, lo, hi = boot_ci(vals - FEE, args.seed + k)
        d = "" if prev is None else f"{m - prev:+.2f}"
        print(f"{H:>5} | {m:>+9.2f} | [{lo:>+8.2f},{hi:>+8.2f}] | {d}")
        prev = m
    print("-" * 80)
    print("diagnostic only — do NOT pick best H on this sample (prereg §6)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
