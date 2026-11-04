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
import sys
import time
from typing import Any, Optional

BAR_MS = 5 * 60_000
HORIZONS = {"1h": 12, "4h": 48, "24h": 288, "72h": 864}
COSTS = {"bp9.26": 0.000926, "bp19": 0.0019}
_DATA_DIR = os.environ.get("HERMES_DATA_DIR", "/data")

# S0-3 (2026-11-04): tier-matched equal-weight benchmark. The old benchmark
# compared a micro candidate against an equal-weight mean of ALL cached coins
# (dominated by mega/mid), which let BCH-72h show gross<0 / excess>0 (the
# broad market fell harder than the coin) — a beta artifact, not alpha. A
# candidate is now compared ONLY against benchmark coins in the SAME static
# 24h-USD-volume tier; if that tier's bench set is empty, excess is None
# (benchmark_status=no_same_tier), never silently pooled. Rows without a
# volume are no_tier and get no excess at all (no fabricated tier). BTC is
# kept separately as a market_factor side reference, never mixed into the
# tier excess. Thresholds are pre-registered here and the resolved tier map is
# written to disk (auditable, not recomputed per timestamp).
TIER_MEGA_MIN_USD = 200_000_000.0
TIER_MID_MIN_USD = 5_000_000.0
MARKET_FACTOR_COIN = "BTC"
# S0-5: the production runner composite gate clears at 54 (runner_entry_gate),
# but classify_path historically bucketed fresh/score rows at 45, so the
# by_path label mis-stated which rows actually cleared the real scan gate.
# classify_path now labels the path at the TRUE 54 gate and keeps the legacy
# 45 verdict in a separate field for new-vs-old comparability.
SCAN_GATE_COMPOSITE = 54.0
LEGACY_PATH_GATE_COMPOSITE = 45.0


def map_bench_tier(volume_24h_usd: Optional[float]) -> Optional[str]:
    """Static 24h-USD-volume tier: mega >= 200M; 5M <= mid < 200M; micro < 5M.

    None (missing/non-numeric volume) -> None so the caller can refuse to
    benchmark instead of guessing a tier.
    """
    if volume_24h_usd is None:
        return None
    try:
        v = float(volume_24h_usd)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(v) or v < 0:
        return None
    if v >= TIER_MEGA_MIN_USD:
        return "mega"
    if v >= TIER_MID_MIN_USD:
        return "mid"
    return "micro"


class CandleSource:
    """5m close series resolver: local cache first, optional network fallback."""

    def __init__(self, cache_path: str = "", use_network: bool = True,
                 source: str = "hyperliquid"):
        self.source = source
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
        if rows is None and self.source == "binance":
            rows = self._binance_series(coin)
        elif rows is None and self.use_network:
            rows = self._network_series(coin)
        self._series_cache[coin] = rows
        return rows

    def _binance_series(self, coin: str):
        try:
            from hermes_trader.data import binance_candles as B
            if B.to_binance_symbol(coin) is None:
                return None
            cdls = B.fetch_candle_range(coin, "5m",
                                        self._bn_start, self._bn_end,
                                        use_cache=True)
            cdls = [c for c in cdls if c.t + BAR_MS <= self._bn_end]
            if not cdls:
                return None
            return ([c.t for c in cdls], [float(c.c) for c in cdls])
        except Exception:
            return None

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
            fh = open(fp, errors="ignore")
        except Exception:
            continue
        with fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                # One corrupt line (rotation-race interleaving) must only cost
                # itself: the previous per-file try aborted the whole file, so a
                # single bad row at runner_gate_shadow.jsonl.1:84 on 2026-11-02
                # silently discarded the 75 rows after it. load_rows then saw
                # 2,386 vs 2,461 physical lines, the truncation guard refused to
                # publish, and decision_shadow_outcomes.jsonl froze.
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                rows[(r.get("ts"), r.get("coin"), r.get("side"), r.get("rule"))] = r
    return list(rows.values())


