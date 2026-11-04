#!/usr/bin/env python3
"""A1/U4 + C8 + C9 (2026-11-04): close the historical short-side evidence gaps
using ONLY already-recorded outcomes + already-closed historical candles.

Context / provenance
--------------------
Every short is short-circuited at the TOP of the runner gate by
"runner_gate_blocked (shorts disabled)" (executor.py, scan_long_only=true /
allow_shorts=false) BEFORE the regime gate runs. As a direct consequence the
1,857 historical short rows in decision_shadow_outcomes.jsonl have
regime_gate / regime_decision / regime_gate_score / flow_* / trigger_hits all
null — those keys were never evaluated on the short path (and the U1 snapshot
only exists from 2026-11-04 02:40 UTC). We cannot wait for new data, so:

  * U4 short x regime: COUNTERFACTUAL-descriptive. Production regime for the
    crypto class is the BTC 1h PROXY (CRYPTO_PROXY="BTC", classify_candles +
    regime_strength_score over the last 100 CLOSED 1h bars). We recompute that
    exact proxy PIT at every short row's entry_ts from already-closed BTC 1h
    klines (offline Binance source; venue trend is equivalent, noted). The
    regime gate never actually RAN on these rows, so this answers "if shorts
    were allowed, what regime/forward-return bucket would they have sat in" —
    it is NOT an as-produced gate verdict.

  * C8 short flow: flow_confirm comes from the live WS taker-trade memory and
    is not reconstructible from closed candles (tape logs only BTC/ETH). The
    honest result is a NULL-AUDIT, not an imputation; we quantify the gap and
    explicitly do NOT fabricate flow values.

  * C9 $50M short 24h-volume floor: the rows already carry volume_24h_usd and
    direction-corrected forward net; split floor-blocked vs floor-passed and
    day-block bootstrap the difference (same estimator as b4_short_floor, run
    directly over outcomes so it is not limited to the 8h shadow window).

Read-only; writes only the two --out artifacts. Never orders.
"""
from __future__ import annotations

import argparse
import bisect
import json
import os
import random
import statistics as st
import time
from collections import Counter, defaultdict

_DATA_DIR = os.environ.get("HERMES_DATA_DIR", "/data")
COST = 0.0019
FLOOR = 50e6


def _pit_proxy_series(interval="1h"):
    """BTC proxy closed klines (offline source)."""
    from hermes_trader.data import binance_candles as B
    # wide enough to cover all outcomes entry_ts with 100 prior bars
    return B


def _classify_pit(candles_1h):
    from hermes_trader.agents.market_regime import (
        classify_candles, regime_strength_score)
    return classify_candles(candles_1h), float(regime_strength_score(candles_1h))


