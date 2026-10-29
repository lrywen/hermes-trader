import json,bisect,time,statistics,math,random,datetime
from collections import defaultdict
from hermes_trader.data import binance_candles as B
from hermes_trader.indicators import math as M
BAR=300000; COST=0.19
class O: pass
def agg(buf):
    o=O();o.t=buf[0].t;o.o=buf[0].o;o.h=max(x.h for x in buf);o.l=min(x.l for x in buf);o.c=buf[-1].c;o.v=sum(x.v for x in buf);return o
def rs(win,n):
    out=[];buf=[];cur=None
    for c in win:
        b=c.t//(n*BAR)
        if cur is None:cur=b
        if b!=cur:out.append(agg(buf));buf=[];cur=b
        buf.append(c)
    if buf:out.append(agg(buf))
    return out
# Universe = ALL decision rows (not just admits): we want to compare ENTRY TYPES
# at the same 1h-sampled grid. Construct pullback vs continuation signals on ALL coins.
rows=json.load(open("/dev/stdin")) if False else None
by=defaultdict(list)
for line in open("/data/decision_shadow_replay_bn90d.jsonl"):
    r=json.loads(line); by[r["coin"]].append(r)
recs=[]; end=int(time.time()*1000)
for coin,rs2 in sorted(by.items()):
    if B.to_binance_symbol(coin) is None: continue
    rs2.sort(key=lambda r:r["ts"])
    try: cd=B.fetch_candle_range(coin,"5m",rs2[0]["ts"]-900*BAR,min(end,rs2[-1]["ts"]+289*BAR))
    except Exception: continue
    ts=[c.t for c in cd]; cs=[c.c for c in cd]
    for r in rs2:
        j=bisect.bisect_left(ts,r["ts"])
        if j<320 or j+288>=len(cs): continue
        win=cd[:j+1]; c4=rs(win,48)
        if len(c4)<20: continue
        cl4=[b.c for b in c4]
        atr4=float(M.atr(c4,14)[-1]); e8=float(M.ema(cl4,8)[-1]); e21=float(M.ema(cl4,21)[-1]); px=cs[j]
        rsi4=float(M.rsi(cl4,14)[-1]); adx4=float(M.adx(c4,14)[-1])
        ext=(px-e21)/atr4
        f1=(cs[j+12]-px)/px*100-COST; f24=(cs[j+288]-px)/px*100-COST
        # continuation: current LIVE admits (for reference)
        cont=bool(r.get("admitted"))
        # mean-reversion pullback LONG in uptrend: e8>e21 (uptrend), price pulled
        # back near/below EMA21 (ext <= 0.3) but not crashing (rsi 35-50), ADX>=20
        pullback_up = (e8>e21 and adx4>=20 and ext<=0.3 and 30<=rsi4<=50)
        # simple dip-buy: rsi4 <35 regardless trend
        dip = rsi4<35
        recs.append((pullback_up,dip,cont,f1,f24,r["ts"]))
def ci(v):
    b2=defaultdict(list)
    for x,t in v:b2[datetime.datetime.utcfromtimestamp(t/1000).date()].append(x)
    dm=[sum(d)/len(d) for d in b2.values()]
    if len(dm)<3:return None
    rnd=random.Random(9);b=[]
    for _ in range(2000):
        s=rnd.choices(dm,k=len(dm));b.append(sum(s)/len(s))
    b.sort();return (round(b[int(.025*len(b))],3),round(b[int(.975*len(b))],3),len(b2))
def rep(name,sel,idx):
    v=[(x[idx],x[5]) for x in recs if sel(x)]
    if not v:print(name,"n=0");return
    print(name,"n=",len(v),"mean",round(statistics.mean(a for a,_ in v),3),"CI",ci(v))
rep("continuation(actual admits)",lambda x:x[2],3)
rep("R5 pullback-long (uptrend+dip to EMA21,rsi30-50)",lambda x:x[0],3)
rep("R5 pullback-long f24",lambda x:x[0],4)
rep("dip-buy rsi4<35 f1",lambda x:x[1],3)
rep("dip-buy rsi4<35 f24",lambda x:x[1],4)
print("total grid",len(recs))
