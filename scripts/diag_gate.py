#!/usr/bin/env python3
"""Gate + entry-price evidence: late-entry gate block rate and entry extension
of trades that actually filled. Read-only."""
import json
from collections import defaultdict, Counter

TA = "/data/ta_late_entry_shadow.jsonl"
BOOK = "/data/.shadow-book.json"


def load_jsonl(p):
    out = []
    for line in open(p):
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except Exception:
                pass
    return out


def main():
    recs = load_jsonl(TA)
    print(f"=== ta_late_entry gate records: {len(recs)} ===")
    modes = Counter(r.get("mode") for r in recs)
    print("mode:", dict(modes))
    blocked = [r for r in recs if r.get("blocked")]
    print(f"blocked={len(blocked)} ({len(blocked)/len(recs)*100:.1f}%)")
    # reasons for blocks
    br = Counter((r.get("reason") or "")[:70] for r in blocked)
    print("--- block reasons (top) ---")
    for reason, c in br.most_common(12):
        print(f"  {c:4d}  {reason}")

    # extension distribution for long signals that were NOT blocked (admitted)
    def num(x):
        try:
            return float(x)
        except Exception:
            return None
    adm_long = [r for r in recs if not r.get("blocked") and r.get("side") == "long"]
    adm_short = [r for r in recs if not r.get("blocked") and r.get("side") == "short"]
    ext_long = [num(r.get("extension")) for r in adm_long]
    ext_long = [x for x in ext_long if x is not None]
    rsi_long = [num(r.get("rsi4h")) for r in adm_long]
    rsi_long = [x for x in rsi_long if x is not None]
    ext_short = [num(r.get("extension")) for r in adm_short]
    ext_short = [x for x in ext_short if x is not None]
    import statistics
    print(f"\n=== ADMITTED long signals: {len(adm_long)} | short: {len(adm_short)} ===")
    if ext_long:
        ext_long.sort()
        print(f"long extension(>0 = price ABOVE 20-high, extended up): "
              f"median={statistics.median(ext_long):.3f} p90={ext_long[int(len(ext_long)*0.9)]:.3f} "
              f"max={max(ext_long):.3f}  frac>0={sum(1 for x in ext_long if x>0)/len(ext_long)*100:.0f}%")
    if rsi_long:
        rsi_long.sort()
        print(f"long rsi4h: median={statistics.median(rsi_long):.1f} p90={rsi_long[int(len(rsi_long)*0.9)]:.1f} "
              f"frac>70={sum(1 for x in rsi_long if x>70)/len(rsi_long)*100:.0f}%")
    if ext_short:
        print(f"short extension: median={statistics.median(ext_short):.3f} (neg = below 20-low)")

    # blocked vs admitted extension — is the gate even separating?
    blk_ext = sorted(num(r.get("extension")) for r in blocked if num(r.get("extension")) is not None)
    if blk_ext:
        print(f"\nblocked extension: median={statistics.median(blk_ext):.3f} min={blk_ext[0]:.3f} max={blk_ext[-1]:.3f}")

    # outcome-tagged records (if the loop tags winners/losers)
    outs = Counter(r.get("outcome") for r in recs if r.get("outcome"))
    print(f"\noutcome-tagged: {dict(outs)}")

    # ---- join with actual fills: what was extension at entries that LOST ----
    book = json.load(open(BOOK))
    bypos = defaultdict(list)
    for f in book["fills"]:
        bypos[f["position_id"]].append(f)
    opens = []
    for pid, fs in bypos.items():
        o = [x for x in fs if x["type"] == "open"]
        c = [x for x in fs if x["type"] == "close"]
        if o and c:
            opens.append((o[0], c[0]))
    # index gate recs by (coin, side) nearest ts
    print(f"\n=== ENTRY EXTENSION OF 50 ACTUAL TRADES (join by coin/side/px) ===")
    rec_by_cs = defaultdict(list)
    for r in recs:
        if r.get("coin") and r.get("side"):
            rec_by_cs[(r["coin"], r["side"])].append(r)
    matched = 0
    lose_ext, win_ext = [], []
    for o, c in opens:
        cands = rec_by_cs.get((o["coin"], o["side"]), [])
        # match by entry px proximity
        best = None
        for r in cands:
            rp = num(r.get("entry_px"))
            if rp is None:
                continue
            if abs(rp - o["price"]) / o["price"] < 0.02:
                best = r
                break
        if best:
            matched += 1
            ext = num(best.get("extension"))
            rsi = num(best.get("rsi4h"))
            pnl = c["realized_pnl_usd"]
            tgt = lose_ext if pnl <= 0 else win_ext
            if ext is not None:
                tgt.append(ext)
            print(f"  {o['coin']:9s} {o['side']:5s} pnl=${pnl:7.3f} ext={ext if ext is None else round(ext,3)} "
                  f"rsi4h={None if rsi is None else round(rsi,1)} blocked={best.get('blocked')} "
                  f"trend={best.get('trend_direction')}")
    print(f"\nmatched {matched}/{len(opens)} trades to gate records")
    if lose_ext and win_ext:
        print(f"LOSERS entry extension: median={statistics.median(lose_ext):.3f} mean={statistics.mean(lose_ext):.3f}")
        print(f"WINNERS entry extension: median={statistics.median(win_ext):.3f} mean={statistics.mean(win_ext):.3f}")


if __name__ == "__main__":
    main()
