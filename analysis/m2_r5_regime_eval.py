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
bcd=B.fetch_candle_range("BTC","5m",end-92*86400000,end)
bt_ts=[c.t for c in bcd];bt_c=[c.c for c in bcd]
def btc_ret(t,hb):
    i=bisect.bisect_left(bt_ts,t);k=i+hb
    if k>=len(bt_c):return None
    return (bt_c[k]-bt_c[i])/bt_c[i]*100
groups=defaultdict(list)
for coin,rs2 in sorted(by.items()):
    if B.to_binance_symbol(coin) is None:continue
    rs2.sort(key=lambda r:r["ts"])
    try: cd=B.fetch_candle_range(coin,"5m",rs2[0]["ts"]-900*BAR,min(end,rs2[-1]["ts"]+289*BAR))
    except:continue
    ts=[c.t for c in cd];cs=[c.c for c in cd];c4=resample(cd,48)
    if len(c4)<30:continue
    t4=[b.t for b in c4];cl4=[b.c for b in c4]
    ATR=M.atr(c4,14);ADX=M.adx(c4,14);E8=M.ema(cl4,8);E21=M.ema(cl4,21)
    RSI=[None]*14+[M.rsi_last(cl4[:k+1],14) for k in range(14,len(cl4))]
    for r in rs2:
        j=bisect.bisect_left(ts,r["ts"])
        if j<320 or j+288>=len(cs):continue
        q=bisect.bisect_right(t4,r["ts"]-48*BAR)-1
        if q<21:continue
        rsi4=RSI[q]
        if rsi4 is None or rsi4!=rsi4:continue
        up=float(E8[q])>float(E21[q]);adx=float(ADX[q])
        px=cs[j];g24=(cs[j+288]-px)/px*100;br=btc_ret(r["ts"],288)
        if br is None:continue
        ex9=g24-0.0926-br
        if rsi4<35:
            groups["dip_any"].append((ex9,r["ts"]))
            if up: groups["dip_uptrend"].append((ex9,r["ts"]))
            if up and adx>=25: groups["dip_up_adx25"].append((ex9,r["ts"]))
            if not up: groups["dip_downtrend"].append((ex9,r["ts"]))
def ci(v):
    d=defaultdict(list)
    for x,t in v:d[datetime.datetime.utcfromtimestamp(t/1000).date()].append(x)
    dm=[sum(x)/len(x) for x in d.values()]
    if len(dm)<3:return None
    rnd=random.Random(3);b=[]
    for _ in range(3000):
        s=rnd.choices(dm,k=len(dm));b.append(sum(s)/len(s))
    b.sort();return [round(b[int(.025*len(b))],3),round(b[int(.975*len(b))],3)]
for k,v in groups.items():
    print(k,"n=",len(v),"excess9_24h mean",round(statistics.mean(x for x,_ in v),3),"CI",ci(v))
