import json,bisect,time,statistics
from collections import defaultdict
from hermes_trader.data import binance_candles as B
from hermes_trader.indicators import math as M
BAR=300000
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
 try:cd=B.fetch_candle_range(coin,"5m",min(r["ts"] for r in rs2)-400*BAR,min(end,max(r["ts"] for r in rs2)+289*BAR))
 except Exception:continue
 ts=[c.t for c in cd];cs=[c.c for c in cd];c4=resample(cd,48)
 if len(c4)<30:continue
 t4=[b.t for b in c4];cl4=[b.c for b in c4];ATR=M.atr(c4,14);E21=M.ema(cl4,21)
 RSI=[None]*14+[M.rsi_last(cl4[:k+1],14) for k in range(14,len(cl4))]
 for r in rs2:
  j=bisect.bisect_left(ts,r["ts"])
  if j<300 or j+288>=len(cs):continue
  q=bisect.bisect_right(t4,r["ts"]-48*BAR)-1
  if q<21:continue
  px=cs[j]
  run12=(px-cs[j-12])/cs[j-12]*100      # 1h pre-entry run-up
  run48=(px-cs[j-48])/cs[j-48]*100      # 4h pre-entry run-up
  ext=(px-float(E21[q]))/float(ATR[q])  # extension vs EMA21 in ATR (5m px)
  rsi=RSI[q]
  f24=(cs[j+288]-px)/px*100-0.19
  recs.append((run12,run48,ext,rsi,f24))
import bisect as _b
def q(v,p):
 v=sorted(x for x in v if x is not None);return v[min(len(v)-1,int(p*len(v)))]
run12=[x[0] for x in recs];run48=[x[1] for x in recs];ext=[x[2] for x in recs];rsi=[x[3] for x in recs if x[3] is not None]
print("n",len(recs))
print("pre-run 1h  p10/med/p90:",[round(q(run12,p),2) for p in (.1,.5,.9)])
print("pre-run 4h  p10/med/p90:",[round(q(run48,p),2) for p in (.1,.5,.9)])
print("ext ATR(5mpx) p10/med/p90:",[round(q(ext,p),2) for p in (.1,.5,.9)])
print("rsi4h p10/med/p90:",[round(q(rsi,p),1) for p in (.1,.5,.9)])
# forward 24h by run-up quintile (top vs bottom)
rs=sorted(recs,key=lambda x:x[1]);n=len(rs)
for i,(lab,seg) in enumerate((("low_run",rs[:n//4]),("mid",rs[n//4:3*n//4]),("high_run(top25%)",rs[3*n//4:]))):
    print(lab,"n",len(seg),"fwd24 mean",round(statistics.mean(x[4] for x in seg),3),"med",round(statistics.median(x[4] for x in seg),3))
# extended ext>=3 vs rest
hi=[x for x in recs if x[2]>=3];lo=[x for x in recs if x[2]<3]
print("ext>=3 n",len(hi),"fwd24",round(statistics.mean(x[4] for x in hi),3),"| ext<3",round(statistics.mean(x[4] for x in lo),3))