#!/usr/bin/env python3
"""bullish_fired bucket forward-return analysis (point-in-time, offline).

For each conjunction_probe signal (coin, ts, bullish_fired, composite) joined
to the nearest research verdict, compute forward mark-to-market returns from
HISTORICAL 15m candles using the same PIT candle layer as production:

  entry reference = close of the last 15m bar that had CLOSED at signal ts
                    (closed_bars_as_of -> no look-ahead)
  ret_long(h)  = close(t+h)/entry - 1
  ret_short(h) = -(ret_long)

Buckets: by bullish_fired, and within strong-bullish (bf>=3) by AI verdict,
which is the direct test of the reported "AI fades momentum" inversion:
do AI-SHORT calls on strongly-bullish setups actually go on to fall?

This script NEVER trades and writes only the given output path. Run it INSIDE
the container (HL transport + candle cache live there):
  docker exec -i -e HERMES_HIST_CANDLE_CACHE=/tmp/bf_candles.json hermes-trader \
      python3 - < scripts/bf_bucket_forward_returns.py  # args via env
"""
from __future__ import annotations

import json
import math
import os
import signal
import sys
import time
import datetime as dt
from collections import defaultdict

from hermes_trader.data.historical_candles import fetch_candle_range, flush_disk_cache

INTERVAL = "15m"
STEP = 15 * 60_000
# horizon in 15m bars
HORIZONS = {"4h": 16, "12h": 48, "24h": 96, "48h": 192}

# Hard per-coin cap on the HL candle request. A delisted/illiquid xyz series
# can otherwise hang on the shared transport long past its HTTP timeout; this
# is an offline batch job, so a SIGALRM bailout just skips that coin.
FETCH_TIMEOUT_S = float(os.environ.get("BF_FETCH_TIMEOUT_S", "30"))
FLUSH_EVERY = int(os.environ.get("BF_FLUSH_EVERY", "10"))
CACHE_PATH = os.environ.get("HERMES_HIST_CANDLE_CACHE", "/data/.historical-candles.json")


class _FetchTimeout(Exception):
    pass


def _alarm_handler(signum, frame):  # pragma: no cover - timing shim
    raise _FetchTimeout("candle fetch timed out")


def xorshift64(seed: int):
    state = seed & 0xFFFFFFFFFFFFFFFF

    def nxt():
        nonlocal state
        state ^= (state << 13) & 0xFFFFFFFFFFFFFFFF
        state ^= state >> 7
        state ^= (state << 17) & 0xFFFFFFFFFFFFFFFF
        state &= 0xFFFFFFFFFFFFFFFF
        return state

    return nxt


