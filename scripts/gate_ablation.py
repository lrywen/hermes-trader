"""Stage-4 gate ablation on the 88-day Binance replay (2026-10-30).

Counter-factual pricing of every record-only arm over the FULL admitted set:
for each arm, the rows it WOULD block are sliced and their 1h/24h market-
excess return (excess19_pct, 19bp round-trip cost minus equal-weight market
beta) is day-block bootstrapped. Read-only; touches no live decision.

DATA SOURCES / JOIN KEY (documented so the horizon files are not missed again):
  decision rows : /data/decision_shadow_replay_bn90d.jsonl  (100,387 long
                   decisions, structural gate replay with LLM conf neutralized;
                   carries the feature fields fresh_impulse/slow_burn_count/
                   composite_score/dMover/brk/vol; NO forward returns)
  outcomes      : /data/bn90_outcomes_{1h,24h}.jsonl          (same 100,387
                   rows, forward net19_pct/excess19_pct attached)
  JOIN          : exact (coin, ts) — see arm_edge_eval.load(). The main
                   bn90d_outcomes.jsonl has decisions only; the horizon-split
                   files carry the forward returns and cover ~100% (1h) /
                   ~99% (24h).

Multiplicity: 11 arms x 2 horizons = 22 tests. A nominal 95% day-block CI
excluding 0 is NOT sufficient to enforce; require Bonferroni p<0.05/22=0.0023
AND same sign across 1h+24h. As of this run only R1 (the already-enforced
mover/path-C veto) clears it; S_slow_ge2 is nominal on both horizons but fails
Bonferroni, so it ships SHADOW-ONLY (deep_slow_burn_veto).
"""
import datetime
import math
import os
import random
import sys
from collections import defaultdict

# Run both inside the container (scripts at /app/scripts) and from the repo
# (scripts/ alongside hermes_trader/).
_HERE = os.path.dirname(os.path.abspath(__file__))
if os.path.isdir("/app/scripts"):
    sys.path.insert(0, "/app/scripts")
sys.path.insert(0, _HERE)
from arm_edge_eval import ARMS, load

_DATA = os.environ.get("ABLATION_DATA", "/data")


def summ(pairs):
    pairs=[(v,t) for v,t in pairs if v is not None and math.isfinite(v)]
    if not pairs: return {"n":0}
    byday=defaultdict(list)
    for v,t in pairs: byday[datetime.datetime.utcfromtimestamp(t/1000).date().isoformat()].append(v)
    dm=[sum(d)/len(d) for d in byday.values()]
    mean=sum(v for v,_ in pairs)/len(pairs)
    rnd=random.Random(7);b=[]
    for _ in range(8000): b.append(sum(rnd.choices(dm,k=len(dm)))/len(dm))
    b.sort()
    # 双侧 p：0 落在 bootstrap 分布外的比例
    p=2*min(sum(1 for x in b if x<0)/len(b), sum(1 for x in b if x>0)/len(b))
    return {"n":len(pairs),"days":len(byday),"mean":round(mean,4),
            "ci":[round(b[int(.025*len(b))],4),round(b[int(.975*len(b))],4)],"p":round(min(1,p),4)}

for h in ["1h","24h"]:
    rows=load(f"{_DATA}/decision_shadow_replay_bn90d.jsonl",
              f"{_DATA}/bn90_outcomes_{h}.jsonl")
    adm=[(f,o) for f,o in rows if f.get("admitted")]
    print(f"\n===== {h} EXCESS19 baseline admitted =====")
    base=summ([(o["excess19_pct"],f["ts"]) for f,o in adm])
    print("baseline:",base)
    print(f"{'arm':<24}{'removed':>8}{'mean':>9}  {'CI':>22} {'sig95':>6}")
    for name,fn in ARMS.items():
        rem=[(o["excess19_pct"],f["ts"]) for f,o in adm if fn(f)]
        s=summ(rem)
        sig = s.get("ci") and s["ci"][1]<0
        print(f"{name:<24}{s['n']:>8}{s.get('mean',''):>9}  {str(s.get('ci')):>22} {'NEG' if sig else '':>6}")
