#!/usr/bin/env python3
"""M-6 pre-registered: pure time-based exit horizon decay curve.

Rebuilds the SAME 5m momentum core / split / cost as M-5, enters with taker at
the first trade after the signal, then for every frozen horizon H closes at the
first trade at/after entry+H. Reports test-half net bps vs H with day-block
bootstrap. No stop, no profit target, no in-sample H selection (see prereg).

Protocol: docs/research/2026-09/m6_hold_horizon_decay_prereg_2026-09-30.md
"""
from __future__ import annotations

import argparse
import datetime as dt
import glob

import numpy as np
import pandas as pd

TICK = "/mnt/tick/raw/parquet"
K_VOL = 1.5
FEE = 9.0  # taker round-trip bps, identical for all H
H_LIST_MIN = [5, 15, 30, 60, 120, 240]


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


def estimate_kret(coin, start, split):
    rets = []
    for _, path in day_files(coin, start, split):
        df = pd.read_parquet(path, columns=["time", "price"])
        t = norm_ms(df["time"].to_numpy())
        p = df["price"].to_numpy()
        ub, ist = np.unique(t // 300_000, return_index=True)
        ien = np.append(ist[1:], len(t))
        for s, e in zip(ist, ien):
            if e - s:
                rets.append(abs(p[e - 1] / p[s] - 1.0))
    return float(np.quantile(rets, 0.90))


def simulate(coin, start, end, k_ret):
    """For each signal, taker-enter then resolve all H on the future trade path.

    Pending signals carry entry time/px; on subsequent bars we searchsorted the
    exit for each not-yet-filled horizon. State persists across day files.
    """
    records = []
    pending = []          # open signals awaiting one or more horizon exits
    bars_vol = []
    last_sig_side = 0
    h_ms = [h * 60_000 for h in H_LIST_MIN]

    for day, path in day_files(coin, start, end):
        df = pd.read_parquet(path, columns=["time", "price", "qty"])
        t = norm_ms(df["time"].to_numpy())
        p = df["price"].to_numpy()
        q = df["qty"].to_numpy()
        ubkt, ist = np.unique(t // 300_000, return_index=True)
        ien = np.append(ist[1:], len(t))

        for bi in range(len(ubkt)):
            s, e = ist[bi], ien[bi]
            if e <= s:
                continue
            b1 = (ubkt[bi] + 1) * 300_000  # bar CLOSE boundary
            tt, pp = t[s:e], p[s:e]

            # service pending: taker-enter on first trade after signal bar, then
            # resolve remaining horizon exits on this slice
            still = []
            for o in pending:
                if o["entry"] is None:
                    o["entry_t"] = tt[0]
                    o["entry"] = pp[0]
                exits = o["exits"]
                for k in range(len(h_ms)):
                    if exits[k] is None:
                        j = np.searchsorted(tt, o["entry_t"] + h_ms[k])
                        if j < len(pp):
                            exits[k] = pp[j]
                if any(x is None for x in exits):
                    still.append(o)
                else:
                    records.append(_emit(o))
            pending = still

            # A signal that fired this bar cannot enter this bar (all trades are
            # <= b1); it enters at the first trade of the NEXT bar below.
            # ---- finalize bar / signal ----
            opn, cls = pp[0], pp[-1]
            vol = float(q[s:e].sum())
            bars_vol.append(vol)
            if len(bars_vol) > 20:
                bars_vol.pop(0)
            sig = 0
            if len(bars_vol) >= 20:
                avg = sum(bars_vol[:-1]) / 19.0
                if avg > 0 and vol >= K_VOL * avg:
                    r = cls / opn - 1.0
                    if r >= k_ret:
                        sig = 1
                    elif r <= -k_ret:
                        sig = -1
            if sig != 0 and sig != last_sig_side and not pending:
                pending.append({
                    "side": sig, "t0": b1,
                    "entry_t": None, "entry": None,
                    "exits": [None] * len(h_ms),
                    "day": dt.datetime.utcfromtimestamp(b1 / 1000).date().isoformat(),
                })
                last_sig_side = sig
            elif sig == 0:
                last_sig_side = 0

        # carry unresolved across the day boundary (their exits land on later days)
    return records


def _emit(o):
    side = o["side"]
    rets = [(float(x) / o["entry"] - 1.0) * side * 1e4 - FEE for x in o["exits"]]
    return {"day": o["day"], "side": side, "ret": rets}


def bootstrap(recs, k, seed, B=2000):
    by_day = {}
    for r in recs:
        by_day.setdefault(r["day"], []).append(r["ret"][k])
    days = sorted(by_day)
    M = np.array([np.mean(by_day[d]) for d in days])
    n = len(M)
    rng = np.random.default_rng(seed)
    bs = [M[rng.integers(0, n, n)].mean() for _ in range(B)]
    return M.mean(), np.percentile(bs, 2.5), np.percentile(bs, 97.5)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2026-01-01")
    ap.add_argument("--end", default="2026-09-28")
    ap.add_argument("--split", default="2026-03-31")
    ap.add_argument("--coins", default="BTCUSDT,ETHUSDT")
    ap.add_argument("--seed", type=int, default=20260930)
    args = ap.parse_args()

    recs = []
    for coin in args.coins.split(","):
        k_ret = estimate_kret(coin, args.start, args.split)
        print(f"{coin} k_ret={k_ret:.5f}; simulating ...")
        recs.extend(simulate(coin, args.start, args.end, k_ret))

    test = [r for r in recs if r["day"] >= args.split]
    print("=" * 78)
    print(f"M-6 time-based exit horizon decay — TEST n={len(test)} signals")
    print("=" * 78)
    print(f"{'H(min)':>7} | {'net bps':>9} | {'95% CI':>22}")
    prev = None
    for k, h in enumerate(H_LIST_MIN):
        m, lo, hi = bootstrap(test, k, args.seed + k)
        diff = "" if prev is None else f"   Δ vs prev {m - prev:+.2f}"
        print(f"{h:>7} | {m:>+9.2f} | [{lo:>+8.2f},{hi:>+8.2f}]{diff}")
        prev = m
    print("-" * 78)
    for s, lab in ((1, "long"), (-1, "short")):
        sub = [r for r in test if r["side"] == s]
        if not sub:
            continue
        row = [bootstrap(sub, k, args.seed + k)[0] for k in range(len(H_LIST_MIN))]
        print(f"{lab:>5} n={len(sub):<5}: " + "  ".join(f"H{H_LIST_MIN[i]}={row[i]:+.2f}" for i in range(len(H_LIST_MIN))))
    print("-" * 78)
    print("diagnostic only — do NOT pick the best H on this same sample (prereg §5.2)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
