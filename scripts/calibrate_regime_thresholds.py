#!/usr/bin/env python3
"""Automated calibration of the production regime classifier.

The production detector (`hermes_trader.agents.market_regime`) classifies each
1h candle window with a binary EMA trend (EMA fast/slow cross + an N-bar slope
threshold) and an ADX chop overlay. The backtest uses a calibrated 5-component
weighted score (ADX/ATR/EMA-gap/price-extension/OBV) with empirically fit
thresholds (>=0.70 STRONG_TREND, >=0.55 TREND, >=0.40 NEUTRAL, else CHOP). The
audit (scripts/audit_neutral_chop.py) measured a 36% false-trend rate:
production calls "trend" on bars the backtest rates NEUTRAL/CHOP.

This script grid-searches the production classifier's three threshold families
  - EMA fast/slow periods
  - slope threshold (fractional move over a fixed 8-bar fast-EMA lookback)
  - ADX chop threshold (only applied when the EMA verdict is neutral)
to maximize agreement with the backtest's collapsed 3-state label
(TREND / NEUTRAL / CHOP) on identical historical 1h candles.

Both classifiers run on the SAME trailing 100-bar window the live detector
fetches (`fetch_hl_candles(..., count=100)`), so the calibrated constants are
directly applicable to production. The search objective is the equal-weight
mean of the two costs the operator cares about:

  false_trend_rate = P(prod=TREND | bt=NEUTRAL/CHOP)   # over-exposure
  missed_trend_rate = P(bt=TREND | prod=neutral/chop)  # under-exposure

with 3-state agreement as the tiebreaker. The score>=0.55 entry overlay
(deployed in market_regime_gate) already eliminates false trends at entry time;
calibrating the binary classifier itself keeps the cached `regime` label, the
operator console, and sizing logic aligned with the score without requiring a
score fetch on every call path.

Outputs the best parameter set, a before/after comparison against the current
production constants, and a ready-to-paste constants block. Optionally writes
the full grid to JSON for offline inspection.

Usage:
    python3 scripts/calibrate_regime_thresholds.py --days 30 --coins 20
    python3 scripts/calibrate_regime_thresholds.py --coin-list BTC,ETH,SOL --days 60
    python3 scripts/calibrate_regime_thresholds.py --write-json /tmp/calib.json
"""
from __future__ import annotations

import argparse
import itertools
import json
import os
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Dict, List, Optional, Tuple

_REPO = Path(__file__).resolve().parents[1]
_env = _REPO / ".env.local"
if _env.is_file():
    for _line in _env.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _, _v = _line.partition("=")
            if _k.strip() == "HYPERLIQUID_PRIVATE_KEY":
                continue
            os.environ.setdefault(_k.strip(), _v.strip())
sys.path.insert(0, str(_REPO))
sys.path.insert(0, str(_REPO / "scripts"))

from hermes_trader.client.hl_client import fetch_hl_candles  # noqa: E402
from hermes_trader.indicators.math import adx as _adx_ind, ema as _ema  # noqa: E402
from compare_regime_architectures import backtest_label  # noqa: E402

# Live detector fetches count=100 1h candles; calibrate on the same window so
# results transfer directly (ema/closes indexing assumes this length).
WINDOW = 100
# Fixed slope lookback (matches market_regime._SLOPE_LOOKBACK); the search
# varies the slope MAGNITUDE, not the lookback window, to keep the grid small.
SLOPE_LOOKBACK = 8

# Current production constants (baseline row in the report).
BASELINE = {"fast": 20, "slow": 50, "slope": 0.001, "adx": 20.0}

# Collapsed 3-state label space used for the alignment objective.
TREND, NEUTRAL, CHOP = "TREND", "NEUTRAL", "CHOP"


# ---------------------------------------------------------------------------
# Parameterised production classifier (mirrors market_regime._classify_candles)
# ---------------------------------------------------------------------------

def _last_finite(arr: List[float]) -> Optional[float]:
    for v in reversed(arr):
        if v == v and v != float("inf"):
            return v
    return None


