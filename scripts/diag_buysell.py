#!/usr/bin/env python3
"""Offline diagnostic: why shadow trades 'buy high, sell low'.

Reads the production shadow book (.shadow-book.json) from /data and the
entry audit (ta_late_entry_shadow.jsonl) to attribute losses with evidence.
Read-only; writes nothing.
"""
import json
import statistics
from collections import defaultdict, Counter

BOOK = "/data/.shadow-book.json"
TA = "/data/ta_late_entry_shadow.jsonl"


def load_book(path=BOOK):
    d = json.load(open(path))
    bypos = defaultdict(list)
    for f in d["fills"]:
        bypos[f["position_id"]].append(f)
    rt = []
    for pid, fs in bypos.items():
        fs.sort(key=lambda x: x["ts"])
        o = [x for x in fs if x["type"] == "open"]
        c = [x for x in fs if x["type"] == "close"]
        if o and c:
            rt.append((o[0], c[0]))
    return d, rt


def main():
    d, rt = load_book()
    n = len(rt)
    wins = [(o, c) for o, c in rt if c["realized_pnl_usd"] > 0]
    loss = [(o, c) for o, c in rt if c["realized_pnl_usd"] <= 0]
    print(f"=== OVERVIEW ===")
    print(f"closed trades: {n}  wallet ${d.get('starting_balance')} -> ${d.get('wallet_balance'):.2f}")
    print(f"wins={len(wins)} loss={len(loss)} winrate={len(wins)/n*100:.0f}%")
    tot = sum(c["realized_pnl_usd"] for _, c in rt)
    gw = sum(c["realized_pnl_usd"] for _, c in wins)
    gl = -sum(c["realized_pnl_usd"] for _, c in loss)
    print(f"total realized=${tot:.3f}  grossWin=${gw:.3f} grossLoss=${gl:.3f} PF={gw/max(gl,1e-9):.2f}")
    aw = gw/max(len(wins),1); al = gl/max(len(loss),1)
    print(f"avgWin=${aw:.3f} avgLoss=${al:.3f}  win/loss size={aw/max(al,1e-9):.2f}")

    # side
    lp = [(o,c) for o,c in rt if o["side"]=="long"]
    sp = [(o,c) for o,c in rt if o["side"]=="short"]
    lpnl = sum(c["realized_pnl_usd"] for _,c in lp)
    spnl = sum(c["realized_pnl_usd"] for _,c in sp)
    print(f"\n=== SIDE ===\nlongs={len(lp)} pnl=${lpnl:.3f}  shorts={len(sp)} pnl=${spnl:.3f}")

    # hold time
    hm = [c["hold_minutes"] for _,c in rt]
    print(f"\n=== HOLD (min) === median={statistics.median(hm):.1f} mean={statistics.mean(hm):.1f} min={min(hm):.1f} max={max(hm):.1f}")

    # exit reason grouping
    print(f"\n=== EXIT REASON (grouped) ===")
    def grp(r):
        rl = r.lower()
        if "max_loss" in rl or "stop" in rl or "sl " in rl: return "stop_loss/max_loss"
        if "take_profit" in rl or "tp " in rl or "target" in rl: return "take_profit"
        if "trail" in rl: return "trailing"
        if "liq" in rl or "liquidat" in rl: return "liquidation"
        if "timeout" in rl or "time" in rl or "eod" in rl: return "time_exit"
        if "roe_halt" in rl or "halt" in rl: return "roe_halt"
        if "flip" in rl or "reverse" in rl or "signal" in rl: return "signal_exit"
        return "other:"+r.split("(")[0].strip()[:30]
    rc = Counter()
    rpnl = defaultdict(float)
    rhold = defaultdict(list)
    for o,c in rt:
        g = grp(c.get("reason",""))
        rc[g]+=1; rpnl[g]+=c["realized_pnl_usd"]; rhold[g].append(c["hold_minutes"])
    for g,cnt in rc.most_common():
        print(f"  {g:24s} n={cnt:3d} pnl=${rpnl[g]:7.3f}  medHold={statistics.median(rhold[g]):6.1f}m")

    # MFE: how far did losers go in our favor before reversing? (buy-high/sell-low signature)
    print(f"\n=== MFE (max favorable excursion, ROE%) ===")
    mfe_l = [c.get("mfe_pct",0) for _,c in loss]
    mfe_w = [c.get("mfe_pct",0) for _,c in wins]
    print(f"  losers: median={statistics.median(mfe_l):.2f}% max={max(mfe_l):.2f}%  (>1%% favorable then reversed: {sum(1 for x in mfe_l if x>1)}/{len(mfe_l)})")
    print(f"  winners: median={statistics.median(mfe_w):.2f}%")

    # spot move at close vs held time: quick stop-outs
    quick = [(o,c) for o,c in rt if c["hold_minutes"] < 15 and c["realized_pnl_usd"]<=0]
    print(f"\n=== QUICK STOP-OUTS (<15min, losers): {len(quick)} ===")
    for o,c in quick[:12]:
        print(f"  {o['coin']:10s} {o['side']:5s} entry={o['price']:.6g} exit={c['price']:.6g} "
              f"spot={c.get('spot_pct',0):.2f}% roe={c.get('realized_pnl_pct',0):.1f}% "
              f"hold={c['hold_minutes']:.1f}m mfe={c.get('mfe_pct',0):.2f}% | {c.get('reason','')[:60]}")

    # worst 8 trades
    print(f"\n=== WORST 8 ===")
    for o,c in sorted(rt,key=lambda t:t[1]["realized_pnl_usd"])[:8]:
        print(f"  {o['coin']:10s} {o['side']:5s} pnl=${c['realized_pnl_usd']:7.3f} roe={c.get('realized_pnl_pct',0):6.1f}% "
              f"hold={c['hold_minutes']:6.1f}m mfe={c.get('mfe_pct',0):.2f} regime={c.get('entry_regime','')[:8]:8s} | {c.get('reason','')[:55]}")
    # best 8
    print(f"\n=== BEST 8 ===")
    for o,c in sorted(rt,key=lambda t:-t[1]["realized_pnl_usd"])[:8]:
        print(f"  {o['coin']:10s} {o['side']:5s} pnl=${c['realized_pnl_usd']:7.3f} roe={c.get('realized_pnl_pct',0):6.1f}% "
              f"hold={c['hold_minutes']:6.1f}m mfe={c.get('mfe_pct',0):.2f} regime={c.get('entry_regime','')[:8]:8s} | {c.get('reason','')[:55]}")


if __name__ == "__main__":
    main()
