#!/usr/bin/env python3
"""E2b: Drill into the 'with-crowd LONG_CROWDED' veto and other post-fresh gates."""
from __future__ import annotations

import json
import os
from collections import Counter

SESSION_LOG = os.environ.get("SESSION_LOG_PATH", "/data/session-log.jsonl")
MIN_SCORE = 30.0


def to_str(v):
    if isinstance(v, list):
        return "; ".join(str(x) for x in v)
    return str(v) if v is not None else ""


def main():
    latest_scan = {}
    rows = []
    with open(SESSION_LOG) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            et = ev.get("event")
            ts = ev.get("ts") or ev.get("timestamp")
            if et == "scan" and ts:
                for cs in ev.get("coin_scores", []) or []:
                    coin = cs.get("coin")
                    if coin:
                        latest_scan[coin] = {
                            "score": float(cs.get("score", 0) or 0),
                            "triggers": set(cs.get("triggers", []) or [])}
            elif et == "execute" and ts and ev.get("side") == "long" and coin:
                coin = ev.get("coin")
                if coin not in latest_scan:
                    continue
                sc = latest_scan[coin]
                trg = sc["triggers"]
                fresh = (("breakout" in trg)
                         or (("volumeSpike" in trg) and ("momentumBurst" in trg))
                         or (("momentumBurst" in trg) and sc["score"] >= MIN_SCORE))
                rows.append({
                    "coin": coin, "executed": bool(ev.get("executed")),
                    "blocked_by": to_str(ev.get("blocked_by")),
                    "detail": to_str(ev.get("detail")),
                    "score": sc["score"], "triggers": trg, "fresh": fresh,
                    "breakout": "breakout" in trg,
                    "volume": "volumeSpike" in trg,
                    "burst": "momentumBurst" in trg,
                    "funding": ev.get("funding_regime"),
                })

    fresh = [r for r in rows if r["fresh"] and not r["executed"]]
    print(f"fresh=TRUE & not executed: {len(fresh)}\n")

    # detail distribution for empty blocked_by
    print("=== empty-blocked detail distribution (fresh=TRUE, not exec) ===")
    empty = [r for r in fresh if not r["blocked_by"]]
    print(f"count with empty blocked_by: {len(empty)}")
    dc = Counter()
    for r in empty:
        d = r["detail"] or "(blank)"
        # collapse
        if "cooldown" in d.lower():
            d = "cooldown"
        elif "margin" in d.lower() or "leverage" in d.lower():
            d = "margin/leverage"
        elif "risk" in d.lower():
            d = "risk limits"
        dc[d] += 1
    for k, v in dc.most_common(15):
        print(f"  {v:4d}  {k[:90]}")
    print()

    # crowded analysis
    print("=== with-crowd LONG_CROWDED veto (fresh=TRUE) ===")
    crowd = [r for r in fresh if "CROWDED" in r["blocked_by"].upper()]
    print(f"count: {len(crowd)}")
    print(f"  with breakout: {sum(1 for r in crowd if r['breakout'])}")
    print(f"  with volume+burst: {sum(1 for r in crowd if r['volume'] and r['burst'])}")
    sc = [r["score"] for r in crowd]
    if sc:
        sc.sort()
        print(f"  score: min={sc[0]:.0f} med={sc[len(sc)//2]:.0f} max={sc[-1]:.0f}")
    print("  funding_regime distribution:")
    for k, v in Counter(r["funding"] for r in crowd).most_common():
        print(f"    {v:4d}  {k}")
    print("  by coin:")
    for k, v in Counter(r["coin"] for r in crowd).most_common(15):
        print(f"    {v:4d}  {k}")
    print()

    # momentumBurst-only without volume/breakout (13 fresh=FALSE)
    print("=== momentumBurst-only (no vol, no breakout) — currently fresh=FALSE ===")
    bo = [r for r in rows if r["burst"] and not r["volume"] and not r["breakout"]
          and not r["executed"]]
    print(f"count: {len(bo)}")
    sc2 = [r["score"] for r in bo]
    if sc2:
        sc2.sort()
        print(f"  score: min={sc2[0]:.0f} p25={sc2[len(sc2)//4]:.0f} "
              f"med={sc2[len(sc2)//2]:.0f} max={sc2[-1]:.0f}")
    print("  would qualify if burst alone = fresh (no score gate):")
    print(f"    {len(bo)} added; with score>=30: {sum(1 for r in bo if r['score']>=30)}")
    print()

    # volume-only signals (no breakout, no burst) - currently fresh=FALSE
    print("=== volumeSpike-only (no breakout/burst) — currently fresh=FALSE ===")
    vo = [r for r in rows if r["volume"] and not r["breakout"] and not r["burst"]]
    print(f"count: {len(vo)}; score med={sorted(r['score'] for r in vo)[len(vo)//2] if vo else 0:.0f}")


if __name__ == "__main__":
    main()
