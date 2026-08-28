#!/usr/bin/env python3
"""Regime architecture consistency check: backtest (5-component weighted score)
vs production (EMA20/50 + ADX four-state).

Fetches the SAME historical 1h candles for each coin, then at every bar after
warmup runs both classifiers on identical input and reports:

  1. A 4-state confusion matrix in a common label space:
        trend_up / trend_down / neutral / chop
     - backtest STRONG_TREND/TREND are mapped to trend_up/trend_down using
       EMA8 vs EMA21 (the backtest direction reference)
     - production up/down/neutral/chop pass through directly
  2. A strength-only 3x3 matrix (direction collapsed) that isolates
     "how well do the two STRETH classifiers agree?" from the EMA-pair
     direction-method mismatch (backtest EMA8/21 vs production EMA20/50).
  3. A proxy-mode comparison that runs production classification on the BTC /
     SP500 proxy candles (production's actual behaviour) instead of each
     coin's own candles, to quantify the proxy-vs-own divergence.

Usage:
    python3 scripts/compare_regime_architectures.py --days 30 --coins 20
    python3 scripts/compare_regime_architectures.py --days 30 --coins BTC,ETH,SOL
    python3 scripts/compare_regime_architectures.py --days 30 --coins 20 --proxy
"""
from __future__ import annotations

import argparse
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

from hermes_trader.agents.market_regime import (  # noqa: E402
    CRYPTO_PROXY,
    EQUITY_PROXY,
    _classify_candles,
    classify_asset,
)
from hermes_trader.client.hl_client import fetch_hl_candles  # noqa: E402
from hermes_trader.client.universe import get_universe  # noqa: E402

# Reuse the backtest's exact regime-score implementation + indicator helpers so
# the "backtest" column here is byte-for-byte the production-candidate logic.
from backtest_ab_compare import (  # noqa: E402
    _adx_val,
    _atr_val,
    _ema_val,
    _obv_slope,
    _regime_score,
)

WARMUP = 120  # match backtest_ab_compare._simulate warmup

# Common 4-state label space.
STATES = ["trend_up", "trend_down", "neutral", "chop"]
STRENGTH_STATES = ["TREND", "NEUTRAL", "CHOP"]  # direction collapsed


# ---------------------------------------------------------------------------
# Label mapping
# ---------------------------------------------------------------------------

def backtest_label(window_1h, closes_1h) -> Tuple[str, str, float]:
    """Return (common_4state, backtest_raw, score) for a window using the
    backtest's exact 5-component score + EMA8/21 direction reference."""
    e8 = _ema_val(closes_1h, 8)
    e21 = _ema_val(closes_1h, 21)
    if e8 is None or e21 is None:
        return ("neutral", "NEUTRAL", 0.0)
    atr_v = _atr_val(window_1h)
    adx_v = _adx_val(window_1h)
    obv_dir = _obv_slope(window_1h)
    bullish = e8 > e21
    score, raw = _regime_score(
        window_1h, closes_1h, e8, e21, atr_v, adx_v, obv_dir, bullish,
    )
    if raw in ("STRONG_TREND", "TREND"):
        common = "trend_up" if bullish else "trend_down"
    elif raw == "CHOP":
        common = "chop"
    else:
        common = "neutral"
    return common, raw, score


def production_label(candles) -> str:
    """Run production's _classify_candles and map to the common 4-state space.
    Production already returns up/down/neutral/chop — direct mapping."""
    lbl = _classify_candles(candles)
    return {
        "up": "trend_up",
        "down": "trend_down",
        "neutral": "neutral",
        "chop": "chop",
    }[lbl]


def to_strength(common: str) -> str:
    if common in ("trend_up", "trend_down"):
        return "TREND"
    if common == "chop":
        return "CHOP"
    return "NEUTRAL"


# ---------------------------------------------------------------------------
# Confusion matrix
# ---------------------------------------------------------------------------