def _classify_precomputed(fast_now: float, fast_prev: float, slow_now: float,
                          adx_val: Optional[float], slope_thr: float,
                          adx_thr: float) -> str:
    """Parameterised verdict from precomputed EMA endpoints + ADX.

    EMA verdict first (fast vs slow + 8-bar fast-EMA slope); only when neutral
    does the ADX overlay upgrade to 'chop'."""
    if not fast_prev or fast_prev != fast_prev:
        return "neutral"
    slope = (fast_now - fast_prev) / abs(fast_prev)
    if fast_now > slow_now and slope > slope_thr:
        trend = "up"
    elif fast_now < slow_now and slope < -slope_thr:
        trend = "down"
    else:
        trend = "neutral"
    if trend != "neutral":
        return trend
    if adx_val is not None and adx_val < adx_thr:
        return "chop"
    return "neutral"


def _to_strength(prod: str) -> str:
    if prod in ("up", "down"):
        return TREND
    if prod == "chop":
        return CHOP
    return NEUTRAL


# ---------------------------------------------------------------------------
# Per-coin precompute: backtest target labels + the final EMA values for every
# searched period on each trailing WINDOW slice, plus precomputed ADX. The grid
# then does O(1) work per window instead of recomputing EMAs 240x.
# ---------------------------------------------------------------------------

def precompute_coin(candles: list, periods: List[int]) -> Optional[Dict]:
    """For every trailing WINDOW slice produce (bt_strength, adx_val, ema ends).

    ema_ends[period] = list of (fast_now, fast_prev_8bars) tuples — actually the
    last EMA value and the value SLOPE_LOOKBACK bars earlier for that period.
    Returns None if the coin has insufficient candles."""
    n = len(candles)
    if n < WINDOW + 2:
        return None
    targets: List[str] = []
    adx_vals: List[Optional[float]] = []
    ema_ends: Dict[int, List[Tuple[float, float]]] = {p: [] for p in periods}
    for end in range(WINDOW, n):
        window = candles[end - WINDOW: end]
        closes = [float(c.c) for c in window]
        _, bt_raw, _score = backtest_label(window, closes)
        if bt_raw in ("STRONG_TREND", "TREND"):
            targets.append(TREND)
        elif bt_raw == "CHOP":
            targets.append(CHOP)
        else:
            targets.append(NEUTRAL)
        for p in periods:
            arr = _ema(closes, p)
            now_v = arr[-1]
            prev_v = arr[-(SLOPE_LOOKBACK + 1)] if len(arr) > SLOPE_LOOKBACK else float("nan")
            ema_ends[p].append((now_v, prev_v))
        try:
            adx_vals.append(_last_finite(_adx_ind(window, 14)))
        except Exception:
            adx_vals.append(None)
    return {"n": len(targets), "targets": targets, "adx": adx_vals,
            "ema_ends": ema_ends}


def evaluate(coins_data: List[Dict], fast: int, slow: int, slope_thr: float,
             adx_thr: float) -> Dict:
    """Aggregate 3-state confusion + costs over all precomputed coins."""
    matrix: Dict[str, Counter] = {
        TREND: Counter(), NEUTRAL: Counter(), CHOP: Counter()}
    total = 0
    fast_ends_all = [d["ema_ends"][fast] for d in coins_data]
    slow_ends_all = [d["ema_ends"][slow] for d in coins_data]
    for di, data in enumerate(coins_data):
        fast_ends = fast_ends_all[di]
        slow_ends = slow_ends_all[di]
        for k in range(data["n"]):
            f_now, f_prev = fast_ends[k]
            s_now, _ = slow_ends[k]
            pred = _to_strength(_classify_precomputed(
                f_now, f_prev, s_now, data["adx"][k], slope_thr, adx_thr))
            matrix[data["targets"][k]][pred] += 1
            total += 1

    bt_trend_n = sum(matrix[TREND].values())
    bt_nontrend_n = sum(matrix[NEUTRAL].values()) + sum(matrix[CHOP].values())
    prod_nontrend_n = (matrix[TREND][NEUTRAL] + matrix[TREND][CHOP]
                       + matrix[NEUTRAL][NEUTRAL] + matrix[NEUTRAL][CHOP]
                       + matrix[CHOP][NEUTRAL] + matrix[CHOP][CHOP])
    false_trend = matrix[NEUTRAL][TREND] + matrix[CHOP][TREND]
    missed_trend = matrix[TREND][NEUTRAL] + matrix[TREND][CHOP]
    ft_rate = false_trend / bt_nontrend_n if bt_nontrend_n else 0.0
    mt_rate = missed_trend / prod_nontrend_n if prod_nontrend_n else 0.0
    diag = matrix[TREND][TREND] + matrix[NEUTRAL][NEUTRAL] + matrix[CHOP][CHOP]
    agree = diag / total if total else 0.0
    cost = (ft_rate + mt_rate) / 2.0
    return {
        "fast": fast, "slow": slow, "slope": slope_thr, "adx": adx_thr,
        "total": total, "agree": agree, "false_trend": ft_rate,
        "missed_trend": mt_rate, "cost": cost,
        "matrix": {r: dict(matrix[r]) for r in (TREND, NEUTRAL, CHOP)},
    }


