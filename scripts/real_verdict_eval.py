"""Real-verdict bridge for M2 record-only shadow arms (C6).

Reads the production risk-tuning shadow audit stream, selects would-block rows
for the M2 entry-veto rules, reconstructs the forward return the blocked trade
WOULD have earned (net of taker cost) from candles using recorded entry_px, and
emits a promotion-gate-compatible real_verdict block per rule. A veto is
real-confirmed only if the trades it blocked were net-negative (block avoided a
loss). Insufficient samples -> fail closed. Read-only; never changes admission.
"""
from __future__ import annotations
import argparse, bisect, json, os, statistics, time
from datetime import datetime, timezone

RULES = ("mover_bypass_veto", "overextension_veto")
HORIZONS = {"1h": 12, "4h": 48, "24h": 288}
COST_PCT = 0.19
MIN_REAL_N = 50
_DEFAULT_SHADOW = os.path.join(os.path.sep, "data", "risk_tuning_shadow.jsonl")

def _parse_ts(s):
    dt = datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)

def load_would_blocks(path, rules):
    out = {r: [] for r in rules}
    if not os.path.exists(path):
        return out
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("would") != "block":
                continue
            if rec.get("rule") in out:
                out[rec["rule"]].append(rec)
    return out

def forward_returns(recs, horizon_bars):
    from hermes_trader.data import binance_candles as B
    by_coin = {}
    for r in recs:
        by_coin.setdefault(r.get("coin", ""), []).append(r)
    rets = []
    now_ms = int(time.time() * 1000)
    for coin, rs in by_coin.items():
        if B.to_binance_symbol(coin) is None:
            continue
        ts0 = min(_parse_ts(r["timestamp"]) for r in rs)
        try:
            cd = B.fetch_candle_range(coin, "5m", ts0 - 1000,
                                      min(now_ms, ts0 + (horizon_bars + 5) * 300000))
        except Exception:
            continue
        cts = [c.t for c in cd]; cs = [c.c for c in cd]
        for r in rs:
            entry = (r.get("detail") or {}).get("entry_px")
            if not entry:
                continue
            t = _parse_ts(r["timestamp"])
            j = bisect.bisect_left(cts, t)
            if j >= len(cs) or j + horizon_bars >= len(cs):
                continue
            fwd = (cs[j + horizon_bars] - float(entry)) / float(entry) * 100.0
            rets.append(fwd - COST_PCT)
    return rets

def verdict_for(rule, recs, horizon_bars):
    n_raw = len(recs)
    rets = forward_returns(recs, horizon_bars)
    n = len(rets)
    mean = round(statistics.mean(rets), 4) if rets else None
    agree = n >= MIN_REAL_N and mean is not None and mean < 0.0
    same_direction = n >= MIN_REAL_N and sum(1 for x in rets if x < 0) > n / 2
    return {"rule": rule, "horizon_bars": horizon_bars, "would_block_records": n_raw,
            "real_verdict": {"n": n, "mean_pct": mean, "agree": bool(agree),
                             "same_direction": bool(same_direction),
                             "sufficient": n >= MIN_REAL_N}}

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--shadow", default=_DEFAULT_SHADOW)
    ap.add_argument("--horizon", choices=sorted(HORIZONS), default="1h")
    ap.add_argument("--out", default="")
    args = ap.parse_args()
    blocks = load_would_blocks(args.shadow, RULES)
    hb = HORIZONS[args.horizon]
    result = {"generated_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
              "horizon": args.horizon, "cost_pct": COST_PCT, "min_real_n": MIN_REAL_N,
              "arms": [verdict_for(r, blocks[r], hb) for r in RULES]}
    text = json.dumps(result, indent=2)
    if args.out:
        with open(args.out, "w") as fh:
            fh.write(text + "\n")
    print(text)
    return 0

if __name__ == "__main__":
    raise SystemExit(main())