#!/usr/bin/env python3
"""Offline: does the [43,47) ADX taper move the weight-recalibration baselines?

Re-checks the two baselines flagged stale by the 2026-10-24 selection review
(ch_weight_calib "pass-45", ch_scan_gate54 "score floor") under BOTH ADX score
curves with the CURRENT trigger weights, so weight change and curve change are
not confounded:

  * legacy curve : hard branch adx>45 -> x0.5 (pre-f51ce79)
  * taper curve  : symmetric [43,47) band (current production, 831fd33)

Only the trendStrength hit score differs between curves, so we run build_hits
once per sampled bar (current/taper curve) and reconstruct the legacy
trendStrength score from the same ADX reading, applying the score delta to the
composite. Mechanical TA only; PIT feeds; offline, never enters the image.
"""
from __future__ import annotations

import bisect
import collections
import json
import math
import os
import sys

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO)

from hermes_trader.agents.config import (  # noqa: E402
    trigger_thresholds_params, trigger_weights_params)
from hermes_trader.data import historical_candles as hc  # noqa: E402
from hermes_trader.indicators import triggers as trig  # noqa: E402

TAIL = 500
# Indicators used by build_hits need only a bounded tail (ADX(14), BB(20,2)
# over last 100, breakout lookback). Truncating keeps the per-bar cost bounded;
# production perception likewise scores the recent window, not all history.
MAX_1H = 200
MAX_15M = 300
WARMUP_1H = 60
BYPASS = {"momentumBurst", "uptrendMomentum", "downtrendMomentum"}
_TW = "trendStrength"


def _all_cached(coin, interval):
    hc._load_disk_cache()
    m = hc._BAR_CACHE.get((coin, interval), {})
    return [m[t] for t in sorted(m)]


def _legacy_ts_score(adx_val: float) -> tuple[float, bool]:
    """Pre-f51ce79 trendStrength score/fired for an ADX reading."""
    if not math.isfinite(adx_val) or adx_val < 25:
        return 0.0, False
    base = min(10, max(0, adx_val / 4)) * (0.5 if adx_val > 45 else 1.0)
    return base, True


def main() -> int:
    hc.set_cache_file(os.environ.get("HERMES_HIST_CANDLE_CACHE",
                                     "/tmp/v2/ch_hist_candles.json"))
    cfg = json.load(open(os.environ.get("CH_LIVE_CONFIG",
                                        "/tmp/v2/live-agent-config.json")))
    thresholds = trigger_thresholds_params(config=cfg)
    weights = trigger_weights_params(config=cfg)
    wsum = sum(weights.values())
    step = int(os.environ.get("CH_SCAN_STEP", "6"))
    book = json.load(open(os.environ.get("CH_BOOK", "/tmp/v2/shadow_book_1024.json")))
    opens = [f for f in book["accounts"]["taker"]["fills"] if f.get("type") == "open"]
    cnt = collections.Counter(f["coin"].replace(":PERP", "").split(":")[0] for f in opens)
    coins = [c for c, _ in cnt.most_common(40)]
    for c in ("REZ", "ZORA", "GRAM", "INIT"):
        if c not in coins:
            coins.append(c)

    st = {k: {"bars": 0, "pass45": 0, "pass45_nb": 0, "pass54": 0, "pass54_nb": 0}
          for k in ("legacy", "taper_43_47")}

    for coin in coins:
        b5, b15, b1h = _all_cached(coin, "5m"), _all_cached(coin, "15m"), _all_cached(coin, "1h")
        if len(b5) < TAIL or len(b1h) < WARMUP_1H:
            continue
        t15 = [b.t for b in b15]
        t1h = [b.t for b in b1h]
        for i in range(TAIL, len(b5) - 1, step):
            close_ms = b5[i].t + 5 * 60_000
            k1h = bisect.bisect_right(t1h, close_ms - 3_600_000)
            k15 = bisect.bisect_right(t15, close_ms - 900_000)
            if k1h < WARMUP_1H or k15 < 120:
                continue
            win = list(b5[max(0, i - TAIL + 1): i + 1])
            w1h = list(b1h[max(0, k1h - MAX_1H): k1h])
            w15 = list(b15[max(0, k15 - MAX_15M): k15])
            hits, _ = trig.build_hits(win, thresholds, weights,
                                      candles_1h=w1h, candles_15m=w15)
            fired = {h["name"] for h in hits if h.get("fired")}
            if not fired:
                continue
            s_new = trig.composite_score(hits, weights)
            # Reconstruct legacy trendStrength score from the same ADX reading.
            ts = next((h for h in hits if h["name"] == _TW), None)
            adx_val = trig.adx(w1h, thresholds.get("adxPeriod", 14))[-1]
            legacy_ts, ts_fired = _legacy_ts_score(adx_val)
            new_ts = ts["score"] if ts else 0.0
            # composite delta from swapping only this trigger's score
            s_old = s_new + (legacy_ts - new_ts) * weights.get(_TW, 0.0) / wsum * 10
            nob = not (fired & BYPASS)
            for name, s in (("taper_43_47", s_new), ("legacy", s_old)):
                d = st[name]
                d["bars"] += 1
                if s >= 45:
                    d["pass45"] += 1
                    d["pass45_nb"] += 1 if nob else 0
                if s >= 54:
                    d["pass54"] += 1
                    d["pass54_nb"] += 1 if nob else 0

    print(f"weights sum={wsum:.2f} step={step} coins={len(coins)}")
    print(f"{'curve':12} {'firedBars':>9} {'pass45':>7} {'pass45_nb':>9} "
          f"{'pass54':>7} {'pass54_nb':>9}")
    for name, d in st.items():
        print(f"{name:12} {d['bars']:9d} {d['pass45']:7d} {d['pass45_nb']:9d} "
              f"{d['pass54']:7d} {d['pass54_nb']:9d}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
