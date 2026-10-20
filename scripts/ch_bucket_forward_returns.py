#!/usr/bin/env python3
"""Surface-channel forward-return analysis (point-in-time, offline).

Extension of bf_bucket_forward_returns to the L1-audit F2 question. Signals are
the mutually-exclusive surface channels emitted by ch_signal_extract:
  trend  (uptrend/downtrendMomentum bypass)
  mover  (dailyMover bypass)
  pattern(bullish/bearishReversalCandle bypass)
  burst  (momentumBurst bypass)
  gate   (composite >= 54 with no bypass — the genuine gate admits)
  other

For each signal we attach the same no-look-ahead 15m forward returns as the bf
run (imported), then test three things:

1. Raw per-channel forward returns (long & short convention), median CI.
2. Cross-sectionally de-meaned returns: subtract, per 15m time bucket, the mean
   return of ALL coins signalling that bucket. Removes "when / market beta".
3. Coin-clustered paired contrast (trend vs gate, mover vs gate): collapse each
   coin to its per-channel mean, bootstrap the coins (not the signals) so
   repeated signals on one coin can't fake significance.

Reads CH_SIGNALS (json array), candle cache via the bf OFFLINE path or network
fetch. Writes CH_OUT json and, if CH_ROWS_OUT set, per-signal priced rows.
"""
from __future__ import annotations

import json
import math
import os
import sys
import time
import signal
import datetime as dt
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import importlib.util
_HERE = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location(
    "bf_bucket", os.path.join(_HERE, "bf_bucket_forward_returns.py"))
bf = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bf)

HORIZONS = bf.HORIZONS
STEP = bf.STEP
INTERVAL = bf.INTERVAL

CHANNELS = ("gate", "trend", "mover", "pattern", "burst", "other")
BANDS = (">=54", "45-54", "35-45", "25-35", "<25", "na")
# Median bootstrap sorts n values 2000x; on the 25k-row trend bucket that is
# minutes of pure-Python sorting with no precision gain. Cap the CI sample with
# a deterministic stride (exact median/win/mean below still use every row).
MAX_CI_N = int(os.environ.get("CH_MAX_CI_N", "4000"))
CI_BOOT = int(os.environ.get("CH_CI_BOOT", "1000"))


def _xorshift_deterministic(seed):
    return bf.xorshift64(seed & 0xFFFFFFFFFFFFFFFF)


