#!/usr/bin/env python3
"""M-5 pre-registered execution A/B: immediate taker vs pullback limit entry.

Rebuilds a 5m momentum core on Binance BTC/ETH tick data (/mnt/tick parquet),
then for each signal simulates two fills using the exact trade path:
  A  taker at next trade, pay 9 bps round-trip
  B  post-only limit pulled back r=10bps; fill if price trades through it within
     W=15m else cancel (no chase); pay 6.5 bps round-trip when filled
Both hold H=15m and exit at the next trade after H.

Protocol: docs/research/2026-09/m5_limit_vs_taker_ab_prereg_2026-09-30.md
TEST half (>= split) reported once; k_ret from TRAIN only.
"""
from __future__ import annotations

import argparse
import datetime as dt
import glob
import os

import numpy as np
import pandas as pd

TICK = "/mnt/tick/raw/parquet"
R = 0.0010          # pullback 10bps
W_MS = 15 * 60_000  # limit validity
H_MS = 15 * 60_000  # holding period
K_VOL = 1.5
FEE_A = 9.0
FEE_B = 6.5


def day_files(coin, start, end):
    fs = sorted(glob.glob(f"{TICK}/{coin}/{coin}-trades-*.parquet"))
    out = []
    for f in fs:
        d = f.split("-trades-")[1][:10]
        if start <= d < end:
            out.append((d, f))
    return out


