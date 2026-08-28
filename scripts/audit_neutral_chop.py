#!/usr/bin/env python3
"""NEUTRAL/CHOP calibration audit: backtest vs production regime labels.

The production detector (`market_regime._classify_candles`, EMA20/50 + ADX<20
chop overlay) has no strength score and is known to over-label non-trending
tape as a trend. The backtest's 5-component score distinguishes
STRONG_TREND / TREND / NEUTRAL / CHOP with calibrated thresholds. This script
focuses specifically on the disagreement the user flagged — where one side
says NEUTRAL/CHOP and the other says a trend — to decide whether production
entry logic needs a stricter regime gate.

For every 1h bar (after warmup) on each coin it runs BOTH classifiers on
identical candles and reports, restricted to the non-trend / trend boundary:

  1. Rows where BACKTEST = NEUTRAL or CHOP  -> what did production say?
     (production false-trend rate: how often it enters a regime the backtest
      considers non-tradable)
  2. Rows where PRODUCTION = neutral/chop  -> what did backtest say?
     (production missed-trend rate: how often it sits out while the backtest
      sees a STRONG_TREND)
  3. The NEUTRAL/CHOP-only 2x2 exact-agreement table
  4. A calibration verdict: whether production entry should require the
     backtest score (or a stricter in-house proxy) before firing.

Usage:
    python3 scripts/audit_neutral_chop.py --days 30 --coins 20
    python3 scripts/audit_neutral_chop.py --days 30 --coin-list BTC,ETH
"""
from __future__ import annotations

import argparse
import os
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Dict, List, Tuple

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

from hermes_trader.client.hl_client import fetch_hl_candles  # noqa: E402

# Reuse the byte-aligned classifiers from the architecture comparison.
from compare_regime_architectures import (  # noqa: E402
    WARMUP,
    backtest_label,
    production_label,
)

# Non-trend states in each classifier's own label vocabulary.
BT_NONTREND = {"NEUTRAL", "CHOP"}      # backtest raw labels
BT_TREND = {"TREND", "STRONG_TREND"}
PROD_NONTREND = {"neutral", "chop"}    # production common labels
PROD_TREND = {"trend_up", "trend_down"}


def _pick_coins(n: int, explicit: str) -> List[str]:
    if explicit:
        return [c.strip() for c in explicit.split(",") if c.strip()]
    # Import lazily so --coin-list works without the universe fetch.
    from hermes_trader.client.universe import get_universe
    uni = get_universe(include_hip3=False)
    uni.sort(key=lambda m: float(m.get("dayNtlVlm", 0) or 0), reverse=True)
    return [m["coin"] for m in uni[:n]]


def audit_coin(coin: str, candles, min_score: float = 0.55) -> Dict:
    n = len(candles)
    # Rows indexed by backtest raw label, cols by production common label.
    cross: Dict[str, Counter] = defaultdict(Counter)
    # Same grid but after applying the continuous-strength gate: a production
    # trend call with backtest 5-component score < min_score is demoted to
    # "weak_trend" (treated as non-aligned by market_regime_gate, i.e. it loses
    # the aligned discount and must clear the counter-trend conviction bar).
    cross_gated: Dict[str, Counter] = defaultdict(Counter)
    bt_strength_score: Dict[str, List[float]] = defaultdict(list)
    # Backtest score grouped by production label (for View 4).
    score_by_prod: Dict[str, List[float]] = defaultdict(list)
    bars = 0
    for i in range(WARMUP, n - 1):
        window = candles[: i + 1]
        closes = [c.c for c in window]
        if len(closes) < 50:
            continue
        _, bt_raw, score = backtest_label(window, closes)
        prod_common = production_label(window)
        cross[bt_raw][prod_common] += 1
        if prod_common in PROD_TREND and score < min_score:
            prod_gated = "weak_trend"
        else:
            prod_gated = prod_common
        cross_gated[bt_raw][prod_gated] += 1
        bt_strength_score[bt_raw].append(score)
        score_by_prod[prod_common].append(score)
        bars += 1
    return {"cross": cross, "cross_gated": cross_gated,
            "scores": bt_strength_score,
            "score_by_prod": score_by_prod, "bars": bars}


def _merge(agg: Dict[str, Counter], add: Dict[str, Counter]) -> None:
    for r, row in add.items():
        for c, v in row.items():
            agg[r][c] += v


def _merge_scores(agg: Dict[str, List[float]],
                  add: Dict[str, List[float]]) -> None:
    for k, vals in add.items():
        agg[k].extend(vals)


