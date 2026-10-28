#!/usr/bin/env python3
"""maker v2 historical parameter grid (read-only, no funds).

Sweeps passive-maker parameters over REAL historical candles and tests whether
ANY cell clears a positive, statistically-significant net maker advantage after
adverse selection, using DAY-BLOCK (not per-fill) bootstrap so clustered fills
on the same tape day do not inflate significance.

Net per filled order (bp):
    net = maker_edge_bps - post_fill_mid_drift_bps
(price improvement vs post-mid, minus the next-bar adverse mid drift; fills with
no forward bar are excluded, matching the simulator's no-fabrication rule).

Dimensions:
    offset_bps  : passive limit offset behind mid (buy = mid*(1-offset))
    kappa       : fraction of adverse drift charged into the fill price
    base/decay  : queue-penetration fill probability (calibrated defaults)
    vol gate    : none / high-realized-vol-only (PIT within the signal window)

Candles: Binance spot cache (BTC 1m; BTC/ETH/SOL/BNB 5m). Same cost convention
as the rest of the research (this grid isolates the MAKER leg; the 5bp offset is
already the maker rebate proxy inside maker_edge, so no extra fee is layered).

Usage:
  PYTHONPATH=. python3 scripts/maker_v2_param_grid.py --mc 40 --json
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import random
import statistics
from typing import Optional

from hermes_trader.execution.maker_shadow import (
    ShadowMakerOrder,
    simulate_shadow_order,
    simulate_shadow_order_v2,
)
from hermes_trader.models.types import Candle

REST_MINUTES = 30
OFFSETS = (3.0, 5.0, 8.0, 12.0)
KAPPAS = (0.0, 0.5, 1.0)
BASE_DEFAULT, DECAY_DEFAULT = 0.30, 0.5
VOL_GATES = ("none", "high")
VOL_HIGH_Q = 0.60          # within-window realized-vol percentile (PIT)
BOOT = 5000
SEED = 20261028

DEFAULT_FILES = (
    ("BTC", "logs/binance_klines_cache/BTCUSDT_1m.json"),
    ("BTC", "logs/binance_klines_cache/BTCUSDT_5m.json"),
    ("ETH", "logs/binance_klines_cache/ETHUSDT_5m.json"),
    ("SOL", "logs/binance_klines_cache/SOLUSDT_5m.json"),
    ("BNB", "logs/binance_klines_cache/BNBUSDT_5m.json"),
)


def _candle(b):
    if isinstance(b, dict):
        return Candle(t=float(b["t"]), o=float(b["o"]), h=float(b["h"]),
                      l=float(b["l"]), c=float(b["c"]), v=float(b.get("v", 1.0)))
    return Candle(t=float(b[0]), o=float(b[1]), h=float(b[2]),
                  l=float(b[3]), c=float(b[4]),
                  v=float(b[5]) if len(b) > 5 else 1.0)


def load_bars(path):
    with open(path) as fh:
        raw = json.load(fh)
    interval_min = None
    rows = raw
    if isinstance(raw, dict):
        interval_min = raw.get("interval_minutes") or raw.get("interval_min")
        rows = raw.get("bars") or raw.get("candles")
    bars = sorted((_candle(b) for b in rows), key=lambda x: x.t)
    if interval_min is None and len(bars) > 1:
        diffs = sorted(bars[i + 1].t - bars[i].t
                       for i in range(len(bars) - 1))
        interval_min = diffs[len(diffs) // 2] / 60_000.0
    return float(interval_min or 1.0), bars


def build_signals(bars, interval_min, offset_bps):
    """One non-overlapped passive BUY every REST window; PIT vol tag."""
    ttl = max(2, round(REST_MINUTES / interval_min))
    win = ttl + 2
    step = ttl + 1
    sig = []
    for start in range(0, len(bars) - win, step):
        w = bars[start:start + win]
        base = w[0].c
        mid = 100.0
        reb = [Candle(t=float(i), o=x.o / base * 100, h=x.h / base * 100,
                      l=x.l / base * 100, c=x.c / base * 100, v=x.v)
               for i, x in enumerate(w)]
        limit = mid * (1.0 - offset_bps / 1e4)
        order = ShadowMakerOrder(
            coin="X", is_buy=True, size=1.0, posted_bar_idx=0,
            limit_px=limit, post_mid_px=mid, ttl_bars=ttl)
        # realized vol (|close-to-close| pct) over the pre-signal window; use
        # the posted bar's own range vs the window as a simple PIT activity tag.
        rng_pct = (w[0].h - w[0].l) / base * 100.0
        day = dt.datetime.utcfromtimestamp(w[0].t / 1000.0).strftime("%Y-%m-%d")
        sig.append({"order": order, "bars": reb, "day": day,
                    "rng_pct": rng_pct, "ts": w[0].t})
    return sig


def day_block_stats(by_day, boot=BOOT, seed=SEED):
    """by_day: day -> list[net bp]. Returns mean/t/CI over equal-weight days."""
    days = [statistics.fmean(v) for v in by_day.values()]
    if len(days) < 2:
        return {"days": len(days), "mean_bp": None, "t": None, "ci": None}
    m = statistics.fmean(days)
    sd = statistics.pstdev(days)
    t = m / (sd / len(days) ** 0.5) if sd > 0 else None
    rng = random.Random(seed)
    nd = len(days)
    sims = sorted(statistics.fmean(days[rng.randrange(nd)] for _ in range(nd))
                  for _ in range(boot))
    ci = [round(sims[int(boot * 0.025)], 3),
          round(sims[int(boot * 0.975)], 3)]
    return {"days": nd, "mean_bp": round(m, 3), "t": round(t, 2) if t else None,
            "ci": ci}


def run_cell(signals, *, mc, base, decay, kappa, gate):
    # vol-gate threshold is the within-file PIT percentile of signal ranges
    if gate == "high":
        rs = sorted(s["rng_pct"] for s in signals)
        q = rs[int(len(rs) * VOL_HIGH_Q)]
        pool = [s for s in signals if s["rng_pct"] >= q]
    else:
        pool = signals
    fill_n = 0
    touch_n = 0
    by_day: dict = {}
    for r in range(mc):
        rng = random.Random(SEED + r)
        for s in pool:
            f2 = simulate_shadow_order_v2(
                s["order"], s["bars"], base_fill_prob=base,
                fill_prob_decay=decay, adv_drift_kappa=kappa,
                deterministic=False, rng=rng)
            if f2.touch_bar_idx is not None:
                touch_n += 1
            if not f2.filled:
                continue
            if (f2.maker_edge_bps is None
                    or f2.post_fill_mid_drift_bps is None):
                continue
            net = f2.maker_edge_bps - f2.post_fill_mid_drift_bps
            by_day.setdefault(s["day"], []).append(net)
            fill_n += 1
    sig_n = len(pool) * mc
    st = day_block_stats(by_day)
    st.update({
        "fill_rate_pct": round(100.0 * fill_n / sig_n, 2) if sig_n else 0.0,
        "n_fills": fill_n,
    })
    return st


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mc", type=int, default=40)
    ap.add_argument("--base", type=float, default=BASE_DEFAULT)
    ap.add_argument("--decay", type=float, default=DECAY_DEFAULT)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--save", default=None)
    ap.add_argument("--coins", nargs="*", default=None,
                    help="subset of BTC/ETH/SOL/BNB (default all available)")
    ap.add_argument("--max-signals", type=int, default=2000,
                    help="even-stride cap of signals per file (keeps full span)")
    args = ap.parse_args(argv)

    files = [(c, p) for c, p in DEFAULT_FILES if __import__("os").path.exists(p)]
    if args.coins:
        sel = set(args.coins)
        # keep one interval per coin preferring 1m then 5m; simpler: filter
        files = [(c, p) for c, p in files if c in sel]

    # cache signals per (coin,path,offset)
    loaded = {}
    for coin, path in files:
        interval, bars = load_bars(path)
        loaded[(coin, path)] = (interval, bars)

    results = []
    for (coin, path), (interval, bars) in loaded.items():
        for off in OFFSETS:
            sig = build_signals(bars, interval, off)
            cap = args.max_signals
            if cap and len(sig) > cap:
                k = len(sig) / cap
                sig = [sig[int(i * k)] for i in range(cap)]
            for gate in VOL_GATES:
                for kappa in KAPPAS:
                    st = run_cell(sig, mc=args.mc, base=args.base,
                                  decay=args.decay, kappa=kappa, gate=gate)
                    results.append({
                        "coin": coin,
                        "interval_min": interval,
                        "offset_bps": off, "kappa": kappa, "gate": gate,
                        "base": args.base, "decay": args.decay, **st})

    out = {"grid": {"offsets": OFFSETS, "kappas": KAPPAS, "gates": VOL_GATES,
                    "mc": args.mc, "rest_minutes": REST_MINUTES},
           "results": results}
    if args.save:
        with open(args.save, "w") as fh:
            json.dump(out, fh, ensure_ascii=False, indent=2)

    # human table: only show economically non-empty cells (>=0.5% fill rate)
    if not args.json:
        print("coin int off kappa gate | fill%  nfill days  mean_bp   t    CI95")
        for r in sorted(results, key=lambda x: (-(x["mean_bp"] or -999))):
            if r["mean_bp"] is None:
                continue
            mark = "  <== CI>0" if r["ci"] and r["ci"][0] > 0 else ""
            print(f"{r['coin']:3s} {r['interval_min']:g}m {r['offset_bps']:4.0f} "
                  f"{r['kappa']:.1f} {r['gate']:4s} | {r['fill_rate_pct']:5.1f} "
                  f"{r['n_fills']:6d} {r['days']:4d} {r['mean_bp']:7.2f} "
                  f"{str(r['t']):>5} {r['ci']}{mark}")
        pos = [r for r in results if r["ci"] and r["ci"][0] > 0
               and r["fill_rate_pct"] >= 0.5]
        print(f"\ncells with day-block CI lower>0 and fill>=0.5%: {len(pos)}")
    else:
        print(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