def norm_ms(t):
    """Some days store microseconds; normalise the whole day's array to ms."""
    med = t[len(t) // 2]
    if med > 10_000_000_000_000:  # > year 2286 in ms -> microseconds
        return t // 1000
    return t


def simulate(coin, start, end, split, k_ret):
    """Two-pass streaming over daily parquet.

    State machine across bars:
      - build 5m bars from trades; on each closed bar evaluate signal (flat only).
      - when a signal fires at bar close t0, schedule two exits using future trades:
          A: entry = first trade after t0 ; exit = first trade with time >= entry_t+H
          B: limit fill checked on trades in (t0, t0+W]; if filled, exit at first
             trade >= fill_t+H ; else missed.
    Because signals and fills both depend on the trade stream, we process each day
    and carry pending orders across day boundaries via a continuous frame list.
    """
    records = []  # dicts: day, side, a_net, b_net(or None), missed, a_px,b_px...

    # pending: list of dicts with side, t0, p0, and stage flags
    pending = []
    bars_vol = []  # rolling volumes of last 20 closed bars
    last_sig_side = 0

    files = day_files(coin, start, end)
    for day, path in files:
        df = pd.read_parquet(path, columns=["time", "price", "qty", "is_buyer_maker"])
        t = norm_ms(df["time"].to_numpy())
        p = df["price"].to_numpy()
        q = df["qty"].to_numpy()
        # bucket index (5m) within global ms
        bkt = t // 300_000
        # build bars by scanning bucket boundaries
        ubkt, idx_start = np.unique(bkt, return_index=True)
        idx_end = np.append(idx_start[1:], len(t))
        # iterate bars; need trade slices to also service pending orders.
        # We'll walk trade pointer once and, within each bucket, first service
        # pending orders then finalize the bar at bucket close.
        ptr = 0
        for bi in range(len(ubkt)):
            s, e = idx_start[bi], idx_end[bi]
            b0, b1 = ubkt[bi] * 300_000, (ubkt[bi] + 1) * 300_000
            if e <= s:
                continue  # empty bucket (e.g. gap across day boundary)
            # trades in this bucket
            tt, pp = t[s:e], p[s:e]
            # ---- service pending orders against these trades ----
            still = []
            for o in pending:
                keep = _service(o, tt, pp, b1, records)
                if keep:
                    still.append(o)
            pending = still
            # ---- finalize this 5m bar ----
            opn, cls = pp[0], pp[-1]
            vol = float(q[s:e].sum())
            bars_vol.append(vol)
            if len(bars_vol) > 20:
                bars_vol.pop(0)
            ret = cls / opn - 1.0
            sig = 0
            if len(bars_vol) >= 20:
                avg = sum(bars_vol[:-1]) / 19.0
                if avg > 0 and vol >= K_VOL * avg:
                    if ret >= k_ret:
                        sig = 1
                    elif ret <= -k_ret:
                        sig = -1
            # only fire when flat (no pending) and not same-side repeat bar
            if sig != 0 and sig != last_sig_side and not pending:
                # signal decided at close; schedule using trades strictly after b1
                pending.append({
                    "side": sig, "t0": b1, "p0": cls,
                    "a_entry": None, "b_limit": cls * (1 - R * sig),
                    "b_fill_t": None, "b_fill_px": None, "b_done": False,
                })
                last_sig_side = sig
            elif sig == 0:
                last_sig_side = 0
    return records


def _service(o, tt, pp, bar_end, records):
    """Advance one pending order on a trade slice. Return keep-alive flag.

    Trades here are those within the bar bucket; the order acts only on trades
    with time > t0. We use all given trades conservatively.
    """
    side = o["side"]
    # ---- A leg: enter at first available trade, then exit after H ----
    if o["a_entry"] is None:
        if len(pp):
            o["a_entry"] = pp[0]
            o["a_entry_t"] = tt[0]
    if o["a_entry"] is not None and "a_exit" not in o:
        target = o["a_entry_t"] + H_MS
        j = np.searchsorted(tt, target)
        if j < len(pp):
            o["a_exit"] = pp[j]
            o["a_exit_t"] = tt[j]
    # ---- B leg: limit fill within W, then exit after H ----
    if o["b_fill_t"] is None:
        lim = o["b_limit"]
        if side == 1:
            hit = np.where(pp <= lim)[0]
        else:
            hit = np.where(pp >= lim)[0]
        if len(hit):
            j = hit[0]
            o["b_fill_t"] = tt[j]
            o["b_fill_px"] = pp[j]
    if o["b_fill_t"] is not None and not o["b_done"]:
        target = o["b_fill_t"] + H_MS
        j = np.searchsorted(tt, target)
        if j < len(pp):
            o["b_exit"] = pp[j]
            o["b_done"] = True
    # ---- finalize / expiry ----
    a_done = "a_exit" in o
    b_done = o["b_done"]
    expired = bar_end > o["t0"] + W_MS
    if a_done and (b_done or expired):
        rec = _emit(o, expired)
        if rec:
            records.append(rec)
        return False
    return True


def _emit(o, expired):
    day = dt.datetime.utcfromtimestamp(o["t0"] / 1000).date().isoformat()
    # A net bps
    a_ret = (o["a_exit"] / o["a_entry"] - 1.0) * o["side"] * 1e4 - FEE_A
    if o["b_done"]:
        b_ret = (o["b_exit"] / o["b_fill_px"] - 1.0) * o["side"] * 1e4 - FEE_B
        missed = False
    else:
        b_ret = None
        missed = True
    return {"day": day, "side": o["side"], "a": a_ret,
            "b": b_ret, "missed": missed,
            "a_entry": o["a_entry"], "b_fill": o["b_fill_px"]}


def bootstrap_pairs(recs, seed, B=2000):
    by_day = {}
    for r in recs:
        by_day.setdefault(r["day"], []).append(r)
    days = np.array(sorted(by_day))
    rng = np.random.default_rng(seed)

    def day_means(d):
        rs = by_day[d]
        a = np.mean([r["a"] for r in rs])
        # B' treats missed as 0
        bp = np.mean([r["b"] if r["b"] is not None else 0.0 for r in rs])
        return a, bp

    M = np.array([day_means(d) for d in days])
    n = len(days)
    A, Bp = M[:, 0], M[:, 1]
    delta = Bp - A
    bootsA, bootsB, bootsD = [], [], []
    for _ in range(B):
        idx = rng.integers(0, n, n)
        bootsA.append(A[idx].mean())
        bootsB.append(Bp[idx].mean())
        bootsD.append(delta[idx].mean())
    return {
        "A": (A.mean(), (np.percentile(bootsA, 2.5), np.percentile(bootsA, 97.5))),
        "Bprime": (Bp.mean(), (np.percentile(bootsB, 2.5), np.percentile(bootsB, 97.5))),
        "delta": (delta.mean(), (np.percentile(bootsD, 2.5), np.percentile(bootsD, 97.5))),
        "n_days": n,
    }


def estimate_kret(coin, start, split):
    rets = []
    for day, path in day_files(coin, start, split):
        df = pd.read_parquet(path, columns=["time", "price"])
        t = norm_ms(df["time"].to_numpy()); p = df["price"].to_numpy()
        bkt = t // 300_000
        ub, ist = np.unique(bkt, return_index=True)
        ien = np.append(ist[1:], len(t))
        for s, e in zip(ist, ien):
            if e - s:
                rets.append(abs(p[e - 1] / p[s] - 1.0))
    return float(np.quantile(rets, 0.90))


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
        print(f"estimating k_ret on TRAIN {coin} ...")
        k_ret = estimate_kret(coin, args.start, args.split)
        print(f"  k_ret={k_ret:.5f}")
        print(f"simulating {coin} ...")
        recs.extend(simulate(coin, args.start, args.end, args.split, k_ret))

    split_day = args.split
    test = [r for r in recs if r["day"] >= split_day]
    filled = [r for r in test if not r["missed"]]
    miss = [r for r in test if r["missed"]]
    boot = bootstrap_pairs(test, args.seed)

    # opportunity cost of missed trades = their A result (would taker have made $)
    miss_a = np.mean([r["a"] for r in miss]) if miss else float("nan")
    fill_b = np.mean([r["b"] for r in filled]) if filled else float("nan")
    fill_rate = len(filled) / max(len(test), 1)

    rules = {
        "R1 B'>=A and delta CI lo>=0":
            boot["delta"][0] >= 0 and boot["delta"][1][0] >= 0,
        "R2 fill rate >= 50%": fill_rate >= 0.50,
        "R3 missed-trade opp cost not strongly positive":
            (np.isnan(miss_a) or miss_a <= 1.0),
        "R4 A trades >= 300": len(test) >= 300,
    }
    decision = "SWITCH TO LIMIT (B)" if all(rules.values()) else "KEEP TAKER (A)"

    print("=" * 72)
    print("M-5 execution A/B — TEST single report")
    print("=" * 72)
    print(f"signals(A)         : {len(test)}  days {boot['n_days']}")
    print(f"B filled / missed  : {len(filled)} / {len(miss)}  fill rate {fill_rate:.1%}")
    print(f"A net              : {boot['A'][0]:+.2f} bps CI[{boot['A'][1][0]:+.2f},{boot['A'][1][1]:+.2f}]")
    print(f"B filled-only net  : {fill_b:+.2f} bps")
    print(f"B' (missed=0) net  : {boot['Bprime'][0]:+.2f} bps CI[{boot['Bprime'][1][0]:+.2f},{boot['Bprime'][1][1]:+.2f}]")
    print(f"delta(B'-A)        : {boot['delta'][0]:+.2f} bps CI[{boot['delta'][1][0]:+.2f},{boot['delta'][1][1]:+.2f}]")
    print(f"missed opp cost(A) : {miss_a:+.2f} bps")
    print("-" * 72)
    for k, v in rules.items():
        print(f"[{'PASS' if v else 'FAIL'}] {k}")
    print("-" * 72)
    print(f"DECISION: {decision}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
