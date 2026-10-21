#!/usr/bin/env python3
"""REGIME-FAITHFUL exit-protect grid over historical LONG signals.

The first grid (ch_exit_param_grid) varied protect_pct against a single neutral
baseline, but production applies a regime overlay via overlay_regime_exit:
  up/down  -> trend_ride: protect 2.5, retrace 0.4, stops 4.0%/20 ROE, stale 120
  chop/neutral (non_trend): protect 1.5, stops 1.5%/15 ROE, stale 60
This replay builds the ACTUAL per-signal policy from the live dsl_exit config
(BTC-proxy regime already attached to each feature row) and compares:
  baseline                 : production regime policy as-is
  nontrend_1.0             : non_trend protect 1.5 -> 1.0 only
  nontrend_0.75            : non_trend protect 1.5 -> 0.75 only
  trend_1.5                : trend protect 2.5 -> 1.5 only
  both_trend1.5_nontrend1.0: the candidate combined change
Same entries / same intrabar path across variants (paired). Offline.

Env: CH_TRADES, CH_DEEP, CH_FEATURES (regime labels), CH_LIVE_CONFIG (the
/etc.. /data/.agent-config.json copy), CH_GRID2_OUT.
"""
from __future__ import annotations

import bisect
import dataclasses
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import importlib.util

_HERE = os.path.dirname(os.path.abspath(__file__))


def _load(name, fn):
    spec = importlib.util.spec_from_file_location(name, os.path.join(_HERE, fn))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


rp = _load("ch_event_replay", "ch_event_replay.py")
grid = _load("ch_exit_param_grid", "ch_exit_param_grid.py")

TREND = {"up", "down"}


def main() -> int:
    from hermes_trader.agents import dsl_exit as dsl_module
    from hermes_trader.agents.regime_exit_common import overlay_regime_exit
    dsl_module._request_save = lambda force=False: None

    cfg = json.load(open(os.environ.get(
        "CH_LIVE_CONFIG", "/tmp/v2/live-agent-config.json")))
    dsl_cfg = cfg.get("dsl_exit", {})
    base = dsl_module._policy_from_dsl_dict(dsl_cfg)

    series = rp._load_cache(os.environ.get(
        "CH_DEEP", "/tmp/v2/ch_deep_candles.json"))
    trades = {(t["coin"], t["ts"]): t for t in
              (json.loads(l) for l in open(os.environ.get(
                  "CH_TRADES", "/tmp/v2/ch_trades.jsonl")))
              if t.get("side") == "LONG"}
    feats = [json.loads(l) for l in open(os.environ.get(
        "CH_FEATURES", "/tmp/v2/ch_features.jsonl"))]

    variants = ["baseline", "nontrend_1.0", "nontrend_0.75",
                "trend_1.5", "both_trend1.5_nontrend1.0"]
    rows = {v: [] for v in variants}

    by_coin = {}
    for f in feats:
        by_coin.setdefault(f["coin"], []).append(f)

    done = 0
    for coin, fs in by_coin.items():
        bars = series.get(coin)
        if not bars:
            continue
        ts = [b[0] for b in bars]
        for f in fs:
            sig = trades.get((coin, f["ts"]))
            if not sig:
                continue
            i = bisect.bisect_left(ts, f["ts"])
            if i <= 0 or i >= len(bars) - 2:
                continue
            regime = f.get("regime") or "neutral"
            pol0, _ = overlay_regime_exit(base, dsl_cfg, regime)
            is_trend = regime in TREND
            cand = {
                "baseline": pol0,
                "nontrend_1.0": dataclasses.replace(
                    pol0, protect_pct=1.0) if not is_trend else pol0,
                "nontrend_0.75": dataclasses.replace(
                    pol0, protect_pct=0.75) if not is_trend else pol0,
                "trend_1.5": dataclasses.replace(
                    pol0, protect_pct=1.5) if is_trend else pol0,
                "both_trend1.5_nontrend1.0": dataclasses.replace(
                    pol0, protect_pct=1.5 if is_trend else 1.0),
            }
            for vname in variants:
                r = grid.replay_one(
                    bars=bars, i_entry=i, policy=cand[vname],
                    dsl_module=dsl_module, tracker_cls=dsl_module.DSLTracker)
                if r is None:
                    continue
                r["regime"] = "trend" if is_trend else "non_trend"
                r["channel"] = f.get("channel")
                rows[vname].append(r)
            done += 1

    def summ(rs):
        import statistics as st
        roes = [r["roe"] for r in rs]
        n = n = len(rs) or 1
        reasons = {}
        for r in rs:
            reasons[r["reason"]] = reasons.get(r["reason"], 0) + 1
        return {
            "n": len(rs),
            "roe_median": round(st.median(roes), 3),
            "roe_mean": round(sum(roes) / len(roes), 4),
            "win_rate": round(100 * sum(x > 0 for x in roes) / n, 2),
            "armed_pct": round(100 * sum(r["armed_protect"] for r in rs) / n, 2),
            "stale_pct": round(100 * reasons.get("stale_flat_timeout", 0) / n, 1),
            "floor_pct": round(100 * reasons.get("floor_breach", 0) / n, 1),
        }

    out = {"entries": done, "overall": {}, "by_regime": {}}
    for v in variants:
        out["overall"][v] = summ(rows[v])
        out["by_regime"][v] = {
            rg: summ([r for r in rows[v] if r["regime"] == rg])
            for rg in ("trend", "non_trend")}
    path = os.environ.get(
        "CH_GRID2_OUT", "/tmp/v2/ch_grid_regime_result.json")
    json.dump(out, open(path, "w"), indent=2)
    print("WROTE", path, "entries", done)
    for v in variants:
        s = out["overall"][v]
        print(f"  {v:26s} medROE={s['roe_median']:>7} mean={s['roe_mean']:>7} "
              f"win={s['win_rate']:>5} arm={s['armed_pct']:>5} "
              f"stale={s['stale_pct']:>5} floor={s['floor_pct']:>5}")
    print("  -- non_trend detail --")
    for v in variants:
        s = out["by_regime"][v]["non_trend"]
        print(f"  {v:26s} n={s['n']:5d} med={s['roe_median']:>7} "
              f"win={s['win_rate']:>5} arm={s['armed_pct']:>5}")
    print("  -- trend detail --")
    for v in variants:
        s = out["by_regime"][v]["trend"]
        print(f"  {v:26s} n={s['n']:5d} med={s['roe_median']:>7} "
              f"win={s['win_rate']:>5} arm={s['armed_pct']:>5}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
