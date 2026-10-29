import json,bisect,time,statistics,math,random,datetime,sys
from collections import defaultdict
from hermes_trader.data import binance_candles as B
from hermes_trader.indicators import math as M
sys.path.insert(0,"/tmp")
BAR=300000; COST=0.19
class O: pass
def agg(buf):
    o=O();o.t=buf[0].t;o.o=buf[0].o;o.h=max(x.h for x in buf);o.l=min(x.l for x in buf);o.c=buf[-1].c;o.v=sum(x.v for x in buf);return o
def rs(win,n):
    out=[];buf=[];cur=None
    for c in win:
        b=c.t//(n*BAR)
        if cur is None:cur=b
        if b!=cur: out.append(agg(buf));buf=[];cur=b
        buf.append(c)
    if buf:out.append(agg(buf))
    return out
rows=[json.loads(l) for l in open("/data/decision_shadow_replay_bn90d.jsonl")]
adm=[r for r in rows if r.get("admitted")]
by=defaultdict(list)
for r in adm: by[r["coin"]].append(r)
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
        atr4=float(M.atr(c4,14)[-1]); adx4=float(M.adx(c4,14)[-1])
        e8=float(M.ema(cl4,8)[-1]); e21=float(M.ema(cl4,21)[-1]); px=cs[j]
        reach=atr4/px*100
        trend4=e8>e21 and adx4>=25
        c1=rs(win,12); trend1=False
        if len(c1)>=30:
            cl1=[b.c for b in c1]; trend1=float(M.ema(cl1,8)[-1])>float(M.ema(cl1,21)[-1])
        f1=(cs[j+12]-px)/px*100-COST; f4=(cs[j+48]-px)/px*100-COST; f24=(cs[j+288]-px)/px*100-COST
        mover=bool(r.get("dMover")) and not r.get("fresh_impulse")
        recs.append(dict(coin=coin,reach=reach,trend4=trend4,trend1=trend1,mover=mover,
                         f1=f1,f4=f4,f24=f24,ts=r["ts"]))
print("recs",len(recs),flush=True)
def ci(v):
    by2=defaultdict(list)
    for x,t in v: by2[datetime.datetime.utcfromtimestamp(t/1000).date()].append(x)
    dm=[sum(d)/len(d) for d in by2.values()]
    if len(dm)<3:return None
    rnd=random.Random(5);b=[]
    for _ in range(2000):
        s=rnd.choices(dm,k=len(dm));b.append(sum(s)/len(s))
    b.sort();return (round(b[int(.025*len(b))],3),round(b[int(.975*len(b))],3),len(by2))
def stat(sub,key):
    if not sub:return (0,None,None)
    return (len(sub),round(statistics.mean(x[key] for x in sub),3),ci([(x[key],x["ts"]) for x in sub]))
base=recs; r1=[x for x in recs if not x["mover"]]
print("BASE all admits",stat(base,"f1"),stat(base,"f24"),flush=True)
print("R1 no-mover  ",stat(r1,"f1"),stat(r1,"f24"),flush=True)
for mult in [0.5,1.0,1.5]:
    sub=[x for x in r1 if x["reach"]>=mult*0.38]
    print(f"R2 reach>={mult}*2cost",stat(sub,"f1"),stat(sub,"f4"),stat(sub,"f24"),flush=True)
sub=[x for x in r1 if x["trend4"] and x["trend1"]]
print("R4 R1+trend4+trend1",stat(sub,"f1"),stat(sub,"f4"),stat(sub,"f24"),flush=True)
sub=[x for x in r1 if x["trend4"]]
print("R4 trend4 only",stat(sub,"f1"),stat(sub,"f24"),flush=True)
sub=[x for x in r1 if x["trend4"] and x["trend1"] and x["reach"]>=0.38]
print("R1+R2+R4",stat(sub,"f1"),stat(sub,"f4"),stat(sub,"f24"),flush=True)
# distribution of reach
rs3=sorted(x["reach"] for x in r1)
print("reach pctiles p10/p50/p90:",[round(rs3[int(q*len(rs3))],3) for q in (.1,.5,.9)],"share reach>=0.38:",round(sum(1 for x in r1 if x["reach"]>=0.38)/len(r1),2),flush=True)