def boot_median_ci(vals, n_boot=2000, seed=20261020):
    """Deterministic bootstrap 95% CI of the median. Returns (med, lo, hi)."""
    vals = sorted(vals)
    n = len(vals)
    if n == 0:
        return None
    med = vals[n // 2] if n % 2 else 0.5 * (vals[n // 2 - 1] + vals[n // 2])
    if n < 3:
        return med, vals[0], vals[-1]
    rnd = xorshift64(seed + n)
    meds = []
    for _ in range(n_boot):
        s = sorted(vals[rnd() % n] for _ in range(n))
        m = s[n // 2] if n % 2 else 0.5 * (s[n // 2 - 1] + s[n // 2])
        meds.append(m)
    meds.sort()
    return med, meds[int(0.025 * n_boot)], meds[int(0.975 * n_boot)]


def price_rows(signals, *, ts_by_coin, cl_by_coin):
    """Attach point-in-time forward returns to each signal.

    ``ts_by_coin[coin]`` are ascending bar-open ms; ``cl_by_coin[coin]`` the
    matching closes. Entry is the last bar that had CLOSED at the signal ts
    (open + STEP <= ts), so no forming/future bar is ever used. Pure and
    network-free so it is unit-testable.
    """
    import bisect
    rows = []
    by_coin = defaultdict(list)
    for s in signals:
        by_coin[s["coin"]].append(s)
    for coin, ss in by_coin.items():
        ts = ts_by_coin.get(coin)
        cl = cl_by_coin.get(coin)
        if not ts:
            continue
        for s in ss:
            sig = s["ts"]
            k = bisect.bisect_right(ts, sig - STEP) - 1
            if k < 0:
                continue
            entry = cl[k]
            if not (entry and math.isfinite(entry)):
                continue
            r = {"bf": s["bf"], "verdict": s.get("verdict"),
                 "conf": s.get("conf"), "composite": s.get("composite"),
                 "coin": coin, "ts": s["ts"], "entry_idx": k}
            for name, nb in HORIZONS.items():
                kk = k + nb
                r["rl_" + name] = (cl[kk] / entry - 1.0
                                   if kk < len(cl) and math.isfinite(cl[kk])
                                   else None)
            rows.append(r)
    return rows


def agg_h(vals):
    """Stats for one horizon from a list of ret_long floats (None ok)."""
    vals = [p for p in vals if p is not None]
    if len(vals) < 5:
        return {"n": len(vals)}
    m, lo, hi = boot_median_ci(vals)
    return {
        "n": len(vals),
        "median_pct": round(100 * m, 3),
        "ci_lo_pct": round(100 * lo, 3),
        "ci_hi_pct": round(100 * hi, 3),
        "win_pct": round(100 * sum(1 for v in vals if v > 0) / len(vals), 1),
        "mean_pct": round(100 * sum(vals) / len(vals), 3),
    }


def _flip_short(a: dict) -> dict:
    """Convert a long-convention horizon stat to short convention."""
    if "median_pct" not in a:
        return a
    return {**a,
            "median_pct": -a["median_pct"],
            "mean_pct": -a["mean_pct"],
            "ci_lo_pct": -a["ci_hi_pct"],
            "ci_hi_pct": -a["ci_lo_pct"],
            "win_pct": round(100 - a["win_pct"], 1)}


def summarize(rows, *, n_signals, n_coins, fetch_fail):
    """Build the bucketed result dict from priced rows."""
    result = {"generated_utc": dt.datetime.utcnow().isoformat() + "Z",
              "n_signals": n_signals, "n_rows_priced": len(rows),
              "n_coins": n_coins, "fetch_fail": fetch_fail,
              "by_bf_long": {}, "by_bf_short": {},
              "strong_bull_verdict_long": {}, "weak_bull_verdict_long": {}}

    for bf in sorted({r["bf"] for r in rows if r["bf"] is not None}):
        sub = [r for r in rows if r["bf"] == bf]
        for side, bucket in (("long", result["by_bf_long"]),
                             ("short", result["by_bf_short"])):
            stats = {}
            for h in HORIZONS:
                a = agg_h([r.get("rl_" + h) for r in sub])
                stats[h] = a if side == "long" else _flip_short(a)
            bucket[str(bf)] = {"n_signals": len(sub), **stats}

    def verdict_table(pool):
        t = {}
        for v in ("LONG", "SHORT", "PASS"):
            sub = [r for r in pool if r["verdict"] == v]
            t[v] = {"n_signals": len(sub),
                    **{h: agg_h([r.get("rl_" + h) for r in sub])
                       for h in HORIZONS}}
        return t

    result["strong_bull_verdict_long"] = verdict_table(
        [r for r in rows if r["bf"] is not None and r["bf"] >= 3])
    result["weak_bull_verdict_long"] = verdict_table(
        [r for r in rows if r["bf"] is not None and r["bf"] <= 1])
    return result


def main() -> int:
    inp = os.environ.get("BF_SIGNALS", "/tmp/bf_signals.json")
    out = os.environ.get("BF_OUT", "/tmp/bf_result.json")
    signals = json.load(open(inp, encoding="utf-8"))

    by_coin = defaultdict(list)
    for s in signals:
        by_coin[s["coin"]].append(s)

    now_ms = int(time.time() * 1000)
    # span needed: earliest signal (minus a bar for entry ref) to latest
    # signal + longest horizon; capped at now (future bars simply absent).
    t_min = min(s["ts"] for s in signals) - STEP
    t_max = min(now_ms, max(s["ts"] for s in signals) + max(HORIZONS.values()) * STEP)

    offline = os.environ.get("BF_OFFLINE", "0") == "1"
    if offline:
        # Read the bar cache directly and NEVER hit the network. Each value is
        # [o,h,l,c,v] keyed by bar-open ms under "coin|interval".
        raw = json.load(open(CACHE_PATH, encoding="utf-8"))
        disk_series: dict[str, dict[int, tuple]] = {}
        for key, bars in raw.items():
            if "|" not in key:
                continue
            coin, iv = key.split("|", 1)
            if iv == INTERVAL:
                disk_series[coin] = {int(t): tuple(v) for t, v in bars.items()}
        ts_by_coin = {c: sorted(ser) for c, ser in disk_series.items()}
        cl_by_coin = {c: [ser[t][3] for t in ts_by_coin[c]]
                      for c, ser in disk_series.items()}
        rows = price_rows(signals, ts_by_coin=ts_by_coin, cl_by_coin=cl_by_coin)
        fetch_fail: list = []
    else:
        # rows carry computed forward returns per horizon, long convention
        rows = []
        fetch_fail = []
        signal.signal(signal.SIGALRM, _alarm_handler)
        for ci, (coin, ss) in enumerate(sorted(by_coin.items()), 1):
            try:
                signal.setitimer(signal.ITIMER_REAL, FETCH_TIMEOUT_S)
                bars = fetch_candle_range(coin, INTERVAL, t_min, t_max, use_cache=True)
                signal.setitimer(signal.ITIMER_REAL, 0)
            except _FetchTimeout:
                signal.setitimer(signal.ITIMER_REAL, 0)
                fetch_fail.append((coin, "timeout", f">{FETCH_TIMEOUT_S:.0f}s"))
                continue
            except Exception as e:  # network / delisting: skip coin
                signal.setitimer(signal.ITIMER_REAL, 0)
                fetch_fail.append((coin, type(e).__name__, str(e)[:80]))
                continue
            if not bars:
                fetch_fail.append((coin, "empty", ""))
                continue
            ts = [b.t for b in bars]
            cl = [b.c for b in bars]
            rows.extend(price_rows(ss, ts_by_coin={coin: ts},
                                   cl_by_coin={coin: cl}))
            if ci % FLUSH_EVERY == 0:
                flush_disk_cache(CACHE_PATH)
                print(f"fetched {ci}/{len(by_coin)} coins, rows={len(rows)}",
                      flush=True)
        signal.setitimer(signal.ITIMER_REAL, 0)
        flush_disk_cache(CACHE_PATH)

    result = summarize(rows, n_signals=len(signals),
                       n_coins=len(by_coin), fetch_fail=fetch_fail)

    rows_out = os.environ.get("BF_ROWS_OUT")
    if rows_out:
        with open(rows_out, "w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, separators=(",", ":")) + "\n")
        print("WROTE_ROWS", rows_out, len(rows), flush=True)

    with open(out, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2)
    print("WROTE", out, "rows_priced =", len(rows),
          "fetch_fail =", len(fetch_fail), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
