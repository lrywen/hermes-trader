#!/usr/bin/env python3
"""Entry-timing decomposition (2026-11-03): is the edge lost at ENTRY or HOLD?

The main-strategy WFA verdict is fail and gross PnL sits below the real cost
line (gross +5.92bp across 158 taker closes; net at the true 9.26bp cost is
-3.34bp; top-1 trade removal flips gross negative). Before any new arm, exit
tuning, or backtester calibration, locate WHERE the PnL is made:

  * entry->1h  : fill price to the first 1h bar close AFTER entry (timing)
  * 1h->4h     : first 1h close to first 4h close (early hold)
  * 4h->exit   : first 4h close to the actual taker close fill (later hold/exit)

All legs are direction-aware (sgn = -1 for short) so each segment is PnL in the
position's direction. Bars come from the merged on-disk candle cache
(ch_hist_candles.json + .historical-candles.json). 5m/15m fine grains are NOT
recoverable for the September entries (HL retains ~17d of 5m; local 5m cache
covers only 24/158), so 1h is the finest reliable grain and that limitation is
reported, never silently filled with a different grain.

Coverage is reported per leg; positions lacking a bar for a leg are excluded
from that leg only (never dropped wholesale, never imputed). Read-only.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import statistics as st
from collections import defaultdict
from typing import Optional

_DATA_DIR = os.environ.get("HERMES_DATA_DIR", "/data")
H = 3_600_000


def _load_candles(data_dir: str):
    merged: dict[str, dict[int, list]] = {}
    for fn in ("ch_hist_candles.json", ".historical-candles.json"):
        p = os.path.join(data_dir, fn)
        if not os.path.exists(p):
            continue
        d = json.load(open(p))
        for k, bars in d.items():
            t = merged.setdefault(k, {})
            for ts, ohlcv in bars.items():
                t.setdefault(int(ts), ohlcv)  # do not let one cache overwrite
    # pre-sort
    return {k: sorted(v.items()) for k, v in merged.items()}


def _first_close_after(bars: list[tuple[int, list]], t: int) -> Optional[tuple[int, float]]:
    for ts, o in bars:
        # bar key is the bar OPEN ms; close is known at open+interval.
        if ts + H > t and ts <= t + 12 * H:  # first 1h bar straddling/after entry
            return ts, float(o[3])
    return None


def _first_close_after_tf(bars: list[tuple[int, list]], t: int, tf_ms: int):
    for ts, o in bars:
        if ts + tf_ms > t and ts <= t + 6 * tf_ms:
            return ts, float(o[3])
    return None


def _dayblock(xs: list[float], days: list[int], B: int = 4000, seed: int = 5):
    if len(xs) < 2:
        return {"n": len(xs), "days": len(set(days)), "mean_bp": None,
                "median_bp": None, "ci95_bp": [None, None]}
    byd = defaultdict(list)
    for x, d in zip(xs, days):
        byd[d].append(x)
    blocks = [st.mean(v) for v in byd.values()]
    k = len(blocks)
    rnd = random.Random(seed)
    means = sorted(st.mean(rnd.choice(blocks) for _ in range(k)) for _ in range(B))
    return {"n": len(xs), "days": k,
            "mean_bp": round(1e4 * st.mean(xs), 2),
            "median_bp": round(1e4 * st.median(xs), 2),
            "ci95_bp": [round(1e4 * means[int(.025 * B)], 2),
                        round(1e4 * means[int(.975 * B)], 2)]}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--data-dir", default=_DATA_DIR)
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    book = json.load(open(os.path.join(args.data_dir, ".shadow-book.json")))
    fills = book["accounts"]["taker"]["fills"]
    closes = {f["position_id"]: f for f in fills if f.get("type") == "close"}
    opens = [f for f in fills if f.get("type") == "open"]

    candles = _load_candles(args.data_dir)

    seg = {"entry_1h": ([], []), "h1_h4": ([], []), "h4_exit": ([], []),
           "entry_exit_actual": ([], [])}
    cov = {"opens": len(opens), "entry_1h": 0, "h1_h4": 0, "h4_exit": 0,
           "matched_close": 0}
    per_coin = defaultdict(list)

    for o in opens:
        coin, side = o["coin"], o.get("side", "long")
        sgn = -1.0 if side == "short" else 1.0
        t, entry = int(o["ts"]), float(o["price"])
        day = t // 86_400_000
        c1s = candles.get(f"{coin}|1h")
        c4s = candles.get(f"{coin}|4h")
        if not c1s:
            continue
        b1 = _first_close_after_tf(c1s, t, H)
        if not b1:
            continue
        cov["entry_1h"] += 1
        r_entry_1h = sgn * (b1[1] / entry - 1.0)
        seg["entry_1h"][0].append(r_entry_1h); seg["entry_1h"][1].append(day)

        cl = closes.get(o["position_id"])
        if cl:
            cov["matched_close"] += 1
            exit_px = float(cl["price"])
            r_entry_exit = sgn * (exit_px / entry - 1.0)
            seg["entry_exit_actual"][0].append(r_entry_exit)
            seg["entry_exit_actual"][1].append(day)
            per_coin[coin].append(r_entry_exit)

        if c4s:
            b4 = _first_close_after_tf(c4s, t, 4 * H)
            if b4:
                cov["h1_h4"] += 1
                seg["h1_h4"][0].append(sgn * (b4[1] / b1[1] - 1.0))
                seg["h1_h4"][1].append(day)
                if cl:
                    cov["h4_exit"] += 1
                    seg["h4_exit"][0].append(sgn * (float(cl["price"]) / b4[1] - 1.0))
                    seg["h4_exit"][1].append(day)

    # coin concentration on the actual entry->exit gross
    allr = seg["entry_exit_actual"][0]
    coin_means = sorted(((c, 1e4 * st.mean(v), len(v)) for c, v in per_coin.items()),
                        key=lambda x: x[1], reverse=True)

    report = {
        "meta": {
            "grain": "1h finest reliable (5m unrecoverable for Sept entries; "
                     "local 5m covers 24/158)",
            "coverage": cov,
            "direction": "sgn=-1 for short; each leg is PnL in position direction",
            "interpretation": "negative entry_1h mean => edge lost at entry "
                              "(timing); positive entry_1h but negative h1_h4/"
                              "h4_exit => hold/exit problem.",
        },
        "segments_bp": {k: _dayblock(*v) for k, v in seg.items()},
        "top_coins_entry_exit_gross_bp": [
            {"coin": c, "mean_bp": round(m, 1), "n": n} for c, m, n in coin_means[:8]],
        "bottom_coins_entry_exit_gross_bp": [
            {"coin": c, "mean_bp": round(m, 1), "n": n} for c, m, n in coin_means[-5:]],
    }
    text = json.dumps(report, ensure_ascii=False, indent=2)
    print(text)
    if args.out:
        open(args.out, "w").write(text + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
