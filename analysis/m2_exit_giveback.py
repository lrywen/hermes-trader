import json,bisect,time,statistics
from collections import defaultdict
from hermes_trader.data import binance_candles as B
BAR=300000;FEE=0.19
def retrace_for(mfe):
    if mfe>=20:return 0.15
    if mfe>=12:return 0.20
    if mfe>=6:return 0.30
    if mfe>=2:return 0.35
    return None  # below protect/phase2 -> not armed
rows=[json.loads(l) for l in open("/data/decision_shadow_replay_bn90d.jsonl")]
adm=[r for r in rows if r.get("admitted")]
by=defaultdict(list)
for r in adm:by[r["coin"]].append(r)
end=int(time.time()*1000)
res=[]
for coin,rs2 in sorted(by.items()):
 if B.to_binance_symbol(coin) is None:continue
 try:cd=B.fetch_candle_range(coin,"5m",min(r["ts"] for r in rs2)-200*BAR,min(end,max(r["ts"] for r in rs2)+289*BAR))
 except Exception:continue
 ts=[c.t for c in cd];hi=[c.h for c in cd];lo=[c.l for c in cd];cs=[c.c for c in cd]
 for r in rs2:
  j=bisect.bisect_left(ts,r["ts"])
  if j<200 or j+288>=len(cs):continue
  entry=cs[j];peak=entry;trough=entry;exitbar=288;reason="timeout";real=None
  # simulate each subsequent bar: update peak on high, test stop/floor on low then close
  armed=False
  for k in range(j+1,j+289):
    if hi[k]>peak:peak=hi[k]
    mfe=(peak-entry)/entry*100
    # hard stop -1.5 spot (check low)
    if (lo[k]-entry)/entry*100<=-1.5:exitbar=k-j;reason="hard_stop";real=-1.5;break
    rt=retrace_for(mfe)
    if rt is not None:
      armed=True
      floor_pct=mfe*(1-rt)
      if mfe>=2.5:floor_pct=max(floor_pct,0.5)  # breakeven lock
      floor=entry*(1+floor_pct/100)
      if lo[k]<=floor:exitbar=k-j;reason="floor";real=(floor-entry)/entry*100;break
  if real is None:real=(cs[j+exitbar]-entry)/entry*100-FEE
  mfe=(peak-entry)/entry*100
  res.append((mfe,real,reason,exitbar))
import collections
print("n",len(res))
print("reasons",collections.Counter(x[2] for x in res).most_common())
armed=[x for x in res if x[0]>=2.5]
print("armed MFE>=2.5:",len(armed),"share",round(len(armed)/len(res),3))
ptl=[x for x in armed if x[1]<=0]
print("armed that realized<=0 (profit round-trip to loss/flat):",len(ptl),"of armed",round(len(ptl)/max(1,len(armed)),3))
gb=[(x[0]-x[1])/x[0] for x in armed if x[0]>0]
print("giveback fraction med/p75/p90:",[round(sorted(gb)[int(p*len(gb))],2) for p in (.5,.75,.9)])
print("armed gave back >half MFE:",sum(1 for g in gb if g>0.5),"of",len(gb))
# realized stats overall net
real=[x[1]-FEE for x in res]
print("net realized mean/med/winrate:",round(statistics.mean(real),3),round(statistics.median(real),3),round(sum(1 for x in real if x>0)/len(real),3))
# hold bars for floor exits
fl=[x for x in res if x[2]=="floor"]
print("floor exits",len(fl),"med hold bars",statistics.median([x[3] for x in fl]) if fl else None)