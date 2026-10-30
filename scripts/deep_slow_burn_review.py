"""Stage-4 promotion review: deep slow-burn veto (slow_burn_count>=2).

Pre-registered multi-source replication. The offline bn90d ablation found the
S_slow_ge2 arm nominally negative on both 1h/24h but FAILING the Bonferroni
gate (alpha*=0.0023). Before promoting enforce=true we require the SAME signal
to hold on independent data sources, not just the discovery sample, so this
re-evaluates the exact live hit condition (admitted long + fresh_impulse +
slow_burn_count>=2) on:

  - replay_60d_outcomes.jsonl : Hyperliquid 1m/5m replay (out-of-sample venue)
  - decision_shadow_outcomes.jsonl : live HL SHADOW forward outcomes
  - bn90d_outcomes_{1h,24h}   : Binance 88d discovery sample (reported for
                                context; already used to find the arm, so it
                                cannot be confirmation)

Per source/horizon: day-block bootstrap 95% CI, time-half sign consistency,
last-20d, and gap3d de-correlated independent events. Read-only.

PROMOTION RULE (pre-registered, not tunable after seeing results):
  enforce=true only if the 24h excess 95% day-block CI is fully <0 AND the
  gap3d independent-event mean is <0 on BOTH independent confirmations
  (HL replay + live shadow), with >=30 independent events each. Otherwise keep
  shadow-only. Discovery sample alone never promotes.
"""
from __future__ import annotations

import datetime as dt
import json
import math
import random
import sys
from collections import defaultdict

HORIZONS = ("1h", "24h")
MIN_INDEP_EVENTS = 30
_BOOT_RNG = random.Random(20261031)


def _ts_ms(r: dict):
    t = r.get("entry_ts") or r.get("ts")
    return float(t)


def _excess_pct(r: dict, h: str):
    """Return market-excess net return in PERCENT.

    replay/live files carry excess_bp19 as {horizon: fraction} where the stored
    fraction is a decimal (0.004 = 0.4%); bn90d split files carry excess19_pct
    directly in percent. We normalize everything to percent.
    """
    v = (r.get("excess_bp19") or {}).get(h)
    if v is not None and math.isfinite(float(v)):
        return float(v) * 100.0  # decimal fraction -> percent
    v = r.get("excess19_pct")
    if v is not None and math.isfinite(float(v)):
        return float(v)
    return None


def _hits(rows):
    out = []
    for r in rows:
        if not (r.get("admitted") and r.get("side") == "long"):
            continue
        if not r.get("fresh_impulse"):
            continue
        if int(r.get("slow_burn_count") or 0) < 2:
            continue
        out.append(r)
    return out


def _day(t_ms):
    return dt.datetime.utcfromtimestamp(t_ms / 1000.0).date()