def _false_trend(cross: Dict[str, Counter], trend_cols) -> Tuple[int, int]:
    """Return (false-trend bars, total non-trend bars) over bt NEUTRAL/CHOP."""
    ft = 0
    tot = 0
    for bt in ("NEUTRAL", "CHOP"):
        row = cross.get(bt, Counter())
        trend_n = sum(row.get(s, 0) for s in trend_cols)
        all_n = sum(row.values())
        ft += trend_n
        tot += all_n
    return ft, tot


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--coins", type=int, default=20)
    ap.add_argument("--coin-list", type=str, default="")
    ap.add_argument("--min-score", type=float, default=0.55,
                    help="Continuous strength gate applied to production "
                         "trend calls (default 0.55, matches .agent-config).")
    args = ap.parse_args()

    count = args.days * 24 + 200
    coins = _pick_coins(args.coins, args.coin_list)

    print(f"=== NEUTRAL/CHOP calibration audit "
          f"({args.days}d, {len(coins)} coins, min_score={args.min_score}) ===")
    print("backtest = 5-component score (STRONG_TREND/TREND/NEUTRAL/CHOP)")
    print("production = EMA20/50 + ADX<20 (trend_up/down/neutral/chop)")
    print(f"gate = production trend demoted to weak_trend when score<{args.min_score}")
    print()

    agg_cross: Dict[str, Counter] = defaultdict(Counter)
    agg_cross_gated: Dict[str, Counter] = defaultdict(Counter)
    agg_scores: Dict[str, List[float]] = defaultdict(list)
    agg_prod_scores: Dict[str, List[float]] = defaultdict(list)
    total_bars = 0
    per_coin: List[Tuple[str, Dict]] = []

    for ci, coin in enumerate(coins, 1):
        try:
            candles = fetch_hl_candles(coin, "1h", count)
        except Exception as e:
            print(f"  [{ci}/{len(coins)}] {coin}: fetch failed: {e}")
            continue
        if len(candles) < WARMUP + 10:
            print(f"  [{ci}/{len(coins)}] {coin}: only {len(candles)} candles, skipped")
            continue
        res = audit_coin(coin, candles, min_score=args.min_score)
        _merge(agg_cross, res["cross"])
        _merge(agg_cross_gated, res["cross_gated"])
        _merge_scores(agg_scores, res["scores"])
        _merge_scores(agg_prod_scores, res["score_by_prod"])
        total_bars += res["bars"]
        per_coin.append((coin, res))

    if total_bars == 0:
        print("No bars evaluated.")
        return 1

    # ---- View 1: when BACKTEST says NEUTRAL/CHOP, what does production say?
    print("View 1 — bars where BACKTEST = NEUTRAL or CHOP "
          "(production false-trend exposure)")
    print(f"  {'bt label':<14}{'prod trend':>12}{'prod neutral':>14}"
          f"{'prod chop':>11}{'total':>8}{'false-trend%':>14}")
    print("  " + "-" * 73)
    ft_total = 0
    nontotal_total = 0
    for bt in ("NEUTRAL", "CHOP"):
        row = agg_cross.get(bt, Counter())
        trend_n = sum(row.get(s, 0) for s in PROD_TREND)
        neu_n = row.get("neutral", 0)
        chop_n = row.get("chop", 0)
        tot = trend_n + neu_n + chop_n
        ft_rate = trend_n / tot * 100 if tot else 0.0
        ft_total += trend_n
        nontotal_total += tot
        print(f"  {bt:<14}{trend_n:>12}{neu_n:>14}{chop_n:>11}"
              f"{tot:>8}{ft_rate:>13.1f}%")
    overall_ft = ft_total / nontotal_total * 100 if nontotal_total else 0.0
    print(f"  {'combined':<14}{ft_total:>12}{'':>14}{'':>11}"
          f"{nontotal_total:>8}{overall_ft:>13.1f}%")
    print()

    # ---- View 1b: same rows after the score>=min_score gate ---------------
    # A "false trend" is now a production trend call that ALSO clears the
    # continuous-score bar; sub-threshold trend calls become weak_trend and no
    # longer qualify as aligned entries.
    print(f"View 1b — after score>={args.min_score} gate "
          f"(weak_trend = prod trend but score<{args.min_score})")
    print(f"  {'bt label':<14}{'strong trend':>14}{'weak_trend':>12}"
          f"{'neutral':>10}{'chop':>8}{'false-trend%':>15}")
    print("  " + "-" * 73)
    gft_total = 0
    gtot_total = 0
    for bt in ("NEUTRAL", "CHOP"):
        row = agg_cross_gated.get(bt, Counter())
        strong = sum(row.get(s, 0) for s in PROD_TREND)
        weak = row.get("weak_trend", 0)
        neu = row.get("neutral", 0)
        chop = row.get("chop", 0)
        tot = strong + weak + neu + chop
        gft_total += strong
        gtot_total += tot
        grate = strong / tot * 100 if tot else 0.0
        print(f"  {bt:<14}{strong:>14}{weak:>12}{neu:>10}{chop:>8}{grate:>14.1f}%")
    gated_ft = gft_total / gtot_total * 100 if gtot_total else 0.0
    print(f"  {'combined':<14}{gft_total:>14}{'':>12}{'':>10}{'':>8}"
          f"{gated_ft:>14.1f}%")
    rel = (overall_ft - gated_ft) / overall_ft * 100 if overall_ft else 0.0
    print(f"  -> false-trend rate: {overall_ft:.1f}% -> {gated_ft:.1f}% "
          f"(relative reduction {rel:.1f}%)")
    print()

    # ---- View 2: when PRODUCTION says neutral/chop, what does backtest say?
    print("View 2 — bars where PRODUCTION = neutral/chop "
          "(missed-trend exposure)")
    print(f"  {'prod label':<14}{'bt STRONG':>12}{'bt TREND':>12}"
          f"{'bt NEUTRAL':>12}{'bt CHOP':>10}{'total':>8}{'missed%':>10}")
    print("  " + "-" * 78)
    miss_total = 0
    pn_total = 0
    for plbl in ("neutral", "chop"):
        strong = agg_cross.get("STRONG_TREND", Counter()).get(plbl, 0)
        trend = agg_cross.get("TREND", Counter()).get(plbl, 0)
        neu = agg_cross.get("NEUTRAL", Counter()).get(plbl, 0)
        chop = agg_cross.get("CHOP", Counter()).get(plbl, 0)
        tot = strong + trend + neu + chop
        missed = strong + trend
        miss_total += missed
        pn_total += tot
        mrate = missed / tot * 100 if tot else 0.0
        print(f"  {plbl:<14}{strong:>12}{trend:>12}{neu:>12}{chop:>10}"
              f"{tot:>8}{mrate:>9.1f}%")
    overall_miss = miss_total / pn_total * 100 if pn_total else 0.0
    print(f"  {'combined':<14}{'':>12}{'':>12}{'':>12}{'':>10}"
          f"{pn_total:>8}{overall_miss:>9.1f}%")
    print()

    # ---- View 3: full 4x4 cross matrix (rows=bt, cols=prod)
    print("View 3 — full cross matrix (rows: backtest, cols: production)")
    prod_cols = ["trend_up", "trend_down", "neutral", "chop"]
    bt_rows = ["STRONG_TREND", "TREND", "NEUTRAL", "CHOP"]
    header = "  " + "".join(f"{c:>14}" for c in prod_cols) + f"{'total':>9}"
    print(header)
    print("  " + "-" * (14 * len(prod_cols) + 9))
    col_tot = Counter()
    for r in bt_rows:
        row = agg_cross.get(r, Counter())
        cells = []
        rtot = 0
        for c in prod_cols:
            v = row.get(c, 0)
            cells.append(v)
            col_tot[c] += v
            rtot += v
        print(f"  {r:<14}" + "".join(f"{v:>14}" for v in cells)
              + f"{rtot:>9}")
    print("  " + "-" * (14 * len(prod_cols) + 9))
    print(f"  {'total':<14}"
          + "".join(f"{col_tot[c]:>14}" for c in prod_cols)
          + f"{total_bars:>9}")
    print()

    # ---- View 4: mean backtest score when production calls a trend
    print("View 4 — backtest score distribution by production label "
          "(calibration signal)")
    for plbl in prod_cols:
        vals = agg_prod_scores.get(plbl, [])
        if vals:
            mean = sum(vals) / len(vals)
            below = sum(1 for v in vals if v < 0.55)
            print(f"  prod={plbl:<11} n={len(vals):<6} "
                  f"mean bt-score={mean:.3f}  "
                  f"%bars score<0.55 (non-trend) = "
                  f"{below/len(vals)*100:.1f}%")
    print()

    # ---- Verdict
    print("=== Calibration verdict ===")
    print(f"  bars evaluated: {total_bars}")
    print(f"  production false-trend rate (bt=NEUTRAL/CHOP but prod=trend): "
          f"{overall_ft:.1f}%  ({ft_total}/{nontotal_total})")
    print(f"  after score>={args.min_score} gate: "
          f"{gated_ft:.1f}%  ({gft_total}/{gtot_total})  "
          f"(relative drop {(overall_ft-gated_ft)/overall_ft*100:.1f}%)"
          if overall_ft else
          f"  after score>={args.min_score} gate: {gated_ft:.1f}%")
    print(f"  production missed-trend rate (prod=neutral/chop but bt=TREND+): "
          f"{overall_miss:.1f}%  ({miss_total}/{pn_total})")
    if gated_ft <= 25.0 and overall_ft > 25.0:
        print("  -> The score>=0.55 gate brings false-trend exposure under the "
              "25% warning line while preserving high-conviction overrides "
              "(weak trends are demoted, not hard-blocked).")
    if overall_ft > 25.0 and gated_ft > 25.0:
        print("  -> HIGH false-trend rate. Production is over-allocating to "
              "non-trend tape. Recommend gating production entries on the "
              "5-component strength score (>=0.55) or a proxy threshold "
              "(ADX>=20 + EMA spread + OBV confirm) before sizing.")
    elif overall_ft > 15.0:
        print("  -> MODERATE false-trend rate. Consider a stricter entry "
              "confirmation (ADX>=22 or EMA20/50 spread floor) for "
              "counter/neutral regime signals.")
    else:
        print("  -> LOW false-trend rate. Production NEUTRAL/CHOP handling is "
              "adequate; no strictness calibration required.")
    if overall_miss > 20.0:
        print("  -> NOTE: missed-trend rate is also elevated, so a simple "
              "stricter gate may forgo valid trends. Prefer the continuous "
              "score over a binary ADX chop overlay.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