def count_source_lines(pattern: str) -> int:
    """Count non-blank, parseable source lines across the glob.

    Used as the truncation baseline instead of ``len(load_rows())``: a
    rotation race (a file mid-rename / partially written at cron time) makes
    load_rows read a short snapshot, so a guard compared only against its own
    de-duplicated count would see src==out and miss the loss. The physical
    line count of whatever the glob currently resolves is the floor the
    output must not materially fall below.
    """
    n = 0
    for fp in sorted(glob.glob(pattern)):
        try:
            for line in open(fp, errors="ignore"):
                line = line.strip()
                if not line:
                    continue
                try:
                    json.loads(line)
                except Exception:
                    continue
                n += 1
        except Exception:
            continue
    return n



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

def classify_path(r: dict[str, Any],
                  gate: float = SCAN_GATE_COMPOSITE) -> str:
    fresh = bool(r.get("fresh_impulse"))
    slow = int(r.get("slow_burn_count") or 0)
    score = r.get("composite_score")
    dm = bool(r.get("dMover"))
    if fresh and slow >= 1:
        return "A_fresh_slow"
    if fresh and slow == 0 and score is not None and float(score) >= gate:
        return "B_fresh_score"
    if dm:
        return "C_daily_mover"
    if not r.get("admitted"):
        return "Z_blocked"
    return "other"


def legacy_path_45(r: dict[str, Any]) -> str:
    """Pre-S0 path label at the old 45 cutoff (comparison / audit only)."""
    return classify_path(r, gate=LEGACY_PATH_GATE_COMPOSITE)


def enrich(rows: list[dict[str, Any]], src: CandleSource,
           bench_by_tier: dict[str, list],
           market_factor: Optional[tuple[list[int], list[float]]] = None
           ) -> list[dict[str, Any]]:
    """Attach forward returns + tier-matched excess.

    S0-3: bench_by_tier maps tier -> list of (ts, closes) benchmark series.
    A row's excess uses ONLY the benchmark coins in its own static tier.
    market_factor (BTC) is recorded side-by-side as market_factor_{label},
    never folded into the tier excess. Rows with no volume (bench_tier None)
    get excess=None + benchmark_status='no_tier'; rows whose tier bench set
    is empty/unresolved at the entry bar get 'no_same_tier'.
    """
    out = []
    for r in rows:
        coin = r.get("coin") or ""
        s = src.series(coin)
        rec = dict(r)
        rec["path"] = classify_path(r)
        rec["path_legacy_45"] = legacy_path_45(r)
        rec["fired_names"] = legacy_fired_names(r)
        # S0-3 static tier mapping (auditable column on every row).
        rec["bench_tier"] = map_bench_tier(r.get("volume_24h_usd"))
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
        # P0-3 (2026-11-03): gross must be direction-aware. A short profits when
        # price FALLS, but the prior code always used long-direction gross
        # (fwd/entry - 1), so the 1,594 source short rows (819 covered) had every
        # return/excess sign inverted — an up-move (a short loss) was booked as a
        # gain. sgn flips both leg and benchmark for shorts; excess then equals
        # sgn*(leg - bench), the correct market-neutral short return. Cost is
        # paid once per round trip regardless of side, so it is NOT doubled and
        # stays outside the sign flip.
        sgn = -1.0 if str(r.get("side")).lower() == "short" else 1.0
        # S0-3: choose the SAME-tier benchmark set once per row. A row with no
        # volume has no tier -> benchmark_status='no_tier' and excess stays None;
        # a tier whose bench set is empty/unresolved at this bar -> 'no_same_tier'.
        row_tier = rec["bench_tier"]
        tier_series = bench_by_tier.get(row_tier, []) if row_tier else []
        rets: dict[str, Optional[float]] = {}
        bench_ret: dict[str, Optional[float]] = {}
        mf_ret: dict[str, Optional[float]] = {}
        for hz, n in HORIZONS.items():
            if j + n >= len(ts) or not math.isfinite(cs[j + n]):
                rets[hz] = None
                bench_ret[hz] = None
                mf_ret[hz] = None
                continue
            gross = sgn * (cs[j + n] / entry - 1.0)
            rets[hz] = gross
            vals = []
            for bcoin, (bts, bcs) in tier_series:
                if bcoin == coin:
                    continue  # never benchmark a coin against itself
                bj = bisect.bisect_right(bts, ts[j] - 1) - 1
                if bj >= 0 and bj + n < len(bcs) and abs(bts[bj] - ts[j]) <= BAR_MS and bcs[bj] > 0:
                    vals.append(sgn * (bcs[bj + n] / bcs[bj] - 1.0))
            bench_ret[hz] = (sum(vals) / len(vals)) if vals else None
            if market_factor:
                mts, mcs = market_factor
                mj = bisect.bisect_right(mts, ts[j] - 1) - 1
                if mj >= 0 and mj + n < len(mcs) and abs(mts[mj] - ts[j]) <= BAR_MS and mcs[mj] > 0:
                    mf_ret[hz] = sgn * (mcs[mj + n] / mcs[mj] - 1.0)
                else:
                    mf_ret[hz] = None
            else:
                mf_ret[hz] = None
        # S0-3 machine-readable benchmark status.
        if row_tier is None:
            rec["benchmark_status"] = "no_tier"
        elif any(v is not None for v in bench_ret.values()):
            rec["benchmark_status"] = "same_tier"
        else:
            rec["benchmark_status"] = "no_same_tier"
        rec["gross_fwd"] = rets
        for label, cost in COSTS.items():
            rec[f"net_{label}"] = {
                hz: (None if rets[hz] is None else rets[hz] - cost) for hz in HORIZONS}
            # S0-3: excess is tier-matched and a SECONDARY benchmark; None unless
            # a same-tier benchmark exists at that horizon.
            rec[f"excess_{label}"] = {
                hz: (None if (rets[hz] is None or bench_ret[hz] is None)
                     else rets[hz] - bench_ret[hz] - cost) for hz in HORIZONS}
            # BTC side reference, never folded into excess.
            rec[f"market_factor_{label}"] = {
                hz: (None if (rets[hz] is None or mf_ret[hz] is None)
                     else rets[hz] - mf_ret[hz] - cost) for hz in HORIZONS}
        rec["covered"] = True
        out.append(rec)
    return out


