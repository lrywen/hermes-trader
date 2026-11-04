#!/usr/bin/env python3
"""A4 (2026-11-04): repair the missing equal-weight market BENCHMARK so excess
returns populate on the historical decision-shadow outcomes.

Root cause (proven this session): scripts/reconcile_decision_shadow.py derives
its benchmark basket as

    bench_coins = {k[:-3] for k in candle_cache if k.endswith("|5m")}

and the daily cron's candle cache held almost no 5m series, so bench_series was
empty on virtually every row: 1,309 rows carry gross_fwd/net_* but only 6 rows
carry excess_bp*. This is a benchmark-coverage bug, not a missing-leg problem.

This repair does NOT recompute the leg (which production booked from real
Hyperliquid mid/fwd prices — re-pricing the leg from Binance would inject a
basis). It keeps every existing column and only fills the missing piece:

  * builds an equal-weight market benchmark from a fixed liquid basket whose
    1:1 Binance USDT spot klines are fetched ONCE as already-closed history
    (offline source, never used by live trading);
  * for every covered row with an existing gross_fwd[hz], computes the signed
    benchmark forward over the SAME closed 5m bar the leg was booked on
    (entry_ts) and the SAME horizon;
  * excess_bp{9.26,19}[hz] = existing gross_fwd[hz] - bench[hz] - cost,
    byte-aligned with reconcile_decision_shadow.enrich (short flips both leg
    and bench; cost paid once, outside the sign flip);
  * horizons whose forward window has not yet CLOSED as of run time (the
    last 1-3 days for 24h/72h) are left None honestly — no future is
    interpolated.

Writes a NEW file; never mutates decision_shadow_outcomes.jsonl. Read-only
against the exchange and the existing outcomes file.
"""
from __future__ import annotations

import argparse
import bisect
import json
import os
import time
from typing import Optional

BAR_MS = 5 * 60_000
HORIZONS = {"1h": 12, "4h": 48, "24h": 288, "72h": 864}
COSTS = {"bp9.26": 0.000926, "bp19": 0.0019}
_DATA_DIR = os.environ.get("HERMES_DATA_DIR", "/data")

# Fixed liquid basket: large-cap names with uninterrupted 1:1 Binance USDT
# spot history spanning the entire outcomes window. Equal-weighted, exactly as
# reconcile_decision_shadow.enrich averages per-benchant forward returns.
BENCH_BASKET = [
    "BTC", "ETH", "SOL", "BNB", "XRP", "DOGE", "ADA", "AVAX", "LINK", "LTC",
    "TRX", "DOT", "MATIC", "NEAR", "ATOM", "UNI", "ETC", "FIL", "APT", "ARB", "OP",
]


def _load_bench(coins, start_ms, end_ms):
    """coin -> (ts_list, close_list) over closed 5m Binance klines."""
    from hermes_trader.data import binance_candles as B
    series = {}
    for c in coins:
        if B.to_binance_symbol(c) is None:
            continue
        try:
            cdls = [x for x in B.fetch_candle_range(
                c, "5m", start_ms, end_ms, use_cache=True)
                if x.t + BAR_MS <= end_ms]
        except Exception:
            cdls = []
        if cdls:
            series[c] = ([x.t for x in cdls], [float(x.c) for x in cdls])
    B.flush_disk()
    return series


def _bench_fwd(bench_series, t_entry_ms, n_bars, sgn):
    """Equal-weight signed benchmark forward over n_bars from the closed bar
    whose open-time == t_entry_ms (tolerance one 5m bar), mirroring enrich."""
    vals = []
    for bts, bcs in bench_series.values():
        bj = bisect.bisect_right(bts, t_entry_ms - 1) - 1
        if (bj >= 0 and bj + n_bars < len(bcs)
                and abs(bts[bj] - t_entry_ms) <= BAR_MS and bcs[bj] > 0):
            vals.append(sgn * (bcs[bj + n_bars] / bcs[bj] - 1.0))
    return (sum(vals) / len(vals)) if vals else None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--data-dir", default=_DATA_DIR)
    ap.add_argument("--in", dest="inp",
                    default=os.path.join(_DATA_DIR, "decision_shadow_outcomes.jsonl"))
    ap.add_argument("--out", default="")
    ap.add_argument("--basket", nargs="*", default=None)
    args = ap.parse_args()

    rows = [json.loads(l) for l in open(args.inp) if l.strip()]
    now_ms = int(time.time() * 1000)

    # fetch window: earliest entry - 1 bar through latest entry + 72h (capped at
    # last closed bar; Binance returns only closed history anyway).
    entry_ts = [r["entry_ts"] for r in rows if r.get("entry_ts")]
    start_ms = min(entry_ts) - BAR_MS
    end_ms = min(max(entry_ts) + 72 * 3_600_000 + BAR_MS,
                 now_ms - (now_ms % BAR_MS))
    basket = args.basket or BENCH_BASKET
    bench_series = _load_bench(basket, start_ms, end_ms)

    repaired = {hz: 0 for hz in HORIZONS}
    already = {hz: 0 for hz in HORIZONS}
    still_missing = {hz: 0 for hz in HORIZONS}
    bench_used = sorted(bench_series)

    for r in rows:
        gross = r.get("gross_fwd")
        entry = r.get("entry_ts")
        if not isinstance(gross, dict) or not entry:
            continue
        sgn = -1.0 if str(r.get("side")).lower() == "short" else 1.0
        for hz, n in HORIZONS.items():
            g = gross.get(hz)
            if g is None:
                still_missing[hz] += 1
                continue  # leg forward not closed yet / unavailable
            # keep an existing production excess if present
            existing = (r.get("excess_bp19") or {}).get(hz)
            if existing is not None:
                already[hz] += 1
                continue
            bf = _bench_fwd(bench_series, int(entry), n, sgn)
            if bf is None:
                # forward window not closed yet OR no bench bar aligned: leave
                # None honestly (distinguish in counts below via closed check).
                if int(entry) + n * BAR_MS + BAR_MS <= now_ms:
                    still_missing[hz] += 1  # closed but unaligned: real gap
                else:
                    still_missing[hz] += 1  # not yet closed: expected
                continue
            for label, cost in COSTS.items():
                key = f"excess_{label}"
                blk = r.get(key)
                if not isinstance(blk, dict):
                    blk = {}
                blk[hz] = g - bf - cost
                r[key] = blk
            repaired[hz] += 1

    # post coverage
    def cov(hz, label):
        return sum(1 for r in rows
                   if isinstance(r.get(f"excess_{label}"), dict)
                   and r[f"excess_{label}"].get(hz) is not None)

    summary = {
        "meta": {
            "generated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "input": args.inp, "rows": len(rows),
            "bench_basket_req": basket,
            "bench_basket_resolved": bench_used,
            "bench_n": len(bench_used),
            "method": "keep HL gross/net; fill equal-weight Binance benchmark "
                      "on the same closed entry bar; no leg reprice; unclosed "
                      "24h/72h windows left None",
        },
        "repaired_per_horizon": repaired,
        "already_had_excess": already,
        "excess_coverage_after": {hz: {"bp19": cov(hz, "bp19"),
                                       "bp9.26": cov(hz, "bp9.26")}
                                  for hz in HORIZONS},
        "gross_rows": sum(1 for r in rows if isinstance(r.get("gross_fwd"), dict)),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))

    if args.out:
        with open(args.out, "w") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        meta = args.out.replace(".jsonl", "_excess_repair_meta.json")
        with open(meta, "w") as f:
            json.dump(summary, f, ensure_ascii=False, indent=2)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