# ---------------------------------------------------------------------------
# Coin selection / fetch
# ---------------------------------------------------------------------------

def _pick_coins(n: int, explicit: str) -> List[str]:
    if explicit:
        return [c.strip() for c in explicit.split(",") if c.strip()]
    from hermes_trader.client.universe import get_universe
    uni = get_universe(include_hip3=False)
    uni.sort(key=lambda m: float(m.get("dayNtlVlm", 0) or 0), reverse=True)
    return [m["coin"] for m in uni[:n]]


def _print_result(title: str, r: Dict) -> None:
    m = r["matrix"]
    print(f"\n  {title}")
    print(f"    fast={r['fast']}  slow={r['slow']}  "
          f"slope={r['slope']:.4f}  adx<{r['adx']:.0f}")
    print(f"    {'':12}{'prod TREND':>12}{'prod NEUT':>12}{'prod CHOP':>12}")
    for row in (TREND, NEUTRAL, CHOP):
        print(f"    bt {row:<8}{m[row].get(TREND,0):>12}"
              f"{m[row].get(NEUTRAL,0):>12}{m[row].get(CHOP,0):>12}")
    print(f"    agreement={r['agree']*100:.1f}%  "
          f"false_trend={r['false_trend']*100:.1f}%  "
          f"missed_trend={r['missed_trend']*100:.1f}%  "
          f"cost={r['cost']*100:.2f}%")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--coins", type=int, default=20)
    ap.add_argument("--coin-list", type=str, default="")
    ap.add_argument("--fast", type=str, default="8,10,15,20",
                    help="comma-separated EMA fast periods to search")
    ap.add_argument("--slow", type=str, default="21,30,40,50",
                    help="comma-separated EMA slow periods to search")
    ap.add_argument("--slopes", type=str, default="0.0005,0.001,0.0015,0.002",
                    help="comma-separated slope thresholds (fractional over "
                         "8 bars; 0.001 = 0.1%%)")
    ap.add_argument("--adx", type=str, default="15,18,20,22,25",
                    help="comma-separated ADX chop thresholds")
    ap.add_argument("--top", type=int, default=10,
                    help="print the top-N parameter sets")
    ap.add_argument("--write-json", type=str, default="",
                    help="optional path to dump the full ranked grid as JSON")
    args = ap.parse_args()

    fasts = [int(x) for x in args.fast.split(",") if x.strip()]
    slows = [int(x) for x in args.slow.split(",") if x.strip()]
    slopes = [float(x) for x in args.slopes.split(",") if x.strip()]
    adxs = [float(x) for x in args.adx.split(",") if x.strip()]

    count = args.days * 24 + WINDOW + 50
    coins = _pick_coins(args.coins, args.coin_list)
    periods = sorted(set(fasts + slows))

    print(f"=== Production regime threshold calibration "
          f"({args.days}d, {len(coins)} coins, window={WINDOW}h) ===")
    print(f"grid: fast={fasts} slow={slows} slope={slopes} adx={adxs}")
    print(f"baseline constants: fast={BASELINE['fast']} slow={BASELINE['slow']} "
          f"slope={BASELINE['slope']} adx<{BASELINE['adx']}")
    print()

    coins_data: List[Dict] = []
    for ci, coin in enumerate(coins, 1):
        try:
            candles = fetch_hl_candles(coin, "1h", count)
        except Exception as e:
            print(f"  [{ci}/{len(coins)}] {coin}: fetch failed: {e}")
            continue
        data = precompute_coin(candles, periods)
        if data is None or data["n"] == 0:
            print(f"  [{ci}/{len(coins)}] {coin}: insufficient candles, skipped")
            continue
        coins_data.append(data)
        print(f"  [{ci}/{len(coins)}] {coin:>10}  windows={data['n']}")

    if not coins_data:
        print("No data — check network/universe.")
        return 1

    grid = list(itertools.product(fasts, slows, slopes, adxs))
    print(f"\nEvaluating {len(grid)} parameter sets over "
          f"{sum(d['n'] for d in coins_data)} windows ...")
    results = []
    for fast, slow, slope_thr, adx_thr in grid:
        if fast >= slow:
            continue
        results.append(evaluate(coins_data, fast, slow, slope_thr, adx_thr))

    results.sort(key=lambda r: (r["cost"], -r["agree"]))
    best = results[0]
    baseline = next((r for r in results
                     if r["fast"] == BASELINE["fast"]
                     and r["slow"] == BASELINE["slow"]
                     and abs(r["slope"] - BASELINE["slope"]) < 1e-9
                     and abs(r["adx"] - BASELINE["adx"]) < 1e-9), None)

    _print_result("BASELINE (current production constants)", baseline)
    _print_result("BEST (calibrated constants)", best)

    print("\n  Change:")
    print(f"    agreement      {baseline['agree']*100:5.1f}% -> "
          f"{best['agree']*100:5.1f}%  "
          f"({(best['agree']-baseline['agree'])*100:+.1f} pts)")
    print(f"    false_trend    {baseline['false_trend']*100:5.1f}% -> "
          f"{best['false_trend']*100:5.1f}%  "
          f"({(best['false_trend']-baseline['false_trend'])*100:+.1f} pts)")
    print(f"    missed_trend   {baseline['missed_trend']*100:5.1f}% -> "
          f"{best['missed_trend']*100:5.1f}%  "
          f"({(best['missed_trend']-baseline['missed_trend'])*100:+.1f} pts)")

    print(f"\n  Top {min(args.top, len(results))} parameter sets "
          f"(sorted by cost, then agreement):")
    print(f"    {'fast':>5}{'slow':>6}{'slope':>9}{'adx':>6}"
          f"{'agree%':>9}{'false%':>9}{'miss%':>9}{'cost%':>9}")
    for r in results[:args.top]:
        print(f"    {r['fast']:>5}{r['slow']:>6}{r['slope']:>9.4f}"
              f"{r['adx']:>6.0f}{r['agree']*100:>9.1f}"
              f"{r['false_trend']*100:>9.1f}{r['missed_trend']*100:>9.1f}"
              f"{r['cost']*100:>9.2f}")

    print("\n  Suggested constants block "
          "(hermes_trader/agents/market_regime.py):")
    print(f"    _SLOPE_UP = {best['slope']:.4f}")
    print(f"    _SLOPE_DOWN = -{best['slope']:.4f}")
    print(f"    _CHOP_ADX_MAX = {best['adx']:.1f}")
    print(f"    # EMA periods in _trend_from_closes: fast={best['fast']}, "
          f"slow={best['slow']}")
    print("  (Re-run this script on a rolling basis — e.g. weekly — to keep "
          "the classifier aligned with evolving market microstructure.)")

    if args.write_json:
        payload = {"baseline": baseline, "best": best,
                   "top": results[:args.top], "coins": len(coins_data),
                   "windows": sum(d["n"] for d in coins_data)}
        Path(args.write_json).write_text(json.dumps(payload, indent=2))
        print(f"\n  Full grid (top {args.top}) written to {args.write_json}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