def agg_h_fast(vals, *, label_seed=0):
    """Exact n/median/win/mean over all values; bootstrap CI on a deterministic
    capped sub-sample for large buckets. Returns the same shape as bf.agg_h."""
    vals = sorted(p for p in vals if p is not None)
    if len(vals) < 5:
        return {"n": len(vals)}
    n = len(vals)
    sample = vals
    if n > MAX_CI_N:
        stride = n / MAX_CI_N
        sample = [vals[min(n - 1, int(i * stride))] for i in range(MAX_CI_N)]
    m = vals[n // 2] if n % 2 else 0.5 * (vals[n // 2 - 1] + vals[n // 2])
    sn = len(sample)
    if sn < 3:
        lo, hi = sample[0], sample[-1]
    else:
        rnd = _xorshift_deterministic(20261020 + sn * 31 + label_seed)
        meds = []
        for _ in range(CI_BOOT):
            s = sorted(sample[rnd() % sn] for _ in range(sn))
            meds.append(s[sn // 2] if sn % 2
                        else 0.5 * (s[sn // 2 - 1] + s[sn // 2]))
        meds.sort()
        lo, hi = meds[int(0.025 * CI_BOOT)], meds[int(0.975 * CI_BOOT)]
    return {
        "n": n,
        "median_pct": round(100 * m, 3),
        "ci_lo_pct": round(100 * lo, 3),
        "ci_hi_pct": round(100 * hi, 3),
        "win_pct": round(100 * sum(1 for v in vals if v > 0) / n, 1),
        "mean_pct": round(100 * sum(vals) / n, 3),
    }


_seed_counter = [0]


def _agg(vals):
    _seed_counter[0] += 1
    return agg_h_fast(vals, label_seed=_seed_counter[0])




def price_signals(signals, *, ts_by_coin, cl_by_coin):
    """Attach bf forward returns, preserving channel/band/fired/verdict."""
    rows = bf.price_rows(
        [{"coin": s["coin"], "ts": s["ts"], "bf": None,
          "verdict": s.get("verdict"), "conf": s.get("conf"),
          "composite": s.get("composite")}
         for s in signals],
        ts_by_coin=ts_by_coin, cl_by_coin=cl_by_coin)
    # price_rows returns rows ordered by coin; re-key by (coin,ts,entry_idx)
    meta = {(s["coin"], s["ts"]): s for s in signals}
    for r in rows:
        s = meta.get((r["coin"], r["ts"]))
        if s is None:
            continue
        r["channel"] = s.get("channel")
        r["band"] = s.get("band")
        r["fired"] = s.get("fired", [])
    return rows


def _time_bucket(ts_ms: int) -> int:
    return ts_ms // STEP


def add_demeaned(rows):
    """Cross-sectional de-meaning within each 15m signal-time bucket.

    For horizon h subtract the mean rl_h across all rows sharing that bucket.
    A bucket with one signal contributes 0 (no cross-section to compare)."""
    buckets = defaultdict(list)
    for r in rows:
        buckets[_time_bucket(r["ts"])].append(r)
    for grp in buckets.values():
        for h in HORIZONS:
            vals = [g["rl_" + h] for g in grp if g.get("rl_" + h) is not None]
            if len(vals) >= 2:
                m = sum(vals) / len(vals)
                for g in grp:
                    if g.get("rl_" + h) is not None:
                        g["dm_" + h] = g["rl_" + h] - m
            else:
                for g in grp:
                    g["dm_" + h] = 0.0 if g.get("rl_" + h) is not None else None


def coin_cluster_contrast(rows, channel, baseline="gate", horizon="24h",
                          field="rl_", n_boot=1000, seed=20261020):
    """Paired, coin-clustered mean difference (channel - baseline).

    Collapse each coin to its mean per channel, keep coins observed in BOTH
    groups, bootstrap the paired coin differences. Returns dict with n_coins,
    point diff (%); CI; and a crude two-sided p from the bootstrap distribution.
    Coin-level clustering is the conservative test (repeated signals don't
    inflate n).
    """
    key = field + horizon

    def coin_means(group):
        acc = defaultdict(list)
        for r in rows:
            if r["channel"] != group:
                continue
            v = r.get(key)
            if v is not None:
                acc[r["coin"]].append(v)
        return {c: sum(v) / len(v) for c, v in acc.items()}

    a = coin_means(channel)
    b = coin_means(baseline)
    common = sorted(set(a) & set(b))
    if len(common) < 5:
        return {"n_coins": len(common), "insufficient": True}
    diffs = [a[c] - b[c] for c in common]
    point = sum(diffs) / len(diffs)
    # deterministic seed (never use built-in hash(): it is per-process salted)
    salt = sum((i + 1) * ord(c) for i, c in enumerate(channel + horizon + field))
    rnd = bf.xorshift64((seed + len(common) * 7919 + salt)
                        & 0xFFFFFFFFFFFFFFFF)
    n = len(diffs)
    boots = []
    for _ in range(n_boot):
        sel = [diffs[rnd() % n] for _ in range(n)]
        boots.append(sum(sel) / n)
    boots.sort()
    lo, hi = boots[int(0.025 * n_boot)], boots[int(0.975 * n_boot)]
    # two-sided bootstrap p (fraction of resampled means on the other side of 0)
    if point >= 0:
        p = max(1.0 / n_boot,
                2.0 * sum(1 for x in boots if x <= 0) / n_boot)
    else:
        p = max(1.0 / n_boot,
                2.0 * sum(1 for x in boots if x >= 0) / n_boot)
    return {"n_coins": n, "point_diff_pct": round(100 * point, 3),
            "ci_lo_pct": round(100 * lo, 3), "ci_hi_pct": round(100 * hi, 3),
            "p_boot": round(min(1.0, p), 4)}


def summarize(rows, *, n_signals, n_coins, fetch_fail):
    result = {"generated_utc": dt.datetime.utcnow().isoformat() + "Z",
              "n_signals": n_signals, "n_rows_priced": len(rows),
              "n_coins": n_coins, "fetch_fail": fetch_fail,
              "by_channel_long": {}, "by_channel_short": {},
              "by_channel_demeaned_long": {}, "by_band_long": {},
              "contrasts_coin_clustered": {}}

    def channel_tables(prefix, field, out):
        for ch in CHANNELS:
            sub = [r for r in rows if r.get("channel") == ch]
            if not sub:
                continue
            stats = {}
            for h in HORIZONS:
                a = _agg([r.get(field + h) for r in sub])
                stats[h] = a
            out[ch] = {"n_signals": len(sub),
                       "n_coins": len({r["coin"] for r in sub}), **stats}

    channel_tables("raw", "rl_", result["by_channel_long"])
    # short convention = negation
    for ch, tab in result["by_channel_long"].items():
        result["by_channel_short"][ch] = {
            k: (bf._flip_short(v) if isinstance(v, dict) and "median_pct" in v
                else v) for k, v in tab.items()}
    add_demeaned(rows)
    channel_tables("dm", "dm_", result["by_channel_demeaned_long"])

    # composite bands (raw long)
    for band in BANDS:
        sub = [r for r in rows if r.get("band") == band]
        if not sub:
            continue
        result["by_band_long"][band] = {
            "n_signals": len(sub),
            "n_coins": len({r["coin"] for r in sub}),
            **{h: _agg([r.get("rl_" + h) for r in sub]) for h in HORIZONS}}

    # coin-clustered contrasts for the two dominant bypasses vs genuine gate
    contrasts = {}
    for ch in ("trend", "mover"):
        for field in ("rl_", "dm_"):
            label = f"{ch}_vs_gate_{'raw' if field=='rl_' else 'demeaned'}"
            contrasts[label] = {h: coin_cluster_contrast(
                rows, ch, "gate", h, field) for h in HORIZONS}
    result["contrasts_coin_clustered"] = contrasts
    return result


def _load_offline():
    raw = json.load(open(bf.CACHE_PATH, encoding="utf-8"))
    disk = {}
    for key, bars in raw.items():
        if "|" not in key:
            continue
        coin, iv = key.split("|", 1)
        if iv == INTERVAL:
            disk[coin] = {int(t): tuple(v) for t, v in bars.items()}
    ts_by = {c: sorted(ser) for c, ser in disk.items()}
    cl_by = {c: [ser[t][3] for t in ts_by[c]] for c, ser in disk.items()}
    return ts_by, cl_by


def main() -> int:
    signals = json.load(open(os.environ["CH_SIGNALS"], encoding="utf-8"))
    by_coin = defaultdict(list)
    for s in signals:
        by_coin[s["coin"]].append(s)

    fetch_fail = []
    if os.environ.get("CH_OFFLINE", "0") == "1":
        ts_by, cl_by = _load_offline()
        rows = price_signals(signals, ts_by_coin=ts_by, cl_by_coin=cl_by)
    else:
        from hermes_trader.data.historical_candles import (
            fetch_candle_range, flush_disk_cache)
        t_min = min(s["ts"] for s in signals) - STEP
        t_max = min(int(time.time() * 1000),
                    max(s["ts"] for s in signals) + max(HORIZONS.values()) * STEP)
        rows = []
        signal.signal(signal.SIGALRM, bf._alarm_handler)
        for ci, (coin, ss) in enumerate(sorted(by_coin.items()), 1):
            try:
                signal.setitimer(signal.ITIMER_REAL, bf.FETCH_TIMEOUT_S)
                bars = fetch_candle_range(coin, INTERVAL, t_min, t_max,
                                          use_cache=True)
                signal.setitimer(signal.ITIMER_REAL, 0)
            except bf._FetchTimeout:
                signal.setitimer(signal.ITIMER_REAL, 0)
                fetch_fail.append((coin, "timeout", "")); continue
            except Exception as e:
                signal.setitimer(signal.ITIMER_REAL, 0)
                fetch_fail.append((coin, type(e).__name__, str(e)[:80])); continue
            if not bars:
                fetch_fail.append((coin, "empty", "")); continue
            rows.extend(price_signals(
                ss, ts_by_coin={coin: [b.t for b in bars]},
                cl_by_coin={coin: [b.c for b in bars]}))
            if ci % bf.FLUSH_EVERY == 0:
                flush_disk_cache(bf.CACHE_PATH)
                print(f"fetched {ci}/{len(by_coin)} rows={len(rows)}",
                      flush=True)
        signal.setitimer(signal.ITIMER_REAL, 0)
        flush_disk_cache(bf.CACHE_PATH)

    result = summarize(rows, n_signals=len(signals),
                       n_coins=len(by_coin), fetch_fail=fetch_fail)
    out = os.environ.get("CH_OUT", "/tmp/ch_result.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2)
    if os.environ.get("CH_ROWS_OUT"):
        with open(os.environ["CH_ROWS_OUT"], "w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, separators=(",", ":")) + "\n")
    print("WROTE", out, "rows=", len(rows), "fail=", len(fetch_fail))
    return 0


if __name__ == "__main__":
    sys.exit(main())
