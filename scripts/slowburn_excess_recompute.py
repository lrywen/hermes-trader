#!/usr/bin/env python3
"""slowBurn B-binary, MARKET-NEUTRAL (excess) recompute — A4 increment.

The 2026-11-03 slowburn_ic report flagged its 24h delta (-406bp) as FALSELY
significant because the equal-weight benchmark was missing (only 6 excess rows),
so long-horizon net was dominated by market beta, not by slowBurn. A4 repaired
the benchmark (decision_shadow_outcomes_excess_2026-11-04.jsonl now carries
excess_bp19/excess_bp9.26 on ~1.3k rows). This recomputes the B-binary
slowBurn-in-fired_names contrast on EXCESS (market-neutral) returns so the
24h/72h question can finally be answered on beta-removed grounds.

Same estimator as the original report: day-block bootstrap, seed=7, B=4000,
direction-aware signs already baked into the excess columns by the repair.
Descriptive only; pre-registered promotion gate (n>=100 AND d>=30) unchanged.
"""
import json, random, statistics as st, time
from collections import defaultdict

SRC = "/data/decision_shadow_outcomes_excess_2026-11-04.jsonl"
COST = 0.0019

def dayblock(xs, days, B=4000, seed=7):
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
    return {"n": len(xs), "days": k, "mean_bp": round(1e4*st.mean(xs),2),
            "ci95_bp": [round(1e4*means[int(.025*B)],2), round(1e4*means[int(.975*B)],2)]}

def paired_delta(T, Td, F, Fd, seed=7):
    a, b = defaultdict(list), defaultdict(list)
    for x,d in zip(T,Td): a[d].append(x)
    for x,d in zip(F,Fd): b[d].append(x)
    am = {d:st.mean(v) for d,v in a.items()}
    bm = {d:st.mean(v) for d,v in b.items()}
    common = sorted(set(am)&set(bm))
    diffs = [am[d]-bm[d] for d in common]
    if len(diffs) < 2:
        return {"common_days": len(diffs), "delta_bp": None, "ci95_bp":[None,None]}
    rnd = random.Random(seed)
    bs = sorted(st.mean(rnd.choice(diffs) for _ in range(len(diffs))) for _ in range(4000))
    return {"common_days": len(diffs), "delta_bp": round(1e4*st.mean(diffs),2),
            "ci95_bp":[round(1e4*bs[100],2), round(1e4*bs[3899],2)]}

rows = [json.loads(l) for l in open(SRC) if l.strip()]

def has_sb(r):
    fn = r.get("fired_names")
    if fn:
        return "slowBurn" in fn
    return int(r.get("slow_burn_count") or 0) >= 1

out = {"report": "slowBurn B-binary MARKET-NEUTRAL (excess) recompute",
       "generated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
       "source": SRC, "cost_convention": "excess_bp19 = signed(gross-bench)-0.0019",
       "bootstrap": "day-block means seed=7 B=4000",
       "decision": "DESCRIPTIVE ONLY; promotion gate n>=100 AND d>=30 unchanged",
       "by_horizon": {}}

for hz in ("1h","4h","24h","72h"):
    for side in ("all","long","short"):
        T,Td,F,Fd = [],[],[],[]
        for r in rows:
            if side != "all" and r.get("side") != side:
                continue
            ex = r.get("excess_bp19")
            t = r.get("entry_ts")
            if not isinstance(ex, dict) or ex.get(hz) is None or not t:
                continue
            d = int(t)//86_400_000
            if has_sb(r):
                T.append(float(ex[hz])); Td.append(d)
            else:
                F.append(float(ex[hz])); Fd.append(d)
        res = {"T": dayblock(T,Td), "F": dayblock(F,Fd),
               "delta_T_minus_F": paired_delta(T,Td,F,Fd)}
        out["by_horizon"].setdefault(hz, {})[side] = res

print(json.dumps(out, ensure_ascii=False, indent=2))
with open("/data/research_slowburn_excess_2026-11-04.json","w") as f:
    f.write(json.dumps(out, ensure_ascii=False, indent=2)+"\n")
