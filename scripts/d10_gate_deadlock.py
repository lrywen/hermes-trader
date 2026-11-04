#!/usr/bin/env python3
"""D10 (2026-11-04): is the runner gate DEADLOCKED, and by WHICH gate?

Question from the review backlog: the long admit rate is very low — is the
regime gate freezing otherwise-profitable admits (option A: accept the freeze,
option B: pre-register a falsification)? Historical-only answer over the
excess-repaired outcomes.

Method: bucket every LONG row by its blocking rule family, count block share,
and compute the MARKET-NEUTRAL (excess_bp19) forward return of the rows each
gate removed. A gate that removes rows with NEGATIVE excess is protective; a
gate that removes rows with POSITIVE excess is freezing winners (the deadlock
concern). Day-block bootstrap CI on each bucket. Descriptive; no gate change.
"""
import json, random, statistics as st, time
from collections import defaultdict, Counter

SRC = "/data/decision_shadow_outcomes_excess_2026-11-04.jsonl"

def fam(rule):
    r = rule or ""
    if not r:
        return "ADMIT"
    if "shorts disabled" in r: return "shorts_disabled"
    if "late trend-only chase" in r: return "late_trend_chase"
    if "needs fresh breakout" in r: return "needs_fresh_structure"
    if "confidence" in r: return "ai_confidence"
    if "weak-trend" in r or "ADX" in r: return "weak_trend_adx"
    if "mover_bypass" in r: return "mover_bypass_veto"
    if "regime" in r.lower(): return "regime_gate"
    if "overextension" in r: return "overextension_veto"
    if "slow_burn" in r or "deep_slow" in r: return "deep_slow_burn"
    return "other:" + r[:40]

def dayblock(xs, days, B=4000, seed=7):
    if len(xs) < 2:
        return {"n": len(xs), "days": len(set(days)), "mean_bp": None, "ci95_bp":[None,None], "win_pct": None}
    byd = defaultdict(list)
    for x,d in zip(xs,days): byd[d].append(x)
    blocks=[st.mean(v) for v in byd.values()]; k=len(blocks)
    rnd=random.Random(seed)
    means=sorted(st.mean(rnd.choice(blocks) for _ in range(k)) for _ in range(B))
    return {"n":len(xs),"days":k,"mean_bp":round(1e4*st.mean(xs),2),
            "ci95_bp":[round(1e4*means[int(.025*B)],2),round(1e4*means[int(.975*B)],2)],
            "win_pct":round(100*sum(1 for x in xs if x>0)/len(xs),1)}

rows=[json.loads(l) for l in open(SRC) if l.strip()]
long=[r for r in rows if r.get("side")!="short"]
HZ="1h"
buck=defaultdict(list); buckd=defaultdict(list); cnt=Counter()
for r in long:
    f=fam(r.get("rule"))
    cnt[f]+=1
    ex=r.get("excess_bp19"); t=r.get("entry_ts")
    if isinstance(ex,dict) and ex.get(HZ) is not None and t:
        buck[f].append(float(ex[HZ])); buckd[f].append(int(t)//86_400_000)

total=len(long)
adm=cnt.get("ADMIT",0)
out={"report":"D10 gate deadlock attribution, MARKET-NEUTRAL excess",
     "generated_utc":time.strftime("%Y-%m-%dT%H:%M:%SZ",time.gmtime()),
     "horizon":HZ,"source":SRC,
     "long_rows":total,"admitted":adm,"blocked":total-adm,
     "admit_rate_pct":round(100*adm/total,2),
     "interpretation":"negative excess on a BLOCKED bucket = protective; positive = freezing winners",
     "by_gate_family":[]}
for f,c in cnt.most_common():
    s=dayblock(buck.get(f,[]),buckd.get(f,[]))
    out["by_gate_family"].append({"gate":f,"rows":c,"pct":round(100*c/total,1),
                                  "blocked_excess":s})
print(json.dumps(out,ensure_ascii=False,indent=2))
with open("/data/research_d10_gate_deadlock_2026-11-04.json","w") as fh:
    fh.write(json.dumps(out,ensure_ascii=False,indent=2)+"\n")
