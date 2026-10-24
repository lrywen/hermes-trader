#!/usr/bin/env python3
"""Weight x min_composite recalibration counterfactual (offline, PIT).

Question (v11 review §2.1, 2026-10-24): the trigger_weights promote moved
trend_strength 0.55 -> 0.30 and added uptrend_momentum 0 -> 0.20, but
min_composite stayed at 45. Did that silently cull setups that USED to clear
the gate, and — if so — were those culled setups good or bad?

Method (mechanical TA only; no LLM verdict, same caveat as heuristic_signals):
  * For every CLOSED 5m bar of each coin, run the production trigger stack once
    via triggers.build_hits (hits are weight-independent). Score the SAME hits
    under OLD weights (trend_strength 0.55, uptrend/downtrend_momentum 0) and
    NEW weights (production: 0.30 / 0.20). 1h/15m feeds are the PIT bars closed
    at that 5m close — no look-ahead.
  * Classify each fired bar against gate G=45 into four quadrants:
      keep_pass  (passes both) , culled (OLD passes, NEW fails)  <- key group
      new_pass   (NEW passes only), keep_block (fails both)
  * Attach forward LONG returns from subsequent 15m bars (4h/12h/24h) net of
    taker round-trip (9 bps) at 3x, plus 24h MFE/MAE. Direction uses the
    long-structured composite (matches allow_shorts=false production state).
  * Also report a rescaled gate G' = 45 * (sumNEW / sumOLD): the threshold that
    preserves the OLD normalization after the weight change.

Thresholds are held IDENTICAL between arms (isolates the weight effect; the
3bar/3% momentum candidate from weight_shadow is deliberately NOT included).

Env: HERMES_HIST_CANDLE_CACHE (bar store), CH_WT_OUT (jsonl), CH_WT_UNIVERSE,
CH_WT_GATE (default 45), CH_WT_STEP (evaluate every Nth 5m bar, default 1),
CH_LIVE_CONFIG (production config for weights/thresholds).
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

TAKER_RT_PCT = 0.09          # round-trip taker fee, % (0.045%/side)
LEV = 3
TAIL = 500                   # 5m bars fed per evaluation (backtest convention)
WARMUP_1H = 100              # need ~100 closed 1h for trend_strength
FWD = {"4h": 16, "12h": 48, "24h": 96}   # in 15m bars
EXC_15M = 96


def _load_config():
    p = os.environ.get("CH_LIVE_CONFIG", "/tmp/v2/live-agent-config.json")
    try:
        return json.load(open(p))
    except OSError:
        return {}


def _weight_arms(new_w: dict) -> tuple[dict, dict]:
    old_w = dict(new_w)
    # Production pre-promote values (v11 §2): trend 0.55, momentum legs 0.
    old_w["trendStrength"] = 0.55
    old_w["uptrendMomentum"] = 0.0
    old_w["downtrendMomentum"] = 0.0
    return old_w, new_w


def _fwd(ts15, c15, h15, l15, i_entry):
    out = {}
    ok = True
    for lab, h in FWD.items():
        j = i_entry + h
        if j < len(c15):
            spot = 100.0 * (c15[j] / c15[i_entry] - 1.0)
            out[f"fwd_{lab}"] = round(spot * LEV - TAKER_RT_PCT * LEV, 3)
        else:
            ok = False
    end = min(len(c15), i_entry + 1 + EXC_15M)
    if i_entry + 1 < end:
        out["mfe24"] = round(100.0 * (max(h15[i_entry + 1:end]) / c15[i_entry] - 1.0), 3)
        out["mae24"] = round(100.0 * (min(l15[i_entry + 1:end]) / c15[i_entry] - 1.0), 3)
    return out, ok


def _all_cached(coin: str, interval: str):
    """Read the full cached series for (coin, interval), ascending.

    The fetch span guard rejects an artificial 0..10^13 range; the bars are
    already resident from ch_weight_calib_fetch, so read the cache map direct.
    """
    hc._load_disk_cache()
    m = hc._BAR_CACHE.get((coin, interval), {})
    return [m[t] for t in sorted(m)]


def main() -> int:
    cache = os.environ.get("HERMES_HIST_CANDLE_CACHE", "/data/ch_hist_candles.json")
    hc.set_cache_file(cache)
    cfg = _load_config()
    thresholds = trigger_thresholds_params(config=cfg)
    new_w = trigger_weights_params(config=cfg)
    old_w, new_w = _weight_arms(new_w)
    sum_old, sum_new = sum(old_w.values()), sum(new_w.values())
    gate = float(os.environ.get("CH_WT_GATE", "45"))
    gate_rescaled = gate * sum_new / sum_old
    step = int(os.environ.get("CH_WT_STEP", "1"))

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

    out_path = os.environ.get("CH_WT_OUT", "/data/ch_weight_calib.jsonl")
    n = 0
    counts = {"keep_pass": 0, "culled": 0, "new_pass": 0, "keep_block": 0, "fired": 0}
    with open(out_path, "w", encoding="utf-8") as fo:
        for coin in coins:
            b5 = _all_cached(coin, "5m")
            b15 = _all_cached(coin, "15m")
            b1h = _all_cached(coin, "1h")
            if len(b5) < TAIL or len(b1h) < WARMUP_1H:
                print(f"skip {coin}: 5m={len(b5)} 1h={len(b1h)}", flush=True)
                continue
            t15 = [b.t for b in b15]
            c15 = [b.c for b in b15]
            h15 = [b.h for b in b15]
            l15 = [b.l for b in b15]
            t1h = [b.t for b in b1h]
            coin_n = 0
            # evaluate closed 5m bars; need >=TAIL history and 1h warmup
            for i in range(TAIL, len(b5) - 1, step):
                close_ms = b5[i].t + 5 * 60_000
                # PIT 1h/15m: bars already closed at this 5m close
                k1h = bisect.bisect_right(t1h, close_ms - 3_600_000)
                k15 = bisect.bisect_right(t15, close_ms - 900_000)
                if k1h < WARMUP_1H or k15 < 200:
                    continue
                win = b5[max(0, i - TAIL + 1): i + 1]
                hits, _ = trig.build_hits(
                    list(win), thresholds, new_w,
                    candles_1h=list(b1h[:k1h]),
                    candles_15m=list(b15[:k15]))
                if not any(h.get("fired") for h in hits):
                    continue
                counts["fired"] += 1
                s_old = trig.composite_score(hits, old_w)
                s_new = trig.composite_score(hits, new_w)
                p_old, p_new = s_old >= gate, s_new >= gate
                if p_old and p_new:
                    q = "keep_pass"
                elif p_old and not p_new:
                    q = "culled"
                elif (not p_old) and p_new:
                    q = "new_pass"
                else:
                    q = "keep_block"
                counts[q] += 1
                if q == "keep_block":
                    continue  # don't persist rows that pass neither (bulk)
                fired = [h["name"] for h in hits if h.get("fired")]
                i15 = bisect.bisect_right(t15, b5[i].t) - 1
                fr, _ = _fwd(t15, c15, h15, l15, i15)
                row = {"coin": coin, "ts": b5[i].t, "quad": q,
                       "s_old": round(s_old, 2), "s_new": round(s_new, 2),
                       "fired": fired, **fr}
                fo.write(json.dumps(row, separators=(",", ":")) + "\n")
                fo.flush()
                n += 1
                coin_n += 1
            print(f"{coin:10} persisted={coin_n}", flush=True)

    meta = {"sum_old": sum_old, "sum_new": sum_new, "gate": gate,
            "gate_rescaled": round(gate_rescaled, 2), "counts": counts,
            "rows_written": n}
    json.dump(meta, open(out_path + ".meta.json", "w"), indent=1)
    print("META", json.dumps(meta))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
