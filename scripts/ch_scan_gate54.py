#!/usr/bin/env python3
"""Should the SCAN surfacing floor minCompositeScore=54 be lowered? (offline PIT)

The TA review (2026-10-24) noted 54 as a surfacing floor, but that floor is NOT
a hard gate: perception drops a candidate only when
``score < 54 AND not burst AND not trend_bypass AND not pattern AND not
daily_mover AND not sigma AND not whale``. This study isolates the population a
lower floor would actually newly surface: fired bars with NO trigger-based
bypass (momentumBurst / uptrend|downtrendMomentum), bucketed by the NEW
production-weight composite, and asks whether the sub-54 buckets have positive
forward LONG returns.

Caveats (same as ch_weight_calib):
  * mechanical TA only — NO LLM verdict, no confidence, no late-chase/fresh
    gates, no research cooldown. fwd is the long-structured composite payoff
    (allow_shorts=false), not a backtest of the full strategy;
  * pattern/daily_mover/sigma/whale bypasses are external to build_hits and are
    treated as absent, so "no_bypass" here means no burst/trend bypass;
  * trend surfacing is suppressed in chop regime in production; this study
    flags trend-fired separately so the chop nuance is not silently ignored;
  * evaluates every CH_SCAN_STEPth closed 5m bar (default 6 = 30min grid).

PIT: 1h/15m feeds are the bars already closed at each 5m close; forward returns
use only subsequent 15m closes (4h/12h/24h) net of taker round-trip 9bp at 3x.
"""
from __future__ import annotations

import bisect
import json
import os
import sys

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO)

from hermes_trader.agents.config import (  # noqa: E402
    trigger_thresholds_params, trigger_weights_params)
from hermes_trader.data import historical_candles as hc  # noqa: E402
from hermes_trader.indicators import triggers as trig  # noqa: E402

TAKER_RT_PCT = 0.09
LEV = 3
TAIL = 500
WARMUP_1H = 100
FWD = {"4h": 16, "12h": 48, "24h": 96}
EXC_15M = 96
FLOOR = 54.0
# trigger-based bypasses reproducible inside build_hits (perception surfacing)
BYPASS_TRIGGERS = {"momentumBurst", "uptrendMomentum", "downtrendMomentum"}
TREND_BYPASS = {"uptrendMomentum", "downtrendMomentum"}
BUCKETS = [(0, 30), (30, 35), (35, 40), (40, 45), (45, 50), (50, 54), (54, 200)]


def _load_config():
    p = os.environ.get("CH_LIVE_CONFIG", "/tmp/v2/live-agent-config.json")
    try:
        return json.load(open(p))
    except OSError:
        return {}


def _bucket(s):
    for lo, hi in BUCKETS:
        if lo <= s < hi:
            return f"[{lo},{hi})"
    return "?"


def _fwd(t15, c15, h15, l15, i_entry):
    out = {}
    ok = True
    for lab, h in FWD.items():
        j = i_entry + h
        if j < len(c15):
            out[f"fwd_{lab}"] = round(
                100.0 * (c15[j] / c15[i_entry] - 1.0) * LEV - TAKER_RT_PCT * LEV, 3)
        else:
            ok = False
    end = min(len(c15), i_entry + 1 + EXC_15M)
    if i_entry + 1 < end:
        out["mfe24"] = round(100.0 * (max(h15[i_entry + 1:end]) / c15[i_entry] - 1.0), 3)
        out["mae24"] = round(100.0 * (min(l15[i_entry + 1:end]) / c15[i_entry] - 1.0), 3)
    return out, ok


def _all_cached(coin, interval):
    hc._load_disk_cache()
    m = hc._BAR_CACHE.get((coin, interval), {})
    return [m[t] for t in sorted(m)]


def main() -> int:
    cache = os.environ.get("HERMES_HIST_CANDLE_CACHE", "/data/ch_hist_candles.json")
    hc.set_cache_file(cache)
    cfg = _load_config()
    thresholds = trigger_thresholds_params(config=cfg)
    new_w = trigger_weights_params(config=cfg)
    step = int(os.environ.get("CH_SCAN_STEP", "6"))

    uni_env = os.environ.get("CH_WT_UNIVERSE")
    if uni_env:
        coins = [c.strip() for c in open(uni_env) if c.strip()]
    else:
        book = json.load(open(os.environ.get("CH_BOOK", "/tmp/shadow_book_1024.json")))
        import collections
        opens = [f for f in book["accounts"]["taker"]["fills"] if f.get("type") == "open"]
        cnt = collections.Counter(
            f["coin"].replace(":PERP", "").split(":")[0] for f in opens)
        coins = [c for c, _ in cnt.most_common(40)]
        for c in ("REZ", "ZORA", "GRAM", "INIT"):
            if c not in coins:
                coins.append(c)

    out_path = os.environ.get("CH_SCAN_OUT", "/data/ch_scan_gate54.jsonl")
    n = 0
    with open(out_path, "w", encoding="utf-8") as fo:
        for coin in coins:
            b5, b15, b1h = _all_cached(coin, "5m"), _all_cached(coin, "15m"), _all_cached(coin, "1h")
            if len(b5) < TAIL or len(b1h) < WARMUP_1H:
                print(f"skip {coin}: 5m={len(b5)} 1h={len(b1h)}", flush=True)
                continue
            t15 = [b.t for b in b15]; c15 = [b.c for b in b15]
            h15 = [b.h for b in b15]; l15 = [b.l for b in b15]
            t1h = [b.t for b in b1h]
            for i in range(TAIL, len(b5) - 1, step):
                close_ms = b5[i].t + 5 * 60_000
                k1h = bisect.bisect_right(t1h, close_ms - 3_600_000)
                k15 = bisect.bisect_right(t15, close_ms - 900_000)
                if k1h < WARMUP_1H or k15 < 200:
                    continue
                win = b5[max(0, i - TAIL + 1): i + 1]
                hits, _ = trig.build_hits(
                    list(win), thresholds, new_w,
                    candles_1h=list(b1h[:k1h]), candles_15m=list(b15[:k15]))
                fired = {h["name"] for h in hits if h.get("fired")}
                if not fired:
                    continue
                s = trig.composite_score(hits, new_w)
                bypass = fired & BYPASS_TRIGGERS
                i15 = bisect.bisect_right(t15, b5[i].t) - 1
                fr, ok = _fwd(t15, c15, h15, l15, i15)
                if not ok:
                    continue
                row = {"coin": coin, "ts": b5[i].t, "s_new": round(s, 2),
                       "bucket": _bucket(s),
                       "bypass": sorted(bypass),
                       "has_burst": "momentumBurst" in fired,
                       "has_trend": bool(fired & TREND_BYPASS),
                       "no_bypass": len(bypass) == 0,
                       "fired": sorted(fired), **fr}
                fo.write(json.dumps(row, separators=(",", ":")) + "\n")
                n += 1
    print("rows", n, "->", out_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
