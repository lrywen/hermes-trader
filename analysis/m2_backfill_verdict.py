import json,bisect,time,statistics,random,datetime
from collections import defaultdict
from hermes_trader.data import binance_candles as B
from hermes_trader.indicators import math as M
BAR=300000;COST=0.19
class O:
 def __init__(s,b):s.t=b[0];s.o=b[1];s.h=b[2];s.l=b[3];s.c=b[4];s.v=b[5]
def resample(cd,n):
 out=[];buf=[];cur=None
 for c in cd:
  b=c.t//(n*BAR)
  if cur is None:cur=b
  if b!=cur:out.append(O((buf[0].t,buf[0].o,max(x.h for x in buf),min(x.l for x in buf),buf[-1].c,sum(x.v for x in buf))));buf=[];cur=b
  buf.append(c)
 if buf:out.append(O((buf[0].t,buf[0].o,max(x.h for x in buf),min(x.l for x in buf),buf[-1].c,sum(x.v for x in buf))))
 return out
rows=[json.loads(l) for l in open("/data/decision_shadow_replay_bn90d.jsonl")]
adm=[r for r in rows if r.get("admitted")]
by=defaultdict(list)
for r in adm:by[r["coin"]].append(r)
end=int(time.time()*1000)
recs=[]
for coin,rs2 in sorted(by.items()):
 if B.to_binance_symbol(coin) is None:continue
 try:cd=B.fetch_candle_range(coin,"5m",min(r["ts"] for r in rs2)-300*BAR,min(end,max(r["ts"] for r in rs2)+289*BAR))
 except Exception:continue
 ts=[c.t for c in cd];cs=[c.c for c in cd];c4=resample(cd,48)
 if len(c4)<30:continue
 t4=[b.t for b in c4];cl4=[b.c for b in c4];ATR=M.atr(c4,14);E21=M.ema(cl4,21)
 for r in rs2:
  j=bisect.bisect_left(ts,r["ts"])
  if j<300 or j+288>=len(cs):continue
  q=bisect.bisect_right(t4,r["ts"]-48*BAR)-1
  if q<21:continue
  px=cs[j];ext=(px-float(E21[q]))/float(ATR[q])
  f1=(cs[j+12]-px)/px*100-COST;f24=(cs[j+288]-px)/px*100-COST
  r1=bool(r.get("dMover")) and not r.get("fresh_impulse")
  r3=ext>=3.0
  recs.append(dict(coin=coin,ts=r["ts"],f1=f1,f24=f24,r1=r1,r3=r3))
def ci(v):
 d=defaultdict(list)
 for x,t in v:d[datetime.datetime.utcfromtimestamp(t/1000).date()].append(x)
 dm=[sum(z)/len(z) for z in d.values()]
 if len(dm)<3:return None
 rnd=random.Random(7);b=[]
 for _ in range(3000):
  s=rnd.choices(dm,k=len(dm));b.append(sum(s)/len(s))
 b.sort();return[round(b[int(.025*len(b))],3),round(b[int(.975*len(b))],3)]
def stat(sub,key):
 if not sub:return None
 return {"n":len(sub),"mean":round(statistics.mean(x[key] for x in sub),3),"ci":ci([(x[key],x["ts"]) for x in sub])}
out={}
for arm,pred in (("R1_mover",lambda x:x["r1"]),("R3_overext",lambda x:x["r3"])):
 blocked=[x for x in recs if pred(x)]
 kept=[x for x in recs if not pred(x)]
 out[arm]={"blocked":stat(blocked,"f1"),"blocked24":stat(blocked,"f24"),"kept":stat(kept,"f1"),"kept24":stat(kept,"f24"),
   "blocked_share":round(len(blocked)/len(recs),3)}
out["total_admits"]=len(recs)
print(json.dumps(out,indent=1))
json.dump(out,open("/data/m2_backfill_verdict.json","w"),indent=1)