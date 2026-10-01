#!/usr/bin/env python3
"""M-7 pre-registered: entry-direction rule significance across timeframes.

For each timeframe (5m/15m/1h), rebuild the same momentum core as M-5/M-6,
then test whether the signal direction has predictive edge on the NEXT bar's
detraded return, using a Politis-Romano stationary bootstrap. Reports gross
bps, net bps (after taker round-trip cost) and a one-sided p-value, split by
long/short. This is a decision gate: it tells whether to move the primary
timeframe up before touching entry/exit construction.

Protocol: docs/research/2026-09/m7_signal_significance_prereg_2026-10-01.md
"""
from __future__ import annotations

import argparse
import glob

import numpy as np
import pandas as pd

TICK = "/mnt/tick/raw/parquet"
K_VOL = 1.5
FEE = 9.0                      # taker round-trip bps
TF_MS = {"5m": 300_000, "15m": 900_000, "1h": 3_600_000}
Q_BLOCK = 0.1                  # stationary bootstrap probability of NEW block
N_BOOT = 2000


def day_files(coin, start, end):
    out = []
    for f in sorted(glob.glob(f"{TICK}/{coin}/{coin}-trades-*.parquet")):
        d = f.split("-trades-")[1][:10]
        if start <= d < end:
            out.append((d, f))
    return out


def norm_ms(t):
    med = t[len(t) // 2]
    if med > 10_000_000_000_000:
        return t // 1000
    return t


def build_bars(coin, start, end, tf_ms):
    """Stream trades -> timeframe bars: (day, open, close, vol, ret)."""
    bars = []
    for day, path in day_files(coin, start, end):
        df = pd.read_parquet(path, columns=["time", "price", "qty"])
        t = norm_ms(df["time"].to_numpy())
        p = df["price"].to_numpy()
        q = df["qty"].to_numpy()
        ub, ist = np.unique(t // tf_ms, return_index=True)
        ien = np.append(ist[1:], len(t))
        for s, e in zip(ist, ien):
            if e > s:
                bars.append((day, p[s], p[e - 1], float(q[s:e].sum())))
    return bars


def signals(coin, start, end, tf_ms, k_ret):
    """Emit (day, side, fwd_ret_bps) on bars meeting the momentum condition.

    fwd_ret_bps is the next bar's return in the signal direction (entry at this
    bar close -> next bar close), BEFORE cost.
    """
    bars = build_bars(coin, start, end, tf_ms)
    vol_hist = []
    out = []
    for i in range(len(bars)):
        day, opn, cls, vol = bars[i]
        vol_hist.append(vol)
        if len(vol_hist) > 20:
            vol_hist.pop(0)
        if i + 1 >= len(bars) or len(vol_hist) < 20:
            continue
        avg = sum(vol_hist[:-1]) / 19.0
        if avg <= 0 or vol < K_VOL * avg:
            continue
        r = cls / opn - 1.0
        side = 1 if r >= k_ret else (-1 if r <= -k_ret else 0)
        if side == 0:
            continue
        nxt = bars[i + 1]
        fwd = (nxt[2] / cls - 1.0) * side * 1e4   # next-bar close-to-close, aligned
        out.append((day, side, fwd))
    return out


def estimate_kret(coin, start, split, tf_ms):
    bars = build_bars(coin, start, split, tf_ms)
    rets = np.array([c / o - 1.0 for _, o, c, _ in bars if o > 0])
    return float(np.quantile(np.abs(rets), 0.90))


def stationary_boot_p(x, seed):
    """One-sided test H0: mean(x-detrended) <= 0 via stationary bootstrap.

    Builds each resample with geometric-length blocks (mean 1/Q_BLOCK), which
    is equivalent to the Politis-Romano stationary bootstrap, but vectorised per
    block instead of per element.
    """
    x = np.asarray(x, dtype=float)
    n = len(x)
    det = x - x.mean()                 # under H0, mean = 0
    obs = x.mean()
    rng = np.random.default_rng(seed)
    mean_block = 1.0 / Q_BLOCK
    cnt = 0
    for _ in range(N_BOOT):
        idx = np.empty(n, dtype=int)
        pos = 0
        while pos < n:
            start = rng.integers(0, n)
            blen = int(rng.geometric(1.0 / mean_block))
            take = min(blen, n - pos)
            seg = (start + np.arange(take)) % n
            idx[pos:pos + take] = seg
            pos += take
        if det[idx].mean() >= obs:
            cnt += 1
    return cnt / N_BOOT


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2026-01-01")
    ap.add_argument("--end", default="2026-09-28")
    ap.add_argument("--split", default="2026-03-31")
    ap.add_argument("--coins", default="BTCUSDT,ETHUSDT")
    ap.add_argument("--seed", type=int, default=20261001)
    args = ap.parse_args()

    print("=" * 86)
    print("M-7 entry-direction significance (next-bar, detrended, stationary bootstrap)")
    print("=" * 86)
    print(f"{'tf':>4} | {'side':>5} | {'n':>5} | {'gross bp':>9} | {'net bp':>8} | {'p(one-sided)':>12}")
    summary = []
    for tf, tf_ms in TF_MS.items():
        recs = []
        for ci, coin in enumerate(args.coins.split(",")):
            k = estimate_kret(coin, args.start, args.split, tf_ms)
            recs.extend(signals(coin, args.start, args.end, tf_ms, k))
        test = [r for r in recs if r[0] >= args.split]
        for side, lab in ((1, "long"), (-1, "short"), (0, "all")):
            sub = [r[2] for r in test if side == 0 or r[1] == side]
            if len(sub) < 30:
                continue
            gross = float(np.mean(sub))
            p = stationary_boot_p(sub, args.seed + len(summary))
            print(f"{tf:>4} | {lab:>5} | {len(sub):>5} | {gross:>+9.2f} | {gross - FEE:>+8.2f} | {p:>12.4f}")
            summary.append((tf, lab, len(sub), gross, gross - FEE, p))
        print("-" * 86)
    print("Read: p<=0.05 significant direction edge. Decision gate for timeframe shift.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
