#!/usr/bin/env python3
"""M1 (plan G1): reconcile runner-gate decision shadow rows with PIT forward
returns, universe-wide (all evaluated candidates, admitted or blocked).

Reads runner_gate_shadow.jsonl(.*) rows (every runner-gate verdict), aligns each
to a closed 5m entry bar, fetches forward 5m closes (read-only, cached), and
computes net forward returns at TWO round-trip cost conventions plus equal-weight
market excess. Emits an enriched per-row JSONL and a summary (coverage, by path
A/B/C, by fired trigger, by volume tier) with day-block bootstrap 95% CI.

Read-only: never mutates the source shadow file and never places an order.
Idempotent: re-running overwrites the enriched output for the same input rows.
"""
from __future__ import annotations

import argparse
import bisect
import glob
import json
import math
import os
import random
import statistics as st
import time
from typing import Any, Optional

BAR_MS = 5 * 60_000
HORIZONS = {"1h": 12, "4h": 48, "24h": 288, "72h": 864}
COSTS = {"bp9.26": 0.000926, "bp19": 0.0019}
_DATA_DIR = os.environ.get("HERMES_DATA_DIR", "/data")


class CandleSource:
    """5m close series resolver: local cache first, optional network fallback."""

    def __init__(self, cache_path: str = "", use_network: bool = True):
        self.cache: dict[str, Any] = {}
        self._series_cache: dict[str, Optional[tuple[list[int], list[float]]]] = {}
        self.use_network = use_network
        if cache_path and os.path.exists(cache_path):
            try:
                self.cache = json.load(open(cache_path))
            except Exception:
                self.cache = {}

    def series(self, coin: str) -> Optional[tuple[list[int], list[float]]]:
        if coin in self._series_cache:
            return self._series_cache[coin]
        candidates = [coin]
        if coin.startswith("k") and len(coin) > 1:
            candidates.append(coin[1:])
        if ":" in coin:
            candidates.append(coin.split(":")[-1])
        rows = None
        for c in candidates:
            d = self.cache.get(c + "|5m")
            if d:
                pts = sorted((int(t), v) for t, v in d.items())
                ts = [t for t, _ in pts]
                cs = []
                for _, v in pts:
                    try:
                        cs.append(float(v[3]))
                    except Exception:
                        cs.append(math.nan)
                rows = (ts, cs)
                break
        if rows is None and self.use_network:
            rows = self._network_series(coin)
        self._series_cache[coin] = rows
        return rows

    def _network_series(self, coin: str, span_bars: int = 18000) -> Optional[tuple[list[int], list[float]]]:
        # Wide history window so rows older than the last ~3 days still resolve.
        # 18000 x 5m ~= 62 days, under the fetch guard (20000 bars/request);
        # fetch_candle_range merges its disk cache, so mature bars are not
        # re-requested. Only fully-closed bars are returned.
        try:
            from hermes_trader.data.historical_candles import fetch_candle_range
            end = int(time.time() * 1000)
            start = end - span_bars * BAR_MS
            cdls = fetch_candle_range(coin, "5m", start, end, use_cache=True)
            cdls = [c for c in cdls if c.t + BAR_MS <= end]  # closed bars only
            if not cdls:
                return None
            return ([c.t for c in cdls], [float(c.c) for c in cdls])
        except Exception:
            return None


def load_rows(pattern: str) -> list[dict[str, Any]]:
    rows: dict[tuple, dict[str, Any]] = {}
    for fp in sorted(glob.glob(pattern)):
        try:
            for line in open(fp, errors="ignore"):
                line = line.strip()
                if not line:
                    continue
                r = json.loads(line)
                rows[(r.get("ts"), r.get("coin"), r.get("side"), r.get("rule"))] = r
        except Exception:
            continue
    return list(rows.values())



def legacy_fired_names(r: dict[str, Any]) -> list[str]:
    """Reconstruct fired names from pre-M1 boolean columns when fired_names absent."""
    if r.get("fired_names"):
        return list(r["fired_names"])
    names = []
    if r.get("vol"): names.append("volumeSpike")
    if r.get("brk"): names.append("breakout")
    if r.get("burst"): names.append("momentumBurst")
    if r.get("dMover"): names.append("dailyMover")
    if r.get("up"): names.append("uptrendMomentum")
    if r.get("down"): names.append("downtrendMomentum")
    if int(r.get("slow_burn_count") or 0) >= 1: names.append("slowBurn")
    return names

def classify_path(r: dict[str, Any]) -> str:
    fresh = bool(r.get("fresh_impulse"))
    slow = int(r.get("slow_burn_count") or 0)
    score = r.get("composite_score")
    dm = bool(r.get("dMover"))
    if fresh and slow >= 1:
        return "A_fresh_slow"
    if fresh and slow == 0 and score is not None and float(score) >= 45:
        return "B_fresh_score"
    if dm:
        return "C_daily_mover"
    if not r.get("admitted"):
        return "Z_blocked"
    return "other"