def _dayblock(xs, days, B=4000, seed=7):
    if len(xs) < 2:
        return {"n": len(xs), "days": len(set(days)), "mean_bp": None,
                "ci95_bp": [None, None]}
    byd = defaultdict(list)
    for x, d in zip(xs, days):
        byd[d].append(x)
    blocks = [st.mean(v) for v in byd.values()]
    k = len(blocks)
    rnd = random.Random(seed)
    means = sorted(st.mean(rnd.choice(blocks) for _ in range(k)) for _ in range(B))
    return {"n": len(xs), "days": k,
            "mean_bp": round(1e4 * st.mean(xs), 2),
            "ci95_bp": [round(1e4 * means[int(.025 * B)], 2),
                       round(1e4 * means[int(.975 * B)], 2)]}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--data-dir", default=_DATA_DIR)
    ap.add_argument("--horizon", default="1h")
    ap.add_argument("--floor", type=float, default=FLOOR)
    ap.add_argument("--out", default="")
    args = ap.parse_args()
    hz = args.horizon

    rows = [json.loads(l) for l in open(
        os.path.join(args.data_dir, "decision_shadow_outcomes.jsonl")) if l.strip()]
    shorts = [r for r in rows if r.get("side") == "short"]

    # ---- PIT BTC-proxy regime over closed 1h klines -----------------------
    from hermes_trader.models.types import Candle
    from hermes_trader.data import binance_candles as B
    entry_ts = [r["entry_ts"] for r in shorts if r.get("entry_ts")]
    start = min(entry_ts) - 120 * 3_600_000
    end = int(time.time() * 1000)
    raw = list(B.fetch_candle_range("BTC", "1h", start, end, use_cache=True))
    raw = [c for c in raw if c.t + 3_600_000 <= end]
    bts = [c.t for c in raw]
    B.flush_disk()

    def regime_at(t_ms):
        # last 100 1h bars CLOSED as of t_ms (c.t + 1h <= t_ms)
        j = bisect.bisect_right(bts, t_ms - 3_600_000)
        win = raw[max(0, j - 100):j]
        if len(win) < 50:
            return None, None
        return _classify_pit(win)

    reg_counter = Counter()
    reg_net = defaultdict(list)
    reg_days = defaultdict(list)
    score_by_reg = defaultdict(list)
    n_regime_resolved = 0
    for r in shorts:
        t = r.get("entry_ts")
        if not t:
            continue
        reg, score = regime_at(int(t))
        r["_pit_btc_regime"] = reg
        r["_pit_btc_regime_score"] = score
        if reg is not None:
            n_regime_resolved += 1
            reg_counter[reg] += 1
            if score is not None:
                score_by_reg[reg].append(score)
        blk = r.get("net_bp19")
        if reg and isinstance(blk, dict) and blk.get(hz) is not None:
            reg_net[reg].append(float(blk[hz]))
            reg_days[reg].append(int(t) // 86_400_000)

    regime_table = {}
    for reg in ("up", "down", "neutral", "chop"):
        regime_table[reg] = {
            "candidate_rows": reg_counter.get(reg, 0),
            "score_mean": (round(st.mean(score_by_reg[reg]), 3)
                           if score_by_reg.get(reg) else None),
            "net_bp19": _dayblock(reg_net.get(reg, []), reg_days.get(reg, [])),
        }

    # ---- C8 flow NULL audit (NO imputation) -------------------------------
    flow_keys = ["flow_data_available", "flow_confirm_raw",
                 "flow_confirmed_breakout"]
    flow_audit = {k: {"non_null": sum(1 for r in shorts if r.get(k) is not None),
                      "null": sum(1 for r in shorts if r.get(k) is None)}
                  for k in flow_keys}
    # newer post-U1 shadow rows (not yet joined into outcomes) — check shadow file
    sh_path = os.path.join(args.data_dir, "runner_gate_shadow.jsonl")
    sh = [json.loads(l) for l in open(sh_path) if l.strip()]
    sh_short = [r for r in sh if r.get("side") == "short"]
    post_flow = Counter()
    for r in sh_short:
        post_flow[r.get("flow_data_available")] += 1

    # ---- C9 $50M floor, directly over outcomes ----------------------------
    lt, lt_d, ge, ge_d = [], [], [], []
    tier_x, tier_d = defaultdict(list), defaultdict(list)
    miss_vol = 0
    for r in shorts:
        blk = r.get("net_bp19")
        t = r.get("entry_ts")
        if not isinstance(blk, dict) or blk.get(hz) is None or not t:
            continue
        v = r.get("volume_24h_usd")
        d = int(t) // 86_400_000
        if v is None:
            miss_vol += 1
            continue
        (ge if float(v) >= args.floor else lt).append(float(blk[hz]))
        (ge_d if float(v) >= args.floor else lt_d).append(d)
        tier = r.get("volume_tier") or "unknown"
        tier_x[tier].append(float(blk[hz])); tier_d[tier].append(d)

    def daymeans(x, dd):
        byd = defaultdict(list)
        for a, b in zip(x, dd):
            byd[b].append(a)
        return {d: st.mean(v) for d, v in byd.items()}
    a, bm = daymeans(ge, ge_d), daymeans(lt, lt_d)
    common = sorted(set(a) & set(bm))
    diffs = [a[d] - bm[d] for d in common]
    if len(diffs) >= 2:
        rnd = random.Random(71)
        bs = sorted(st.mean(rnd.choice(diffs) for _ in range(len(diffs)))
                    for _ in range(4000))
        diff_stat = {"common_days": len(diffs),
                     "ge_minus_lt_mean_bp": round(1e4 * st.mean(diffs), 2),
                     "ci95_bp": [round(1e4 * bs[100], 2), round(1e4 * bs[3899], 2)]}
    else:
        diff_stat = {"common_days": len(diffs), "ge_minus_lt_mean_bp": None,
                     "ci95_bp": [None, None]}

    report = {
        "report": "U4 short x PIT-BTC-regime (counterfactual) + C8 flow null "
                  "audit + C9 $50M short floor, historical-only",
        "generated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "mode": "SHADOW zero exposure; read-only; no config/gate/admit change",
        "horizon": hz,
        "meta": {
            "short_rows_total": len(shorts),
            "short_rows_with_entry_ts": len(entry_ts),
            "pit_regime_resolved": n_regime_resolved,
            "regime_proxy": "BTC 1h, classify_candles + regime_strength_score, "
                            "last-100 closed bars as-of entry_ts",
            "regime_source": "offline Binance closed 1h klines (production uses "
                             "Hyperliquid BTC; equivalent trend, venue delta noted)",
            "counterfactual_notice": "shorts are blocked BEFORE the regime gate "
                             "runs, so regime here is a PIT recompute, not an "
                             "as-produced gate interaction",
        },
        "U4_short_by_pit_btc_regime": regime_table,
        "C8_flow_null_audit": {
            "historical_outcome_short_rows": flow_audit,
            "interpretation": "flow is WS taker-trade memory, not reconstructible "
                              "from candles; nulls are honest gaps, never imputed",
            "post_U1_live_shadow_short_rows_flow_data_available": dict(post_flow),
        },
        "C9_short_floor_50m": {
            "floor_usd": args.floor,
            "floor_passed_ge": _dayblock(ge, ge_d),
            "floor_blocked_lt": _dayblock(lt, lt_d),
            "missing_volume_rows": miss_vol,
            "by_volume_tier": {t: _dayblock(tier_x[t], tier_d[t])
                               for t in sorted(tier_x)},
            "diff_ge_minus_lt": diff_stat,
            "interpretation": "positive ge-minus-lt with CI>0 supports the floor",
        },
    }
    text = json.dumps(report, ensure_ascii=False, indent=2)
    print(text)
    if args.out:
        with open(args.out, "w") as f:
            f.write(text + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