def _blank_matrix(states: List[str]) -> Dict[str, Dict[str, int]]:
    return {a: {b: 0 for b in states} for a in states}


def _print_matrix(title: str, matrix: Dict[str, Dict[str, int]],
                  row_states: List[str], col_states: List[str],
                  row_total: Dict[str, int]) -> None:
    print(f"\n  {title}")
    header = "    " + "".join(f"{c:>12}" for c in col_states) + f"{'total':>10}"
    print(header)
    print("    " + "-" * (12 * len(col_states) + 10))
    diag = 0
    total = 0
    for r in row_states:
        cells = matrix.get(r, {})
        row_sum = sum(cells.get(c, 0) for c in col_states)
        diag += cells.get(r, 0) if r in col_states else 0
        total += row_sum
        line = f"{r:>4}" + "".join(f"{cells.get(c, 0):>12}" for c in col_states)
        line += f"{row_sum:>10}"
        print(line)
    agree = diag / total * 100 if total else 0.0
    print(f"  exact agreement: {diag}/{total} = {agree:.1f}%")


# ---------------------------------------------------------------------------
# Per-coin comparison
# ---------------------------------------------------------------------------

def compare_coin(coin: str, candles_1h, proxy_candles_1h=None) -> Dict:
    """Compare both classifiers on identical per-coin windows. Returns a dict
    of counters/matrices for this coin."""
    n = len(candles_1h)
    matrix_4 = _blank_matrix(STATES)
    matrix_3 = _blank_matrix(STRENGTH_STATES)
    matrix_proxy = _blank_matrix(STATES)  # backtest(own) vs prod(proxy)
    bt_raw_counter: Counter = Counter()
    prod_counter: Counter = Counter()
    score_by_bt_raw: Dict[str, List[float]] = defaultdict(list)
    disagreements: List[Tuple[int, str, str, float]] = []

    # Build a timestamp -> proxy window index map for proxy mode (1h candles
    # across HL perps are aligned to the same hourly epoch).
    proxy_idx_by_ts: Dict[int, int] = {}
    if proxy_candles_1h:
        for idx, c in enumerate(proxy_candles_1h):
            proxy_idx_by_ts[c.t] = idx

    for i in range(WARMUP, n - 1):
        window = candles_1h[: i + 1]
        closes = [c.c for c in window]
        if len(closes) < 50:
            continue
        bt_common, bt_raw, score = backtest_label(window, closes)
        prod_common = production_label(window)

        bt_raw_counter[bt_raw] += 1
        prod_counter[prod_common] += 1
        score_by_bt_raw[bt_raw].append(score)
        matrix_4[bt_common][prod_common] += 1
        matrix_3[to_strength(bt_common)][to_strength(prod_common)] += 1

        if bt_common != prod_common:
            disagreements.append((i, bt_common, prod_common, score))

        # Proxy mode: production runs on proxy candles ending at this bar's ts.
        if proxy_candles_1h is not None:
            ts = candles_1h[i].t
            p_idx = proxy_idx_by_ts.get(ts)
            if p_idx is not None and p_idx + 1 >= WARMUP:
                proxy_window = proxy_candles_1h[: p_idx + 1]
                prod_proxy_common = production_label(proxy_window)
                matrix_proxy[bt_common][prod_proxy_common] += 1

    return {
        "matrix_4": matrix_4,
        "matrix_3": matrix_3,
        "matrix_proxy": matrix_proxy,
        "bt_raw": bt_raw_counter,
        "prod": prod_counter,
        "score_by_bt_raw": score_by_bt_raw,
        "disagreements": disagreements,
        "bars": max(0, n - 1 - WARMUP),
    }


def _pick_coins(n: int, explicit: Optional[str]) -> List[str]:
    if explicit:
        return [c.strip() for c in explicit.split(",") if c.strip()]
    uni = get_universe(include_hip3=False)
    # top N by 24h volume
    uni.sort(key=lambda m: float(m.get("dayNtlVlm", 0) or 0), reverse=True)
    return [m["coin"] for m in uni[:n]]