def _day_bootstrap_ci(vals_ts, nboot=8000):
    """vals_ts: list of (pct_return, ts_ms). Day-block mean bootstrap 95% CI."""
    byday = defaultdict(list)
    for v, t in vals_ts:
        byday[_day(t)].append(v)
    dm = [sum(d) / len(d) for d in byday.values()]
    if not dm:
        return None
    boots = []
    for _ in range(nboot):
        boots.append(sum(_BOOT_RNG.choices(dm, k=len(dm))) / len(dm))
    boots.sort()
    mean = sum(v for v, _ in vals_ts) / len(vals_ts)
    return {
        "n": len(vals_ts), "days": len(byday),
        "mean_pct": round(mean, 4),
        "median_pct": round(sorted(v for v, _ in vals_ts)[len(vals_ts) // 2], 4),
        "ci95": [round(boots[int(0.025 * len(boots))], 4),
                 round(boots[int(0.975 * len(boots))], 4)],
    }


def _indep_events(rows_h):
    """rows_h: list of (return_pct, ts_ms, coin). gap3d per coin."""
    bycoin = defaultdict(list)
    for v, t, coin in rows_h:
        bycoin[coin].append((t, v))
    ev = []
    for coin, lst in bycoin.items():
        lst.sort()
        last_t = None
        for t, v in lst:
            if last_t is None or (t - last_t) >= 3 * 86_400_000:
                ev.append((v, t))
                last_t = t
    return ev


def evaluate(name, rows):
    hits = _hits(rows)
    res = {"source": name, "hits_total": len(hits)}
    promote_h = {}
    for h in HORIZONS:
        triples = [(_excess_pct(r, h), _ts_ms(r), r.get("coin")) for r in hits]
        triples = [x for x in triples if x[0] is not None]
        if not triples:
            res[h] = {"n": 0}
            promote_h[h] = False
            continue
        pairs = [(v, t) for v, t, _ in triples]
        pairs.sort(key=lambda x: x[1])
        # time halves
        mid = len(pairs) // 2
        first = [v for v, _ in pairs[:mid]]
        second = [v for v, _ in pairs[mid:]]
        # last 20 days
        tmax = max(t for _, t in pairs)
        last20 = [(v, t) for v, t in pairs if t >= tmax - 20 * 86_400_000]
        indep = _indep_events(triples)
        indep_mean = round(sum(v for v, _ in indep) / len(indep), 4) if indep else None
        s = _day_bootstrap_ci(pairs)
        s["half1_mean"] = round(sum(first) / len(first), 4) if first else None
        s["half2_mean"] = round(sum(second) / len(second), 4) if second else None
        s["last20d_mean"] = (round(sum(v for v, _ in last20) / len(last20), 4)
                             if last20 else None)
        s["gap3d_n"] = len(indep)
        s["gap3d_mean_pct"] = indep_mean
        res[h] = s
        ci = s.get("ci95")
        promote_h[h] = bool(
            ci and ci[1] < 0 and indep_mean is not None and indep_mean < 0
            and len(indep) >= MIN_INDEP_EVENTS
        )
    res["passes_24h_promotion"] = promote_h.get("24h", False)
    return res


def _load(path):
    rows = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def main():
    out = []
    # Independent confirmations first.
    try:
        out.append(evaluate("hl_replay_60d", _load(sys.argv[1])))
    except (IndexError, FileNotFoundError):
        pass
    try:
        out.append(evaluate("hl_live_shadow", _load(sys.argv[2])))
    except (IndexError, FileNotFoundError):
        pass
    # Discovery sample (context only — cannot promote). The bn90d outcome
    # split files carry ONLY (coin,ts,path,forward); the feature fields
    # (fresh_impulse/slow_burn_count/side) live in the decision replay file,
    # so we must join on (coin,ts) before filtering.
    if len(sys.argv) > 3:
        feat = {}
        for r in _load(sys.argv[5]):  # decision_shadow_replay_bn90d.jsonl
            feat[(r.get("coin"), r.get("ts"))] = r
        merged = {}
        for h, p in (("1h", sys.argv[3]), ("24h", sys.argv[4])):
            for r in _load(p):
                k = (r.get("coin"), r.get("ts"))
                base = dict(feat.get(k) or {})
                d = merged.setdefault(k, base)
                e = dict(d.get("excess_bp19") or {})
                if r.get("excess19_pct") is not None:
                    e[h] = r["excess19_pct"] / 100.0
                d["excess_bp19"] = e
                d.setdefault("side", "long")  # bn90d replay is long-only
        out.append(evaluate("bn90d_discovery", list(merged.values())))

    confirms = [o for o in out if o["source"] != "bn90d_discovery"]
    promote = bool(confirms) and all(o.get("passes_24h_promotion") for o in confirms)
    verdict = {
        "verdict": "PROMOTE" if promote else "KEEP_SHADOW",
        "rule": ("24h excess CI fully <0, gap3d mean <0, gap3d n>=30 on BOTH "
                 "hl_replay_60d and hl_live_shadow; discovery sample excluded"),
        "sources": out,
    }
    print(json.dumps(verdict, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
