#!/usr/bin/env python3
"""P1c：xs_reversal is_candidate 与"权重标定 down 桶"的重叠对账（只读）。

改造方案 P1c 的问题：ch_weight_calibration_2026-10-24 §4 发现真正有判别力的
long edge 在 **BTC-proxy regime = down** 之后的超跌小币反弹（down 桶 n=31，
fwd24 中位 +10.71% / 胜率 90%；up 桶 −4.38%/44%）。xs_reversal 的 M1 edge
也被描述为"downtrend 超跌反弹"。这两个"down"是不是同一批信号？
  - 若 xs 候选高度落在 BTC down 桶（重叠高）→ 两者是同一现象，xs 可作为该
    edge 的可执行载体，互相印证；
  - 若重叠 <20%（方案 P1c 阈值）→ 它们是两个不同的"反弹"故事，xs 必须
    **独立立论**，不能借权重标定的 down 桶结论背书。

口径（与两边各自报告对齐）：
  * xs 候选：本仓库 xs_episode_stats 在 1h 上重放的独立 episode（72h 去重叠，
    top_pct=85），is_candidate=True；
  * down 桶：每个 xs 信号时点，用**生产同款** market_regime.classify_candles
    在 BTC proxy trailing-100 根已收盘 1h 上分类（up/down/chop/neutral）——
    与 ch_weight_calibration §4 的 regime 定义一致；
  * 双向重叠：xs∩down / xs（xs 有多少在 down 桶）；另给 xs 在四个 regime 的
    分布与各自 72h 净收益；
  * 另做时间交集稳健性：在 BTC 被判为 down 的 bar 上，xs 候选的命中率。

数据：logs/binance_klines_cache/{SYM}_1h.json（离线）。研究工具，不下单。

用法：
  python scripts/p1c_xs_down_bucket_overlap.py
"""
from __future__ import annotations

import argparse
import bisect
import datetime as dt
import json
import os
import statistics
from typing import Optional

from hermes_trader.agents.market_regime import classify_candles
from hermes_trader.models.types import Candle
from scripts.xs_episode_stats import deoverlap, load_candles, replay_coin


def btc_regime_series(btc: list[Candle], win: int = 100) -> dict[int, str]:
    """每个 1h bar 时点（截至该 bar 收盘，trailing win 根）的 BTC regime。"""
    out: dict[int, str] = {}
    closes_t = [c.t for c in btc]
    for i in range(win - 1, len(btc)):
        out[btc[i].t] = classify_candles(btc[i - win + 1:i + 1])
    return out


def regime_at(regime_by_t: dict[int, str], ts_sorted: list[int], t: int) -> str:
    k = bisect.bisect_right(ts_sorted, t) - 1
    return regime_by_t[ts_sorted[k]] if k >= 0 else "unknown"


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--symbols", nargs="+",
                    default=["BTCUSDT", "ETHUSDT", "SOLUSDT"])
    ap.add_argument("--cache-dir", default="logs/binance_klines_cache")
    ap.add_argument("--top-pct", type=float, default=85.0)
    ap.add_argument("--cost-bps", type=float, default=9.26)
    ap.add_argument("--save", default=None)
    args = ap.parse_args(argv)

    btc = load_candles(os.path.join(args.cache_dir, "BTCUSDT_1h.json"), "BTCUSDT")
    regime_by_t = btc_regime_series(btc, 100)
    rtimes = sorted(regime_by_t)

    # 全市场 BTC down bar 占比（基准，用于判断 xs 是否"富集"于 down）。
    base_down = sum(1 for r in regime_by_t.values() if r == "down") / len(regime_by_t)

    episodes = []
    for sym in args.symbols:
        p = os.path.join(args.cache_dir, f"{sym}_1h.json")
        if not os.path.exists(p):
            continue
        cs = load_candles(p, sym)
        for r in deoverlap(replay_coin(cs, args.top_pct, args.cost_bps), 72):
            r["coin"] = sym
            r["regime"] = regime_at(regime_by_t, rtimes, cs[r["i"]].t)
            episodes.append(r)

    n = len(episodes)
    by_reg: dict[str, list[dict]] = {}
    for r in episodes:
        by_reg.setdefault(r["regime"], []).append(r)

    def grp(rs):
        if not rs:
            return {"n": 0}
        v = [x["fwd72_net"] for x in rs]
        days = {dt.datetime.utcfromtimestamp(x["ts"] / 1000).strftime("%Y-%m-%d")
                for x in rs}
        return {
            "n": len(rs), "share_pct": round(100 * len(rs) / n, 1),
            "n_days": len(days),
            "mean72_net_bp": round(100 * statistics.fmean(v), 1),
            "median72_net_bp": round(100 * statistics.median(v), 1),
            "win_pct": round(100 * sum(1 for x in v if x > 0) / len(v), 1),
        }

    dist = {k: grp(v) for k, v in sorted(by_reg.items())}
    n_down = len(by_reg.get("down", []))
    overlap_pct = round(100 * n_down / n, 1) if n else 0.0

    # 富集度：xs 在 down 的占度相对全市场 down 基准的倍数。
    enrich = round((n_down / n) / base_down, 2) if base_down else None

    payload = {
        "top_pct": args.top_pct, "cost_bps": args.cost_bps,
        "n_xs_independent_episodes": n,
        "btc_down_bar_baseline_pct": round(100 * base_down, 1),
        "xs_in_down_pct": overlap_pct,
        "down_enrichment_x_vs_baseline": enrich,
        "p1c_threshold_pct": 20.0,
        "same_phenomenon_if_ge_pct": 20.0,
        "regime_distribution": dist,
        "down_group": grp(by_reg.get("down", [])),
        "non_down_group": grp([r for r in episodes if r["regime"] != "down"]),
    }
    payload["needs_independent_thesis"] = overlap_pct < 20.0

    print("=" * 84)
    print("P1c: xs_reversal is_candidate  vs  weight-calibration BTC-down bucket")
    print(f"xs independent episodes={n}  top_pct={args.top_pct}  "
          f"BTC down bar baseline={payload['btc_down_bar_baseline_pct']}%")
    print("-" * 84)
    print(f"xs in BTC-down bucket : {overlap_pct}%  "
          f"(enrichment vs baseline x{enrich})  P1c threshold=20%")
    print("xs regime distribution (72h net):")
    for k, s in dist.items():
        print(f"  {k:8} {s}")
    print("-" * 84)
    print(f"down     : {payload['down_group']}")
    print(f"non-down : {payload['non_down_group']}")
    print("-" * 84)
    verdict = ("SAME phenomenon (overlap>=20%) — xs can ride the down-bucket edge"
               if not payload["needs_independent_thesis"]
               else "OVERLAP<20% — xs must make its OWN thesis, cannot cite down bucket")
    print("verdict:", verdict)
    print("=" * 84)

    if args.save:
        with open(args.save, "w") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
        print(f"saved -> {args.save}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
