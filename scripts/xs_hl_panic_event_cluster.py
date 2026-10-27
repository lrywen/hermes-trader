#!/usr/bin/env python3
"""HL 跨源确认的"有效样本"去相关复核（只读、全历史 81 币）。

把同一波市场级恐慌在多币上的触发合并为 **1 个独立事件**，避免把一次系统性
砸盘当成几十笔独立证据。三种口径并列：

  A. naive 按笔 iid：每个 episode 一个观测（449 笔）——严重虚高，因为同日 38 币
     共享同一市场 beta，且 72h 持仓在相邻日重叠。
  B. 日等权：按 UTC 信号日把多币平均成 1 个日收益（80 日）——去掉同日横截面
     相关，但相邻日仍可能属于同一波 72h 恐慌。
  C. 恐慌事件级（最严格、最接近真实独立样本）：
       - 第一步按 UTC 日聚合（同日多币等权 = 1 个日收益）；
       - 第二步把"间隔 <= gap 天"的相邻活跃日合并成一个恐慌事件，事件收益取
         其中各活跃日收益的等权平均（保守，不重复计 72h 重叠窗）；
       - gap 默认 3 天（72h 持仓窗），另报 gap=1/2/5 敏感性。

数据：logs/hl_klines_cache 全部 81 币 1h（2026-02-24→09-21）。信号/门/成本与
Binance 发现阶段完全一致（top85 ∧ ext<=-8 ∧ RSI>=20 ∧ 币内 ATR 分位>=0.6，
9.26bp 往返，次根 open 入场，72h 收盘），不在 HL 上重新调参。

用法：
  python scripts/xs_hl_panic_event_cluster.py
  python scripts/xs_hl_panic_event_cluster.py --save analysis/x.json
"""
from __future__ import annotations

import argparse
import bisect
import datetime as dt
import glob
import json
import math
import os
import random
import statistics
from typing import Optional

from hermes_trader.models.types import Candle
from scripts.xs_episode_stats import HOLD_HOURS, deoverlap, replay_coin
from scripts.xs_regime_conditional import _atr24_pct

HL_CACHE = "logs/hl_klines_cache"
EXT_MAX, RSI_MIN, ATR_Q, COST = -8.0, 20.0, 0.6, 9.26
PCTILE_WIN = 2160


def load_hl(coin: str) -> list[Candle]:
    raw = json.load(open(os.path.join(HL_CACHE, f"{coin}_1h.json")))
    out = [Candle(t=float(r["t"]), o=float(r["o"]), h=float(r["h"]),
                  l=float(r["l"]), c=float(r["c"]), v=float(r["v"]),
                  coin=coin) for r in raw]
    out.sort(key=lambda c: c.t)
    return out


def tstat(x: list[float]) -> Optional[float]:
    n = len(x)
    if n < 2:
        return None
    sd = statistics.stdev(x)
    return statistics.fmean(x) / (sd / math.sqrt(n)) if sd > 0 else None


def boot_ci(x: list[float], b: int, seed: int) -> Optional[list[float]]:
    n = len(x)
    if n < 2:
        return None
    rng = random.Random(seed)
    sim = sorted(statistics.fmean(x[rng.randrange(n)] for _ in range(n))
                 for _ in range(b))
    return [round(sim[int(b * 0.025)] * 100, 1),
            round(sim[int(b * 0.975)] * 100, 1)]


def block_stats(x: list[float], boot: int, seed: int) -> dict:
    if not x:
        return {"n": 0}
    return {
        "n": len(x),
        "mean_bp": round(statistics.fmean(x) * 100, 1),
        "wr_pct": round(100 * sum(v > 0 for v in x) / len(x), 1),
        "t": round(tstat(x), 2) if tstat(x) is not None else None,
        "ci_bp": boot_ci(x, boot, seed),
    }


