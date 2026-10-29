import json,bisect,time,statistics,random,datetime
from collections import defaultdict
from hermes_trader.data import binance_candles as B
from hermes_trader.indicators import math as M
BAR=300000
class O: pass
def agg(buf):
    o=O();o.t=buf[0].t;o.o=buf[0].o;o.h=max(x.h for x in buf);o.l=min(x.l for x in buf);o.c=buf[-1].c;o.v=sum(x.v for x in buf);return o
def resample(cd,n):
    out=[];buf=[];cur=None
    for c in cd:
        b=c.t//(n*BAR)
        if cur is None:cur=b
        if b!=cur:out.append(agg(buf));buf=[];cur=b
        buf.append(c)
    if buf:out.append(agg(buf))
    return out
by=defaultdict(list)
for line in open("/data/decision_shadow_replay_bn90d.jsonl"):
    r=json.loads(line);by[r["coin"]].append(r)
end=int(time.time()*1000)
# benchmark BTC full grid once
print("fetching benchmark...",flush=True)
bcd=B.fetch_candle_range("BTC","5m",end-92*86400000,end)
bt_ts=[c.t for c in bcd];bt_c=[c.c for c in bcd]
def btc_ret(t,hb):
    i=bisect.bisect_left(bt_ts,t);k=i+hb
    if i<0 or k>=len(bt_c) or i>=len(bt_c):return None
    b0=bt_c[i]
    return (bt_c[k]-b0)/b0*100 if b0>0 else None
# buckets: (label selector) -> lists of (excess9,excess19,gross,ts) at horizons 1h/4h/24h
H={"1h":12,"4h":48,"24h":288}
res={h:defaultdict(list) for h in H}
ncoin=0
for coin,rs2 in sorted(by.items()):
    if B.to_binance_symbol(coin) is None: continue
    rs2.sort(key=lambda r:r["ts"])
    try: cd=B.fetch_candle_range(coin,"5m",rs2[0]["ts"]-900*BAR,min(end,rs2[-1]["ts"]+289*BAR))
    except Exception: continue
    ts=[c.t for c in cd];cs=[c.c for c in cd]
    c4=resample(cd,48)
    if len(c4)<30: continue
    t4=[b.t for b in c4];cl4=[b.c for b in c4]
    ATR=M.atr(c4,14);ADX=M.adx(c4,14);E8=M.ema(cl4,8);E21=M.ema(cl4,21)
    RSI=[None]*(14)+[M.rsi_last(cl4[:k+1],14) for k in range(14,len(cl4))]
    ncoin+=1
    for r in rs2:
        j=bisect.bisect_left(ts,r["ts"])
        if j<320 or j+288>=len(cs):continue
        q=bisect.bisect_right(t4,r["ts"]-48*BAR)-1
        if q<21:continue
        atr4=float(ATR[q]);adx4=float(ADX[q]);rsi4=float(RSI[q]) if RSI[q] is not None else float("nan")
        e8=float(E8[q]);e21=float(E21[q]);px=cs[j];ext=(px-e21)/atr4
        cont=bool(r.get("admitted"))
        pull=(e8>e21 and adx4>=20 and ext<=0.3 and 30<=rsi4<=50)
        dip=(rsi4==rsi4 and rsi4<35)
        for hb,h in [(12,"1h"),(48,"4h"),(288,"24h")]:
            if j+hb>=len(cs):continue
            g=(cs[j+hb]-px)/px*100; br=btc_ret(r["ts"],hb)
            if br is None:continue
            for lab,sel in (("cont",cont),("pull",pull),("dip",dip)):
                if sel:res[h][lab].append((g-9.26/100-br,g-0.19-br,g,r["ts"]))
    print(ncoin,coin,flush=True)
def ci(vals):
    d2=defaultdict(list)
    for x,t in vals:d2[datetime.datetime.utcfromtimestamp(t/1000).date()].append(x)
    dm=[sum(d)/len(d) for d in d2.values()]
    if len(dm)<3:return (None,len(d2))
    rnd=random.Random(11);b=[]
    for _ in range(3000):
        s=rnd.choices(dm,k=len(dm));b.append(sum(s)/len(s))
    b.sort();return([round(b[int(.025*len(b))],3),round(b[int(.975*len(b))],3)],len(d2))
out={"coins":ncoin}
for h in H:
    out[h]={}
    for lab in ("cont","pull","dip"):
        v=res[h][lab]
        if not v:continue
        ex9=[(a,t) for a,b2,c,t in v];ex19=[(b2,t) for a,b2,c,t in v];gr=[(c,t) for a,b2,c,t in v]
        out[h][lab]={"n":len(v),
          "excess_maker9_mean":round(statistics.mean(a for a,_,_,_ in v),3),"excess9_ci":ci(ex9),
          "excess_taker19_mean":round(statistics.mean(b2 for _,b2,_,_ in v),3),"excess19_ci":ci(ex19),
          "gross_mean":round(statistics.mean(c for _,_,c,_ in v),3)}
json.dump(out,open("/data/m2_r5_excess.json","w"),indent=2)
print(json.dumps(out,indent=2))
