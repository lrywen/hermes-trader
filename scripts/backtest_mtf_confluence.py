#!/usr/bin/env python3
"""Offline (read-only): multi-timeframe confluence edge, LONG history paged.

Pages back up to ~MAX_BARS 1h candles per coin (to span BOTH bull and bear
regimes — a short recent window is a single uptrend and produces look-ahead
100%-WR artifacts). Classifies a fast regime (EMA20/30+slope, production) and
slow regime (EMA50/200+same slope), buckets into confluence quadrants, and
measures sign-aligned forward returns. The key comparison is the marginal
effect of requiring slow-trend agreement vs acting on the fast signal alone.

No orders/writes. In-domain hl_client.
"""
from __future__ import annotations

import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from hermes_trader.client.hl_client import _http_post
from hermes_trader.indicators.math import ema

COINS = ["BTC", "ETH", "SOL", "BNB", "DOGE", "AVAX", "LINK"]
MAX_BARS = 12000
PAGE = 4800
SLOPE_BARS, SLOPE_UP = 8, 0.002
FWD = (12, 24, 48, 96, 168)
WARM = 250


def fetch_long(coin):
    end = int(time.time()*1000); out = {}
    for _ in range(6):
        start = end - PAGE*3600_000
        raw = _http_post("/info", {"type": "candleSnapshot", "req": {
            "coin": coin, "interval": "1h", "startTime": start, "endTime": end}})
        if not isinstance(raw, list) or not raw:
            break
        before = len(out)
        for c in raw:
            out[int(c["t"])] = float(c["c"])
        earliest = int(raw[0]["t"])
        end = earliest - 3600_000   # strict paging, no overlap
        # stop when a page adds almost nothing (hit exchange history floor)
        if len(out) - before < 100 or len(out) >= MAX_BARS:
            break
        time.sleep(0.05)
    ts = sorted(out)
    if ts and time.time()*1000 < ts[-1]+3600_000:
        ts = ts[:-1]
    return [out[t] for t in ts]


def trend(cl, fp, sp):
    f = ema(cl, fp); s = ema(cl, sp); n = len(cl); o = ["neutral"]*n
    for i in range(n):
        if i < sp+SLOPE_BARS: continue
        sl = (f[i]-f[i-SLOPE_BARS])/abs(f[i-SLOPE_BARS])
        if f[i] > s[i] and sl > SLOPE_UP: o[i] = "up"
        elif f[i] < s[i] and sl < -SLOPE_UP: o[i] = "down"
    return o


def main():
    # q -> H -> [ret]; also raw fast-only baseline
    agg = {}
    ncoins = 0
    for coin in COINS:
        try:
            cl = fetch_long(coin)
        except Exception as e:
            print("fetch fail", coin, e); continue
        if len(cl) < WARM+200:
            print(f"skip {coin}: {len(cl)} bars"); continue
        ncoins += 1
        fast = trend(cl, 20, 30); slow = trend(cl, 50, 200)
        n = len(cl)
        for i in range(WARM, n):
            fd, sd = fast[i], slow[i]
            if fd not in ("up", "down"): continue
            for H in FWD:
                if i+H >= n: continue
                r = (cl[i+H]-cl[i])/cl[i]*100
                r = r if fd == "up" else -r
                # fast-only baseline
                agg.setdefault("FAST_only", {}).setdefault(H, []).append(r)
                if fd == "up" and sd == "up": q = "confluence_up"
                elif fd == "down" and sd == "down": q = "confluence_dn"
                elif (fd == "up" and sd == "down") or (fd == "down" and sd == "up"): q = "divergence"
                else: q = "slow_neutral"
                agg.setdefault(q, {}).setdefault(H, []).append(r)
        print(f"{coin}: {n} bars")

    print(f"\ncoins={ncoins}  returns sign-aligned to FAST direction (%)")
    hdr = "%-15s %6s" % ("bucket", "n") + "".join(f"  {h}h:WR/m/med" for h in FWD)
    print(hdr); print("-"*100)
    def line(label, q):
        cells=[]; nt=0
        for H in FWD:
            v=agg.get(q,{}).get(H,[])
            nt=max(nt,len(v))
            if v:
                wr=100*sum(1 for x in v if x>0)/len(v)
                cells.append(f" {wr:4.0f}/{statistics.mean(v):+5.2f}/{statistics.median(v):+5.2f}")
            else: cells.append("      -      ")
        print("%-15s %6d%s"%(label,nt,"".join(cells)))
    line("FAST 全部(基准)", "FAST_only")
    line("共振-做多", "confluence_up")
    line("共振-做空", "confluence_dn")
    # merge confluence both dirs
    qc={}
    for H in FWD:
        v=agg.get("confluence_up",{}).get(H,[])+agg.get("confluence_dn",{}).get(H,[])
        qc[H]=v
    agg["confluence_ALL"]=qc
    line("共振-多空合计", "confluence_ALL")
    line("背离(逆长周期)", "divergence")
    line("长周期中性", "slow_neutral")

if __name__ == "__main__":
    main()
