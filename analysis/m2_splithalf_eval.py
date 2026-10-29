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
tss=sorted(r["ts"] for r in adm); mid=tss[len(tss)//2]
by=defaultdict(list)
for r in adm:by[r["coin"]].append(r)
seg=defaultdict(list)
end=int(time.time()*1000)
for coin,rs2 in sorted(by.items()):
 if B.to_binance_symbol(coin) is None:continue
 try:cd=B.fetch_candle_range(coin,"5m",min(r["ts"] for r in rs2)-200*BAR,min(end,max(r["ts"] for r in rs2)+289*BAR))
 except Exception:continue
 ts=[c.t for c in cd];cs=[c.c for c in cd]
 for r in rs2:
  j=bisect.bisect_left(ts,r["ts"])
  if j<200 or j+288>=len(cs):continue
  f1=(cs[j+12]-cs[j])/cs[j]*100-COST;f24=(cs[j+288]-cs[j])/cs[j]*100-COST
  mover=bool(r.get("dMover")) and not r.get("fresh_impulse")
  half="H1" if r["ts"]<mid else "H2"
  seg[(half,"all")].append((f1,f24,r["ts"]))
  if not mover:seg[(half,"R1")].append((f1,f24,r["ts"]))
  else:seg[(half,"mover")].append((f1,f24,r["ts"]))
def ci(v):
 d=defaultdict(list)
 for x,t in v:d[datetime.datetime.utcfromtimestamp(t/1000).date()].append(x)
 dm=[sum(z)/len(z) for z in d.values()]
 if len(dm)<3:return None
 rnd=random.Random(7);b=[]
 for _ in range(2000):
  s=rnd.choices(dm,k=len(dm));b.append(sum(s)/len(s))
 b.sort();return[round(b[int(.025*len(b))],3),round(b[int(.975*len(b))],3)]
out={}
for half in("H1","H2"):
 for grp in("all","R1","mover"):
  v=seg[(half,grp)]
  if not v:continue
  i1=[(a,t) for a,b2,t in v];i24=[(b2,t) for a,b2,t in v]
  out[f"{half}_{grp}"]={"n":len(v),"f1":round(statistics.mean(a for a,_,_ in v),3),"f1_ci":ci(i1),"f24":round(statistics.mean(b2 for _,b2,_ in v),3),"f24_ci":ci(i24)}
print(json.dumps(out,indent=1))