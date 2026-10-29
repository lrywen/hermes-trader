#!/usr/bin/env python3
"""Per-coin streaming forward-return reconciliation for the Binance 90d replay.

Fetches each coin's 5m series ONCE in-process (retries live in the binance
fetcher), matches that coin's replay rows, then moves on -> bounded memory and
no disk-cache flush. Writes enriched JSONL + a day-block-bootstrapped summary.
Read-only; offline-only; SHADOW.
"""
import argparse, bisect, datetime, json, math, random, time
from collections import defaultdict
from hermes_trader.data import binance_candles as B

HORIZONS = {"1h": 12, "4h": 48, "24h": 288, "72h": 864}
COST19 = 0.0019
BAR = 300_000


def path_of(r):
    if r.get("admitted"):
        if r.get("fresh_impulse") and int(r.get("slow_burn_count") or 0) >= 1:
            return "A_fresh_slow"
        if r.get("composite_score", 0) >= 45:
            return "B_fresh_score"
        if r.get("dMover"):
            return "C_daily_mover"
        return "other_admit"
    if r.get("fresh_impulse") and int(r.get("slow_burn_count") or 0) >= 1:
        return "A_blocked"
    return "Z_blocked"


def summarize(pairs):
    pairs = [(v, t) for v, t in pairs if v is not None and math.isfinite(v)]
    if not pairs:
        return {"n": 0}
    byday = defaultdict(list)
    for v, t in pairs:
        byday[datetime.datetime.utcfromtimestamp(t / 1000).date().isoformat()].append(v)
    daymeans = [sum(d) / len(d) for d in byday.values()]
    mean = sum(v for v, _ in pairs) / len(pairs)
    lo = hi = None
    if len(daymeans) >= 3:
        rnd = random.Random(7)
        boots = []
        for _ in range(3000):
            sm = rnd.choices(daymeans, k=len(daymeans))
            boots.append(sum(sm) / len(sm))
        boots.sort()
        lo, hi = boots[int(.025 * len(boots))], boots[int(.975 * len(boots))]
    return {"n": len(pairs), "days": len(byday),
            "mean_pct": round(mean, 4),
            "ci95_pct": [round(lo, 4), round(hi, 4)] if lo is not None else None}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--replay", default="/data/decision_shadow_replay_bn90d.jsonl")
    ap.add_argument("--out", default="/tmp/bn90_outcomes.jsonl")
    ap.add_argument("--summary", default="/tmp/bn90_summary.json")
    ap.add_argument("--horizon", default="1h", choices=list(HORIZONS))
    ap.add_argument("--benchmark", default="BTC")
    ap.add_argument("--coins", default="", help="comma subset for testing")
    a = ap.parse_args()
    hb = HORIZONS[a.horizon]
    end = int(time.time() * 1000)

    by = defaultdict(list)
    for line in open(a.replay):
        r = json.loads(line)
        by[r["coin"]].append(r)

    bench_ts, bench_c = [], {}
    try:
        bc = B.fetch_candle_range(a.benchmark, "5m", end - 96 * 86400_000, end)
        bench_ts = sorted(c.t for c in bc)
        bench_c = {c.t: c.c for c in bc}
    except Exception:
        pass

    path_v = defaultdict(list)
    adm_v = defaultdict(list)
    tier_v = defaultdict(list)
    cov = tot = 0
    if a.coins:
        sel = {c.strip() for c in a.coins.split(",") if c.strip()}
        by = {k: v for k, v in by.items() if k in sel}
    fo = open(a.out, "w")
    for ci, (coin, rows) in enumerate(sorted(by.items())):
        if B.to_binance_symbol(coin) is None:
            continue
        rows.sort(key=lambda r: r["ts"])
        t0 = rows[0]["ts"] - BAR
        t1 = min(end, rows[-1]["ts"] + (hb + 2) * BAR)
        try:
            cd = B.fetch_candle_range(coin, "5m", t0, t1)
        except Exception as e:
            print(coin, "FETCH_ERR", type(e).__name__, flush=True)
            continue
        ts = [c.t for c in cd]
        cs = [c.c for c in cd]
        for r in rows:
            tot += 1
            j = bisect.bisect_left(ts, r["ts"])
            entry = r.get("mid_price")
            if entry is None and 0 <= j < len(ts):
                entry = cs[j]
            k = j + hb
            if entry is None or k >= len(cs):
                fo.write(json.dumps({"ts": r["ts"], "coin": coin, "covered": False}) + chr(10))
                continue
            net19 = (cs[k] - entry) / entry - COST19
            bret = None
            if bench_ts:
                bi = bisect.bisect_left(bench_ts, r["ts"])
                bk = bi + hb
                if 0 <= bi and bk < len(bench_ts):
                    b0 = bench_c[bench_ts[bi]]
                    if b0 > 0:
                        bret = (bench_c[bench_ts[bk]] - b0) / b0
            excess = (net19 - bret) if bret is not None else None
            cov += 1
            p = path_of(r)
            path_v[p].append((100 * net19, r["ts"]))
            adm_v["admitted" if r.get("admitted") else "blocked"].append((100 * net19, r["ts"]))
            tier_v[r.get("volume_tier") or "unknown"].append((100 * net19, r["ts"]))
            fo.write(json.dumps({"ts": r["ts"], "coin": coin, "covered": True,
                "admitted": r.get("admitted"), "path": p,
                "net19_pct": round(100 * net19, 4),
                "excess19_pct": (round(100 * excess, 4) if excess is not None else None)},
                ensure_ascii=False) + chr(10))
        print(ci + 1, coin, "rows", len(rows), "cov", cov, "/", tot, flush=True)
    fo.close()
    out = {"horizon": a.horizon, "total": tot, "covered": cov,
           "coverage_pct": round(100 * cov / tot, 2) if tot else 0,
           "by_path": {k: summarize(v) for k, v in sorted(path_v.items())},
           "by_admit": {k: summarize(v) for k, v in sorted(adm_v.items())},
           "by_tier": {k: summarize(v) for k, v in sorted(tier_v.items())}}
    json.dump(out, open(a.summary, "w"), indent=2, ensure_ascii=False)
    print("DONE", json.dumps({k: out[k] for k in ("total", "covered", "coverage_pct")}), flush=True)


if __name__ == "__main__":
    main()