def cluster_days(day_vals: dict[str, float], gap_days: int) -> list[float]:
    """把相邻（间隔<=gap_days）活跃日合并为一个恐慌事件，事件内日收益等权。"""
    days = sorted(day_vals)
    events: list[float] = []
    cur: list[float] = []
    last: Optional[dt.date] = None
    for d in days:
        dd = dt.date.fromisoformat(d)
        if last is not None and (dd - last).days > gap_days:
            events.append(statistics.fmean(cur))
            cur = []
        cur.append(day_vals[d])
        last = dd
    if cur:
        events.append(statistics.fmean(cur))
    return events


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--top-pct", type=float, default=85.0)
    ap.add_argument("--boot", type=int, default=5000)
    ap.add_argument("--seed", type=int, default=20261027)
    ap.add_argument("--save", default=None)
    args = ap.parse_args(argv)

    naive: list[float] = []
    day_vals: dict[str, list[float]] = {}
    coins = 0
    for f in sorted(glob.glob(os.path.join(HL_CACHE, "*_1h.json"))):
        coin = os.path.basename(f).split("_1h")[0]
        try:
            cs = load_hl(coin)
        except (json.JSONDecodeError, KeyError, ValueError):
            continue
        eps = deoverlap(replay_coin(cs, args.top_pct, COST), HOLD_HOURS)
        n = len(cs)
        atr = [_atr24_pct(cs, i, 24) for i in range(n)]
        hit = False
        for r in eps:
            i = r["i"]
            a = atr[i]
            if not math.isfinite(a):
                continue
            win = sorted(v for v in atr[max(0, i - PCTILE_WIN + 1):i + 1]
                         if math.isfinite(v))
            q = bisect.bisect_right(win, a) / len(win) if win else float("nan")
            if (r["ext_pct"] <= EXT_MAX and r["rsi"] >= RSI_MIN
                    and math.isfinite(q) and q >= ATR_Q):
                net = float(r["fwd72_net"])
                day = dt.datetime.utcfromtimestamp(r["ts"] / 1000).strftime("%Y-%m-%d")
                naive.append(net)
                day_vals.setdefault(day, []).append(net)
                hit = True
        coins += int(hit)

    day_means = {d: statistics.fmean(v) for d, v in day_vals.items()}
    days = list(day_means.values())

    # 逐月（日等权）
    bymonth: dict[str, list[float]] = {}
    for d, v in day_means.items():
        bymonth.setdefault(d[:7], []).append(v)
    monthly = {m: block_stats(v, args.boot, args.seed)
               for m, v in sorted(bymonth.items())}

    views = {
        "A_naive_per_episode_iid": block_stats(naive, args.boot, args.seed),
        "B_day_equal_weight": block_stats(days, args.boot, args.seed),
    }
    for gap in (1, 2, 3, 5):
        ev = cluster_days(day_means, gap)
        views[f"C_panic_event_gap{gap}d"] = block_stats(ev, args.boot, args.seed)

    breadth = [len(v) for v in day_vals.values()]
    out = {
        "data": {"source": HL_CACHE, "coins_with_gated_hit": coins,
                 "window": "2026-02-24..2026-09-21",
                 "gate": {"top_pct": args.top_pct, "ext_max": EXT_MAX,
                          "rsi_min": RSI_MIN, "atr_pctile_min": ATR_Q,
                          "cost_bps": COST}},
        "breadth": {"independent_days": len(days), "episodes": len(naive),
                    "coins_per_day_median": statistics.median(breadth),
                    "coins_per_day_max": max(breadth),
                    "single_coin_days": sum(b == 1 for b in breadth),
                    "multi3_days": sum(b >= 3 for b in breadth)},
        "effective_sample_views": views,
        "monthly_day_equal_weight": monthly,
    }
    print(json.dumps(out, ensure_ascii=False, indent=2))
    if args.save:
        json.dump(out, open(args.save, "w"), ensure_ascii=False, indent=2)
        print(f"saved -> {args.save}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