def _merge_matrices(agg: Dict[str, Dict[str, int]],
                    add: Dict[str, Dict[str, int]]) -> None:
    for r, row in add.items():
        for c, v in row.items():
            agg[r][c] = agg.get(r, {}).get(c, 0) + v


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--coins", type=int, default=20,
                    help="top N coins by 24h volume (when --coin-list not set)")
    ap.add_argument("--coin-list", type=str, default=None,
                    help="comma-separated explicit coin list (overrides --coins)")
    ap.add_argument("--proxy", action="store_true",
                    help="also compare production on its BTC/SP500 proxy vs "
                         "backtest on each coin's own candles")
    ap.add_argument("--show-disagreements", type=int, default=0,
                    help="print up to N sample disagreements per coin")
    args = ap.parse_args()

    count = args.days * 24 + 200  # buffer for warmup
    coins = _pick_coins(args.coins, args.coin_list)

    print(f"Comparing regime architectures on {len(coins)} coins, {args.days}d 1h")
    print(f"  backtest: 5-component weighted score (ADX0.25/ATR0.225/EMA0.175/"
          f"EXT0.175/OBV0.175) -> STRONG_TREND/TREND/NEUTRAL/CHOP, dir=EMA8/21")
    print(f"  production: EMA20/50 + 8-bar slope -> up/down; ADX(14)<20 -> chop")

    # Pre-fetch proxy candles once (crypto proxy = BTC). Equity/commodity coins
    # resolve their own proxy per coin; the per-coin proxy fetch is done below.
    crypto_proxy_candles = None
    if args.proxy:
        try:
            crypto_proxy_candles = fetch_hl_candles(CRYPTO_PROXY, "1h", count)
            print(f"  fetched {len(crypto_proxy_candles)} proxy candles for "
                  f"{CRYPTO_PROXY}")
        except Exception as e:
            print(f"  WARNING: could not fetch crypto proxy {CRYPTO_PROXY}: {e}")

    agg_4 = _blank_matrix(STATES)
    agg_3 = _blank_matrix(STRENGTH_STATES)
    agg_proxy = _blank_matrix(STATES)
    agg_bt_raw: Counter = Counter()
    agg_prod: Counter = Counter()
    agg_score: Dict[str, List[float]] = defaultdict(list)
    total_bars = 0
    per_coin_agree: List[Tuple[str, float]] = []

    for ci, coin in enumerate(coins, 1):
        try:
            candles = fetch_hl_candles(coin, "1h", count)
        except Exception as e:
            print(f"  [{ci}/{len(coins)}] {coin}: fetch failed: {e}")
            continue
        if len(candles) < WARMUP + 10:
            print(f"  [{ci}/{len(coins)}] {coin}: only {len(candles)} candles, skipped")
            continue

        proxy_candles = None
        if args.proxy:
            klass = classify_asset(coin)
            if klass == "crypto":
                proxy_candles = crypto_proxy_candles
            elif klass == "equity":
                try:
                    proxy_candles = fetch_hl_candles(EQUITY_PROXY, "1h", count)
                except Exception:
                    proxy_candles = None
            else:  # commodity / foreign index -> production uses own candles
                proxy_candles = candles

        res = compare_coin(coin, candles, proxy_candles)
        if res["bars"] == 0:
            continue

        _merge_matrices(agg_4, res["matrix_4"])
        _merge_matrices(agg_3, res["matrix_3"])
        if args.proxy:
            _merge_matrices(agg_proxy, res["matrix_proxy"])
        agg_bt_raw.update(res["bt_raw"])
        agg_prod.update(res["prod"])
        for k, v in res["score_by_bt_raw"].items():
            agg_score[k].extend(v)
        total_bars += res["bars"]

        # per-coin 4-state agreement
        diag = sum(res["matrix_4"][s][s] for s in STATES)
        tot = sum(sum(row.values()) for row in res["matrix_4"].values())
        agree = diag / tot * 100 if tot else 0.0
        per_coin_agree.append((coin, agree))
        print(f"  [{ci}/{len(coins)}] {coin:>10}  bars={res['bars']:>4}  "
              f"4-state agree={agree:5.1f}%  "
              f"bt[{dict(res['bt_raw'])}]  prod[{dict(res['prod'])}]")

        if args.show_disagreements and res["disagreements"]:
            shown = res["disagreements"][: args.show_disagreements]
            for bar_i, bt_c, prod_c, score in shown:
                ts = candles[bar_i].t
                print(f"      bar {bar_i} ts={ts}  bt={bt_c}(score={score:.2f}) "
                      f"prod={prod_c}")

    if total_bars == 0:
        print("\nNo bars compared — check network / universe.")
        return

    # ── Aggregate report ──────────────────────────────────────────────────
    print("\n" + "=" * 72)
    print(f"AGGREGATE over {len(per_coin_agree)} coins / {total_bars} bar-classifications")
    print("=" * 72)

    print("\n  Label distribution:")
    print(f"    {'backtest raw':<20}  " + "  ".join(
        f"{k}={v:>5} ({v/total_bars*100:4.1f}%)"
        for k, v in sorted(agg_bt_raw.items())))
    prod_total = sum(agg_prod.values())
    print(f"    {'production':<20}  " + "  ".join(
        f"{k}={v:>5} ({v/prod_total*100:4.1f}%)"
        for k, v in sorted(agg_prod.items())))

    print("\n  Mean backtest score per raw label:")
    for k in ("STRONG_TREND", "TREND", "NEUTRAL", "CHOP"):
        vals = agg_score.get(k) or []
        if vals:
            mean = sum(vals) / len(vals)
            print(f"    {k:<16} n={len(vals):>5}  mean_score={mean:.3f}")

    # 4-state matrix: rows=backtest, cols=production
    _print_matrix(
        "4-state confusion  (rows=backtest, cols=production, same per-coin candles)",
        agg_4, STATES, STATES, agg_bt_raw,
    )
    _print_matrix(
        "3-state strength-only  (direction collapsed; isolates EMA-pair "
        "direction mismatch)",
        agg_3, STRENGTH_STATES, STRENGTH_STATES, agg_bt_raw,
    )

    if args.proxy:
        _print_matrix(
            "PROXY mode: backtest(own candles) vs production(BTC/SP500 proxy) "
            "— production's actual live behaviour",
            agg_proxy, STATES, STATES, agg_bt_raw,
        )

    # Per-coin agreement ranking
    per_coin_agree.sort(key=lambda x: x[1])
    print("\n  Per-coin 4-state agreement (lowest first):")
    for coin, agree in per_coin_agree[:10]:
        print(f"    {coin:>10}  {agree:5.1f}%")
    if len(per_coin_agree) > 20:
        print("    ...")
        for coin, agree in per_coin_agree[-5:]:
            print(f"    {coin:>10}  {agree:5.1f}%")

    # ── Interpretation guide ──────────────────────────────────────────────
    print("\n" + "-" * 72)
    print("Interpretation:")
    print("  • 4-state agreement < 70%  → the two architectures classify tape")
    print("    differently on the SAME candles; a direct production cutover")
    print("    would shift regime-gate behaviour materially.")
    print("  • If 3-state agreement is HIGH but 4-state is LOW, the disagreement")
    print("    is mostly DIRECTION (EMA8/21 vs EMA20/50), not strength.")
    print("  • If 3-state agreement is also LOW, the strength models themselves")
    print("    disagree (5-component score vs ADX<20 chop overlay).")
    print("  • PROXY-mode agreement being lower than same-candle agreement")
    print("    quantifies the cost of production classifying crypto by BTC")
    print("    instead of each coin's own candles.")


if __name__ == "__main__":
    main()