# S0-1 (2026-11-04, H4): horizons >= 24h span multiple UTC days. The old
# day-block bootstrap resampled single-day means, but a 24h/72h forward return
# overlaps 1-3 day blocks, so adjacent "day blocks" are not independent and the
# CI came out too narrow. Worse, the point estimate was the ROW mean while the
# CI was over DAY means — two different estimators. block_ci groups rows into
# non-overlapping calendar blocks whose length is >= the horizon (72h -> 3-day
# blocks, 24h -> 1-day blocks) and makes the point estimate the mean OF THE
# SAME blocks the CI is bootstrapped from (single estimator). <=4h keeps 1-day
# blocks (a 1h/4h return never spans a UTC day boundary materially).
def horizon_block_days(hz: str) -> int:
    """Minimum calendar block length (in UTC days) for a horizon."""
    n_bars = HORIZONS[hz]
    horizon_days = n_bars * BAR_MS / 86_400_000
    return max(1, math.ceil(horizon_days))


def _build_calendar_blocks(xs: list[float], days: list[int],
                           block_days: int) -> list[float]:
    """Group rows into non-overlapping calendar blocks of >= block_days days.

    Days are bucketed by floor(utc_day / block_days), so blocks are fixed
    calendar windows (not a sliding window) — each row lands in exactly one
    block, preserving the non-overlapping resampling unit.
    """
    byblock: dict[int, list[float]] = {}
    for x, d in zip(xs, days):
        byblock.setdefault(d // block_days, []).append(x)
    return [st.mean(v) for v in byblock.values()]


def block_ci(xs: list[float], days: list[int], hz: str = "1h",
             B: int = 4000, seed: int = 7) -> dict[str, Any]:
    """Block bootstrap with block length >= horizon; point estimate == block mean."""
    if len(xs) < 2:
        return {"n": len(xs), "blocks": 0, "block_days": horizon_block_days(hz),
                "mean_pct": None, "ci95_pct": None}
    block_days = horizon_block_days(hz)
    blocks = _build_calendar_blocks(xs, days, block_days)
    if len(blocks) < 2:
        # Too few independent blocks for a bootstrap CI: report the block mean
        # but an honest null interval (single block => no dispersion estimate).
        m = round(100 * blocks[0], 4)
        return {"n": len(xs), "blocks": len(blocks), "block_days": block_days,
                "mean_pct": m, "ci95_pct": [m, m], "degenerate": True}
    n = len(blocks)
    rnd = random.Random(seed)
    means = []
    for _ in range(B):
        acc = 0.0
        for _b in range(n):
            acc += rnd.choice(blocks)
        means.append(acc / n)
    means.sort()
    # Point estimate is the mean of the SAME calendar blocks used for the CI
    # (H4 fix: point and interval now share one estimator).
    return {"n": len(xs), "blocks": n, "block_days": block_days,
            "mean_pct": round(100 * st.mean(blocks), 4),
            "ci95_pct": [round(100 * means[int(.025 * B)], 4),
                         round(100 * means[int(.975 * B)], 4)]}


def dayblock_ci(xs: list[float], days: list[int], B: int = 4000, seed: int = 7,
                hz: str = "1h") -> dict[str, Any]:
    """Backwards-compatible wrapper: delegates to the H4 block bootstrap."""
    return block_ci(xs, days, hz=hz, B=B, seed=seed)


def summarize(out: list[dict[str, Any]], hz: str = "1h",
              metric: str = "net_bp9.26") -> dict[str, Any]:
    """S0 summary.

    S0-4: PRIMARY verdict metrics are net absolute return at BOTH cost
    conventions — net_bp9.26 (realistic taker) and net_bp19 (stress). The
    tier-matched excess_* and BTC market_factor_* are SECONDARY benchmarks and
    are emitted under secondary_benchmarks, never the headline.
    S0-5: every group carries evidence_grade; any group spanning < 30 distinct
    UTC days is machine-readably 'inconclusive' (sign-only). The admitted main
    group additionally carries verdict_hint for the promotion gate.
    S0-6: top-level coverage disclosure (covered/uncovered, benchmark_status
    counts, uncovered_by_tier) so tier-unbenchmarked rows are never hidden.
    """
    covered = [r for r in out if r.get("covered")]
    total = len(out)
    MIN_DAYS = 30

    def distinct_days(rows):
        return {int(r["ts"]) // 86_400_000 for r in rows if r.get("ts")}

    def group_by(keyfn, metric_key):
        # S0-5: EVERY group carries evidence_grade + distinct_days so a short
        # sample is machine-readably inconclusive, not just the headline path.
        buckets: dict[str, list[float]] = {}
        days: dict[str, list[int]] = {}
        for r in covered:
            v = (r.get(metric_key) or {}).get(hz)
            if v is None:
                continue
            k = keyfn(r)
            buckets.setdefault(k, []).append(v)
            days.setdefault(k, []).append(int(r["ts"]) // 86_400_000)
        res = {}
        for k in sorted(buckets):
            stat = block_ci(buckets[k], days[k], hz=hz)
            ndays = len(set(days[k]))
            stat["evidence_grade"] = "inconclusive" if ndays < MIN_DAYS else "graded"
            stat["distinct_days"] = ndays
            res[k] = stat
        return res

    def group(metric_key):
        return group_by(lambda r: r["path"], metric_key)

    # S0-6 coverage disclosure (also covers the pre-existing uncovered_by_tier).
    gap: dict[str, int] = {}
    for r in out:
        if not r.get("covered"):
            gap[r.get("volume_tier") or "unknown"] = gap.get(r.get("volume_tier") or "unknown", 0) + 1
    bench_status: dict[str, int] = {}
    for r in covered:
        bs = r.get("benchmark_status") or "unset"
        bench_status[bs] = bench_status.get(bs, 0) + 1

    admitted = [r for r in covered if r.get("admitted")]
    adm_days = len(distinct_days(admitted))

    def verdict_hint(metric_key):
        vals = [(r.get(metric_key) or {}).get(hz) for r in admitted]
        vals = [v for v in vals if v is not None]
        if not vals:
            return {"grade": "no_data"}
        import statistics as _st
        med = _st.median(vals)
        stat = block_ci(vals, [int(r["ts"]) // 86_400_000 for r in admitted if (r.get(metric_key) or {}).get(hz) is not None], hz=hz)
        lo = stat.get("ci95_pct")
        ci_ok = bool(lo and lo[0] is not None and lo[0] > 0)
        grade = "inconclusive" if adm_days < MIN_DAYS else (
            "edge_indicated" if (med > 0 and ci_ok) else "no_edge")
        return {"grade": grade, "n": len(vals), "distinct_days": adm_days,
                "median_pct": round(100 * med, 4),
                "ci95_pct": lo, "median_positive": med > 0, "ci_lower_gt_0": ci_ok}

    return {
        "horizon": hz,
        "primary_metric": metric,
        "metric_roles": {
            "primary": ["net_bp9.26", "net_bp19"],
            "secondary_benchmarks": ["excess_bp9.26", "excess_bp19",
                                     "market_factor_bp9.26", "market_factor_bp19"],
        },
        "bootstrap": "calendar block bootstrap, block_days>=horizon, point=block mean (H4)",
        "total_rows": total, "covered": len(covered),
        "coverage_pct": round(100 * len(covered) / total, 2) if total else 0.0,
        "by_path_net_bp9.26": group("net_bp9.26"),
        "by_path_net_bp19": group_by(lambda r: r["path"], "net_bp19"),
        "by_admit_net_bp9.26": group_by(
            lambda r: "admitted" if r.get("admitted") else "blocked", "net_bp9.26"),
        "by_tier_net_bp9.26": group_by(
            lambda r: r.get("bench_tier") or "no_tier", "net_bp9.26"),
        "by_trigger_net_bp9.26": group_by(
            lambda r: "|".join(r.get("fired_names") or ["none"]), "net_bp9.26"),
        "admitted_main_group": {
            "net_bp9.26": verdict_hint("net_bp9.26"),
            "net_bp19": verdict_hint("net_bp19"),
        },
        "secondary_benchmarks": {
            "by_path_excess_bp9.26": group_by(lambda r: r["path"], "excess_bp9.26"),
            "by_path_market_factor_bp9.26": group_by(
                lambda r: r["path"], "market_factor_bp9.26"),
        },
        "coverage": {
            "uncovered_by_tier": gap,
            "benchmark_status_counts": bench_status,
            "covered_tier_counts": {t: sum(1 for r in covered if r.get("bench_tier") == t)
                                    for t in ("mega", "mid", "micro")},
            "covered_no_tier": sum(1 for r in covered if r.get("bench_tier") is None),
        },
        "uncovered_by_tier": gap,  # back-compat top-level key
    }


def build_tier_benchmarks(rows: list[dict[str, Any]], src: "CandleSource"):
    """S0-3: build tier -> [(ts, closes)] peer benchmark sets + auditable map.

    A coin is eligible as a benchmark only if it (a) appears in the evaluated
    candidate set, (b) has a resolvable 5m close series, and (c) has an observed
    volume_24h_usd. Its STATIC tier comes from the median observed 24h USD volume
    across its rows (robust to a single thin/fat print) mapped by map_bench_tier.
    The resolved {coin: {median_volume_usd, tier, has_series}} map is returned
    for on-disk audit. bench_by_tier maps tier -> [(coin, (ts, closes))] so
    enrich can EXCLUDE the candidate coin from its own peer mean (a micro tier
    with only a few peers would otherwise be materially self-correlated).
    """
    import statistics as _st
    vol_by_coin: dict[str, list[float]] = {}
    for r in rows:
        c = r.get("coin")
        v = r.get("volume_24h_usd")
        if c and v is not None:
            try:
                vol_by_coin.setdefault(c, []).append(float(v))
            except (TypeError, ValueError):
                pass
    tier_map: dict[str, dict[str, Any]] = {}
    bench_by_tier: dict[str, list] = {"mega": [], "mid": [], "micro": []}
    for c, vols in vol_by_coin.items():
        med = _st.median(vols)
        tier = map_bench_tier(med)
        if tier is None:
            continue
        series = src.series(c)
        rec = {"median_volume_24h_usd": round(med, 2), "tier": tier,
               "has_5m_series": bool(series), "n_observed": len(vols)}
        tier_map[c] = rec
        if series:
            bench_by_tier[tier].append((c, series))
    return bench_by_tier, tier_map


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pattern", default=os.path.join(_DATA_DIR, "runner_gate_shadow.jsonl*"))
    ap.add_argument("--cache", default=os.path.join(_DATA_DIR, "ch_hist_candles.json"))
    ap.add_argument("--out", default=os.path.join(_DATA_DIR, "decision_shadow_outcomes.jsonl"))
    ap.add_argument("--summary-out", default="")
    ap.add_argument("--no-network", action="store_true")
    ap.add_argument("--max-rows", type=int, default=0)
    ap.add_argument("--horizon", default="1h", choices=list(HORIZONS))
    ap.add_argument("--source", choices=["hyperliquid", "binance"],
                    default="hyperliquid")
    args = ap.parse_args()

    rows = load_rows(args.pattern)
    rows.sort(key=lambda r: r.get("ts", 0))
    if args.max_rows:
        rows = rows[-args.max_rows:]
    # Physical parseable source lines across the glob — the truncation floor.
    # Distinct from len(rows): under a rotation race load_rows itself can read
    # a short snapshot, so guarding only against its own count would miss loss.
    src_phys_n = 0 if args.max_rows else count_source_lines(args.pattern)
    src = CandleSource(args.cache, use_network=not args.no_network,
                       source=args.source)
    # S0-3: tier-matched peer benchmark set + auditable static tier map.
    bench_by_tier, tier_map = build_tier_benchmarks(rows, src)
    market_factor = src.series(MARKET_FACTOR_COIN)
    enriched = enrich(rows, src, bench_by_tier, market_factor=market_factor)
    # Persist the resolved static tier map next to the summary (auditable).
    if args.summary_out:
        tier_map_path = args.summary_out.replace(".json", "_bench_tier_map.json")
        with open(tier_map_path, "w") as f:
            json.dump({
                "thresholds_usd": {"mega_min": TIER_MEGA_MIN_USD,
                                   "mid_min": TIER_MID_MIN_USD,
                                   "micro_below": TIER_MID_MIN_USD},
                "market_factor_coin": MARKET_FACTOR_COIN,
                "peer_counts": {t: len(v) for t, v in bench_by_tier.items()},
                "coin_tiers": dict(sorted(tier_map.items())),
            }, f, ensure_ascii=False, indent=2)

    # ADR-0012 / v2 §4.4 truncation guard: this job rewrites the whole derived
    # file and races log rotation (~00:15-00:19 UTC). On 2026-11-01 a rotation
    # race made load_rows see a partial source snapshot; silently rewriting the
    # output then permanently dropped 75 rows (37 more on 11-02). Refuse to
    # publish an output materially below the physical source line count.
    # Checked BEFORE writing so a bad run never overwrites the previous good
    # output. max_rows intentionally truncates and bypasses the guard.
    out_n = len(enriched)
    if src_phys_n and out_n < src_phys_n * 0.98:
        print(f"[FATAL] truncated: source_lines={src_phys_n} out={out_n} "
              f"(< 98% of source; likely a rotation race — output NOT written)",
              file=sys.stderr)
        return 2

    out_dir = os.path.dirname(args.out)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    # Atomic publish: write a temp file in the same directory, fsync, then
    # os.replace (atomic on POSIX) so a concurrent reader / the next rotation
    # can never observe a half-written outcomes file.
    tmp_out = args.out + ".tmp"
    with open(tmp_out, "w") as f:
        for r in enriched:
            f.write(json.dumps(r, ensure_ascii=False) + chr(10))
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_out, args.out)
    summary = summarize(enriched, hz=args.horizon)
    txt = json.dumps(summary, ensure_ascii=False, indent=2)
    print(txt)
    if args.summary_out:
        with open(args.summary_out, "w") as f:
            f.write(txt + chr(10))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