def enrich(rows: list[dict[str, Any]], src: CandleSource, bench_coins: list[str]) -> list[dict[str, Any]]:
    bench_series = []
    for c in bench_coins:
        s = src.series(c)
        if s:
            bench_series.append(s)
    out = []
    for r in rows:
        coin = r.get("coin") or ""
        s = src.series(coin)
        rec = dict(r)
        rec["path"] = classify_path(r)
        rec["fired_names"] = legacy_fired_names(r)
        if not s:
            rec["covered"] = False
            out.append(rec)
            continue
        ts, cs = s
        j = bisect.bisect_right(ts, int(r["ts"]) - 1) - 1
        if j < 0 or abs(ts[j] - int(r["ts"])) > BAR_MS:
            rec["covered"] = False
            out.append(rec)
            continue
        entry = float(r["mid_price"]) if r.get("mid_price") else cs[j]
        if not (entry and math.isfinite(entry)):
            entry = cs[j]
        rec["entry_ts"] = ts[j]
        rec["entry_px"] = entry
        rets: dict[str, Optional[float]] = {}
        bench_ret: dict[str, Optional[float]] = {}
        for hz, n in HORIZONS.items():
            if j + n >= len(ts) or not math.isfinite(cs[j + n]):
                rets[hz] = None
                bench_ret[hz] = None
                continue
            gross = cs[j + n] / entry - 1.0
            rets[hz] = gross
            vals = []
            for bts, bcs in bench_series:
                bj = bisect.bisect_right(bts, ts[j] - 1) - 1
                if bj >= 0 and bj + n < len(bcs) and abs(bts[bj] - ts[j]) <= BAR_MS and bcs[bj] > 0:
                    vals.append(bcs[bj + n] / bcs[bj] - 1.0)
            bench_ret[hz] = (sum(vals) / len(vals)) if vals else None
        rec["gross_fwd"] = rets
        for label, cost in COSTS.items():
            rec[f"net_{label}"] = {
                hz: (None if rets[hz] is None else rets[hz] - cost) for hz in HORIZONS}
            rec[f"excess_{label}"] = {
                hz: (None if (rets[hz] is None or bench_ret[hz] is None)
                     else rets[hz] - bench_ret[hz] - cost) for hz in HORIZONS}
        rec["covered"] = True
        out.append(rec)
    return out


def dayblock_ci(xs: list[float], days: list[int], B: int = 4000, seed: int = 7) -> dict[str, Any]:
    if len(xs) < 2:
        return {"n": len(xs), "mean_pct": None, "ci95_pct": None}
    byday: dict[int, list[float]] = {}
    for x, d in zip(xs, days):
        byday.setdefault(d, []).append(x)
    blocks = [st.mean(v) for v in byday.values()]
    n = len(blocks)
    rnd = random.Random(seed)
    means = []
    for _ in range(B):
        acc = 0.0
        for _b in range(n):
            acc += rnd.choice(blocks)
        means.append(acc / n)
    means.sort()
    return {"n": len(xs), "days": n, "mean_pct": round(100 * st.mean(xs), 4),
            "ci95_pct": [round(100 * means[int(.025 * B)], 4),
                         round(100 * means[int(.975 * B)], 4)]}


def summarize(out: list[dict[str, Any]], hz: str = "1h", metric: str = "net_bp19") -> dict[str, Any]:
    covered = [r for r in out if r.get("covered")]
    total = len(out)

    def group(keyfn):
        buckets: dict[str, list[float]] = {}
        days: dict[str, list[int]] = {}
        for r in covered:
            v = (r.get(metric) or {}).get(hz)
            if v is None:
                continue
            k = keyfn(r)
            buckets.setdefault(k, []).append(v)
            days.setdefault(k, []).append(int(r["ts"]) // 86_400_000)
        return {k: dayblock_ci(buckets[k], days[k]) for k in sorted(buckets)}

    gap: dict[str, int] = {}
    for r in out:
        if not r.get("covered"):
            gap[r.get("volume_tier") or "unknown"] = gap.get(r.get("volume_tier") or "unknown", 0) + 1
    return {
        "horizon": hz, "metric": metric,
        "total_rows": total, "covered": len(covered),
        "coverage_pct": round(100 * len(covered) / total, 2) if total else 0.0,
        "by_path": group(lambda r: r["path"]),
        "by_admit": group(lambda r: "admitted" if r.get("admitted") else "blocked"),
        "by_tier": group(lambda r: r.get("volume_tier") or "unknown"),
        "by_trigger": group(lambda r: "|".join(r.get("fired_names") or ["none"])),
        "uncovered_by_tier": gap,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pattern", default=os.path.join(_DATA_DIR, "runner_gate_shadow.jsonl*"))
    ap.add_argument("--cache", default=os.path.join(_DATA_DIR, "ch_hist_candles.json"))
    ap.add_argument("--out", default=os.path.join(_DATA_DIR, "decision_shadow_outcomes.jsonl"))
    ap.add_argument("--summary-out", default="")
    ap.add_argument("--no-network", action="store_true")
    ap.add_argument("--max-rows", type=int, default=0)
    ap.add_argument("--horizon", default="1h", choices=list(HORIZONS))
    args = ap.parse_args()

    rows = load_rows(args.pattern)
    rows.sort(key=lambda r: r.get("ts", 0))
    if args.max_rows:
        rows = rows[-args.max_rows:]
    src = CandleSource(args.cache, use_network=not args.no_network)
    bench_coins = sorted({k[:-3] for k in getattr(src, "cache", {}) if k.endswith("|5m")})
    enriched = enrich(rows, src, bench_coins)
    out_dir = os.path.dirname(args.out)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    with open(args.out, "w") as f:
        for r in enriched:
            f.write(json.dumps(r, ensure_ascii=False) + chr(10))
    summary = summarize(enriched, hz=args.horizon)
    txt = json.dumps(summary, ensure_ascii=False, indent=2)
    print(txt)
    if args.summary_out:
        with open(args.summary_out, "w") as f:
            f.write(txt + chr(10))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
