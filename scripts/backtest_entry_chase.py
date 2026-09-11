#!/usr/bin/env python3
"""Read-only: replay real filled trades' 4h entry indicators to locate
residual buy-high. For each filled trade, rebuild 4h RSI(14), extension in
ATR(14) from EMA21, and ADX(14) AT THE ENTRY BAR, then join realized PnL.
Tests whether tighter chase filters would have cut losers without harming
winners. In-domain hl_client; no orders/writes.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from hermes_trader.client.hl_client import _http_post
from hermes_trader.indicators.math import adx, atr, ema, rsi

MEM_FILE = os.environ.get("HERMES_MEMORY_FILE", "/data/.agent-memory.json")
if not Path(MEM_FILE).exists():
    raise SystemExit(
        f"memory file not found: {MEM_FILE} (set HERMES_MEMORY_FILE to override)")
mem = json.load(open(MEM_FILE))
trades = mem["trades"]; closes = mem["closes"]

# coin -> open timestamps
entry_ts = {}
for t in trades:
    et = t.get("executed_at") or t.get("event_ts")
    if et: entry_ts.setdefault(t["coin"], []).append(int(et))
for v in entry_ts.values(): v.sort()

# realized pnl by (coin, entry nearest-before close)
def pnl_for(coin, ts):
    best=None
    for c in closes:
        if c["coin"]!=coin: continue
        if c["closed_at"]>=ts-7200_000:
            if best is None or c["closed_at"]<best["closed_at"]: best=c
    return float(best["realized_pnl_pct"]) if best else None

class C:
    __slots__=("h","l","c")
    def __init__(s,h,l,c): s.h,s.l,s.c=h,l,c

def indicators(coin, ts):
    # 4h candles: 120 before + a few after entry
    start=ts-130*4*3600_000; end=ts+3*4*3600_000
    raw=_http_post("/info",{"type":"candleSnapshot","req":{"coin":coin,
        "interval":"4h","startTime":start,"endTime":end}})
    if not isinstance(raw,list) or len(raw)<40: return None
    rows=sorted(raw,key=lambda x:int(x["t"]))
    # last CLOSED bar at/before ts
    cs=[C(float(x["h"]),float(x["l"]),float(x["c"])) for x in rows]
    closes_=[x.c for x in cs]
    le=[i for i,x in enumerate(rows) if int(x["t"])<=ts]
    if not le: return None
    idx=le[-1]
    e21=ema(closes_,21); r=rsi(cs,14); a=adx(cs,14); at=atr(cs,14)
    ext=(closes_[idx]-e21[idx])/at[idx] if at[idx] else float("nan")
    return {"rsi":r[idx],"ext":ext,"adx":a[idx],"close":closes_[idx]}

rows=[]
for coin, tss in entry_ts.items():
    for ts in tss:
        pnl=pnl_for(coin,ts)
        if pnl is None: continue
        try: ind=indicators(coin,ts)
        except Exception as e: ind=None
        if ind is None:
            print("no ind",coin); continue
        rows.append((coin,pnl,ind)); time.sleep(0.05)

rows.sort(key=lambda x:x[1])
print("\ncoin      pnl%     RSI4h  ext(xATR) ADX4h  relax?(ADX>=35)")
for coin,pnl,d in rows:
    relax="RELAX" if d["adx"]>=35 else ""
    print(f"{coin:9} {pnl:+7.2f}  {d['rsi']:5.1f}  {d['ext']:6.2f}   {d['adx']:5.1f}  {relax}")

def test(name, pred):
    kept=[r for r in rows if not pred(r[2])]   # pred=True => would block
    blocked=[r for r in rows if pred(r[2])]
    if not blocked:
        print(f"{name:34} blocks 0"); return
    bl_lose=sum(1 for r in blocked if r[1]<=0); bl_win=sum(1 for r in blocked if r[1]>0)
    kept_pnl=sum(r[1] for r in kept); all_pnl=sum(r[1] for r in rows)
    print(f"{name:34} blocks={len(blocked):2d} (亏{bl_lose}/盈{bl_win}) "
          f"拦下亏损{sum(r[1] for r in blocked if r[1]<=0):+6.2f} 误杀盈利{sum(r[1] for r in blocked if r[1]>0):+6.2f} "
          f"保留总pnl {kept_pnl:+.1f} vs 原始 {all_pnl:+.1f}")

print()
test("ext>=2.5 一律拦(取消relax)", lambda d: d["ext"]>=2.5)
test("ext>=3.0", lambda d: d["ext"]>=3.0)
test("RSI>=75 一律拦(取消relax)", lambda d: d["rsi"]>=75)
test("RSI>=70", lambda d: d["rsi"]>=70)
test("ADX>=35且ext>=3.0(收紧relax)", lambda d: d["adx"]>=35 and d["ext"]>=3.0)
test("ext>=2.5 且 RSI>=70 双确认", lambda d: d["ext"]>=2.5 and d["rsi"]>=70)
