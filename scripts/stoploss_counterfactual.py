#!/usr/bin/env python3
"""Exit-family decomposition + stop counterfactual (2026-11-03, corrected).

The first version pooled floor_breach with max_loss as "hard stop", which mixed
49 near-all-WINNER trailing-floor exits (98% wins) into 61 all-loser true stops.
This version keeps the families separate and answers the real question on the
TRUE stops only:

FAMILIES (from the close fill reason, authoritative realized PnL from gross_pnl):
  * max_loss      — true hard stop (max_loss_pct 1.5 => ~-150bp vs entry)
  * floor_breach  — trailing-protection floor exit (a PROFIT-taking family)
  * timeout       — ignite/stale/hard timeout (comparison)

For each family we report REALIZED bp (gross_pnl_usd/notional) and — only where
meaningful — a forward counterfactual:

  * hold_24h_vs_entry: direction-aware return entry -> 24h-after-EXIT close
  * excess_24h       : hold_24h_vs_entry minus same-window BTC 1h return
                       (removes market beta; longs were structurally short the
                        falling tape over Sep-Oct)
  * paired_delta     : per-trade (hold_24h_vs_entry - realized_vs_entry); the
                       day-block CI of THIS paired series is the test of whether
                       holding 24h would have beaten the actual exit. Marginal
                       medians are reported but not used for inference.

For max_loss, the relevant anchor is its own realized (~-153bp): would holding
have recovered to better than that, on a BTC-adjusted basis? floor_breach is
reported separately (it books winners; "recovery above entry" is tautological
there). Coverage is reported; missing bars exclude a trade only from that leg.
Read-only.
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
FAM = {
    "max_loss": "max_loss",
    "floor_breach": "floor_breach",
    "ignite_timeout": "timeout",
    "stale_flat_timeout": "timeout",
    "hard_timeout": "timeout",
}


def _candles(data_dir: str) -> dict[str, list[tuple[int, list]]]:
    merged: dict[str, dict[int, list]] = {}
    for fn in ("ch_hist_candles.json", ".historical-candles.json"):
        p = os.path.join(data_dir, fn)
        if os.path.exists(p):
            for k, bars in json.load(open(p)).items():
                merged.setdefault(k, {}).update(
                    (int(ts), o) for ts, o in bars.items())
    return {k: sorted(v.items()) for k, v in merged.items()}


def _close_at(bars, t0: int, n_ms: int) -> Optional[float]:
    win = [o[3] for ts, o in bars if t0 < ts <= t0 + n_ms]
    return float(win[-1]) if win else None


def _stat_bp(xs, days, B=4000, seed=13):
    """Values already in bp (no 1e4 scaling)."""
    if len(xs) < 2:
        return {"n": len(xs), "days": len(set(days)), "mean_bp": None,
                "median_bp": None, "ci95_bp": [None, None]}
    byd = defaultdict(list)
    for x, d in zip(xs, days):
        byd[d].append(x)
    blocks = [st.mean(v) for v in byd.values()]
    k = len(blocks)
    rnd = random.Random(seed)
    m = sorted(st.mean(rnd.choice(blocks) for _ in range(k)) for _ in range(B))
    return {"n": len(xs), "days": k,
            "mean_bp": round(st.mean(xs), 2),
            "median_bp": round(st.median(xs), 2),
            "ci95_bp": [round(m[int(.025 * B)], 2), round(m[int(.975 * B)], 2)]}


def _dayblock(xs, days, B=4000, seed=13):
    """Fractional returns -> bp (scales by 1e4)."""
    if len(xs) < 2:
        return {"n": len(xs), "days": len(set(days)), "mean_bp": None,
                "median_bp": None, "ci95_bp": [None, None]}
    byd = defaultdict(list)
    for x, d in zip(xs, days):
        byd[d].append(x)
    blocks = [st.mean(v) for v in byd.values()]
    k = len(blocks)
    rnd = random.Random(seed)
    m = sorted(st.mean(rnd.choice(blocks) for _ in range(k)) for _ in range(B))
    return {"n": len(xs), "days": k,
            "mean_bp": round(1e4 * st.mean(xs), 2),
            "median_bp": round(1e4 * st.median(xs), 2),
            "ci95_bp": [round(1e4 * m[int(.025 * B)], 2),
                       round(1e4 * m[int(.975 * B)], 2)]}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--data-dir", default=_DATA_DIR)
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    book = json.load(open(os.path.join(args.data_dir, ".shadow-book.json")))
    fills = book["accounts"]["taker"]["fills"]
    opens = {f["position_id"]: f for f in fills if f.get("type") == "open"}
    closes = [f for f in fills if f.get("type") == "close"]
    candles = _candles(args.data_dir)
    btc = candles.get("BTC|1h", [])

    realized = {"max_loss": ([], []), "floor_breach": ([], []),
                "timeout": ([], [])}
    hold24 = {f: ([], []) for f in set(FAM.values())}
    excess = {f: ([], []) for f in set(FAM.values())}
    paired = {f: ([], []) for f in set(FAM.values())}
    btc_missing = 0

    for cl in closes:
        reason = (cl.get("reason") or "").split("(")[0].strip()
        fam = FAM.get(reason)
        if fam is None:
            continue
        op = opens.get(cl["position_id"])
        notional = float(cl.get("notional_usd") or 0)
        gp = float(cl.get("gross_pnl_usd") or 0)
        exit_day = int(cl["ts"]) // 86_400_000
        if notional:
            realized[fam][0].append(1e4 * gp / notional)
            realized[fam][1].append(exit_day)

        # forward counterfactual needs open + candles
        if not op:
            continue
        coin, side = op["coin"], op.get("side", "long")
        sgn = -1.0 if side == "short" else 1.0
        entry, exit_t = float(op["price"]), int(cl["ts"])
        day = exit_t // 86_400_000
        bars = candles.get(f"{coin}|1h")
        if not bars:
            continue
        c24 = _close_at(bars, exit_t, 24 * H)
        if c24 is None:
            continue
        hold = sgn * (c24 / entry - 1.0)
        btc24 = _close_at(btc, exit_t, 24 * H)
        # BTC benchmark must be direction-aware too (short earns the inverse).
        btc_now = next((o[3] for ts, o in reversed(btc) if ts <= exit_t), None)
        if btc24 is None or btc_now is None:
            btc_missing += 1
            bench = 0.0
        else:
            bench = sgn * (btc24 / btc_now - 1.0)
        realz_vs_entry = sgn * (float(cl["price"]) / entry - 1.0)

        hold24[fam][0].append(hold); hold24[fam][1].append(day)
        excess[fam][0].append(hold - bench); excess[fam][1].append(day)
        paired[fam][0].append(hold - realz_vs_entry); paired[fam][1].append(day)

    def block(fam):
        return {
            "realized_bp": _stat_bp(*realized[fam]),
            "hold_24h_vs_entry_bp": _dayblock(*hold24[fam]),
            "excess_24h_minus_btc_bp": _dayblock(*excess[fam]),
            "paired_delta_hold24h_minus_realized_bp": _dayblock(*paired[fam]),
            "win_realized_pct": round(
                100 * sum(1 for x in realized[fam][0] if x > 0) /
                max(1, len(realized[fam][0])), 1),
        }

    report = {
        "meta": {
            "groups_kept_separate": True,
            "max_loss_only": "true hard stops; floor_breach is a profit-taking "
                             "trailing floor, not a loss stop",
            "realized_source": "close gross_pnl_usd/notional_usd",
            "inference": "day-block bootstrap CI of the PAIRED delta "
                         "(hold24h - realized), BTC-adjusted excess; marginal "
                         "medians are descriptive only",
            "benchmark": "same-window BTC 1h return, direction-aware",
            "btc_benchmark_missing_trades": btc_missing,
        },
        "max_loss": block("max_loss"),
        "floor_breach_trailing_exit": block("floor_breach"),
        "timeout_comparison": block("timeout"),
    }
    text = json.dumps(report, ensure_ascii=False, indent=2)
    print(text)
    if args.out:
        open(args.out, "w").write(text + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
