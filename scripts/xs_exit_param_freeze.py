#!/usr/bin/env python3
"""S2 promote 未决项③：xs 臂退出参数用其**自身信号**离线验证后冻结（只读）。

诊断 G2 显示出场回吐吃掉峰值浮盈 88–99%，而 xs 是 72h 均值回归持仓，沿用 5m
突破策略的 scalp 退出（1.5% 止损）未必合适。本脚本在**通过生产 panic gate 的
xs 信号**上（top_pct=85，ext<=-8 ∧ RSI>=20 ∧ atr_pctile>=0.6）模拟真实出场：

  * 入场：信号根的下一根 1h open（PIT，无前视）；
  * 止损：持仓期任一根 low <= entry*(1-st) 即按止损价成交（保守，不假设影线恢复）；
  * 时间止损：持有至 H 小时按该根 close 平仓；二者取先触发者。
  * 成本：往返 9.26bp（与 episode 统计同口径）。

对比一组 stop × H 网格 + close-only（无止损、仅 H 时间止损）基线，按 72h 去重
叠、跨币按 UTC 信号日做日块 t/CI。目标是选出在 gated 子集上净 edge 不被出场
吞噬、且不是孤立尖峰的 (stop, H)，作为 S3 之前冻结的退出参数。历史 1h 数据。

用法：
  python scripts/xs_exit_param_freeze.py
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
from typing import Optional

from scripts.xs_episode_stats import (
    load_candles,
    tstat,
)
from scripts.xs_vol_gate_calibration import episodes_with_atr_pctile

EXT_MAX = -8.0
RSI_MIN = 20.0
ATR_Q = 0.6
COST_BPS = 9.26


def simulate_exit(candles, i, stop_pct, horizon_h):
    """信号在 bar i；次根 open 入场。返回 (净收益分数, exit_kind, hold_bars)。

    止损在 j>=i+1 的当根用 low 判定并按 stop 价成交；否则 H 根后 close 平仓。
    """
    e = i + 1
    if e >= len(candles):
        return None
    entry = float(candles[e].o)
    if entry <= 0:
        return None
    stop_px = entry * (1.0 - stop_pct)
    end = min(e + horizon_h, len(candles) - 1)
    for j in range(e, end + 1):
        if float(candles[j].l) <= stop_px:
            gross = (stop_px - entry) / entry
            return gross - COST_BPS / 1e4, "stop", j - e
    exit_px = float(candles[end].c)
    gross = (exit_px - entry) / entry
    return gross - COST_BPS / 1e4, "time", end - e


def day_block(rows, key, boot, seed):
    import random
    by_day = {}
    for r in rows:
        by_day.setdefault(r["day"], []).append(r[key])
    days = [statistics.fmean(v) for v in by_day.values()]
    t = tstat(days) if len(days) >= 2 else None
    ci = None
    if len(days) >= 2:
        rng = random.Random(seed)
        nd = len(days)
        sims = sorted(statistics.fmean(days[rng.randrange(nd)] for _ in range(nd))
                      for _ in range(boot))
        ci = [round(sims[int(boot * 0.025)] * 100, 1),
              round(sims[int(boot * 0.975)] * 100, 1)]
    return t, ci, len(days)


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--symbols", nargs="+",
                    default=["BTCUSDT", "ETHUSDT", "SOLUSDT"])
    ap.add_argument("--cache-dir", default="logs/binance_klines_cache")
    ap.add_argument("--top-pct", type=float, default=85.0)
    ap.add_argument("--boot", type=int, default=5000)
    ap.add_argument("--seed", type=int, default=20261027)
    ap.add_argument("--save", default=None)
    args = ap.parse_args(argv)

    # gated 信号（含 bar 索引 i 与 atr_pctile）
    gated = []
    candles_by_coin = {}
    for sym in args.symbols:
        p = os.path.join(args.cache_dir, f"{sym}_1h.json")
        if not os.path.exists(p):
            continue
        coin = sym.split("USDT")[0]
        candles_by_coin[coin] = load_candles(p, sym)
        rows = episodes_with_atr_pctile(p, args.top_pct, COST_BPS)
        for r in rows:
            if (r["ext_pct"] <= EXT_MAX and r["rsi"] >= RSI_MIN
                    and r["atr_pctile"] >= ATR_Q):
                r["coin"] = coin
                gated.append(r)

    variants = [("close_only_72h", None, 72)]
    for h in (48, 72, 96):
        for s in (0.010, 0.015, 0.020, 0.030):
            variants.append((f"stop{s*100:.1f}%_H{h}", s, h))

    report = {}
    print("=" * 104)
    print(f"xs exit-param freeze on GATED signals | n={len(gated)} "
          f"(top{args.top_pct}, ext<={EXT_MAX}, rsi>={RSI_MIN}, atrP>={ATR_Q})")
    print("-" * 104)
    print(f"{'variant':18}{'n':>4}{'days':>6}{'net_bp':>9}{'WR%':>7}"
          f"{'t':>7}{'CI_bp':>17}{'stop%':>7}{'avgHold':>8}")
    for name, stop, h in variants:
        sims = []
        stop_hits = 0
        holds = []
        for r in gated:
            candles = candles_by_coin[r["coin"]]
            if stop is None:
                # close-only：仅 H 时间止损，无止损判定
                res = simulate_exit(candles, r["i"], 0.0, h)
                if res is None:
                    continue
                net, kind, hb = res
                # stop_pct=0 会立即触发，故 close-only 单独算：直接 H 根收盘
                e = r["i"] + 1
                if e >= len(candles):
                    continue
                end = min(e + h, len(candles) - 1)
                net = (float(candles[end].c) - float(candles[e].o)) / \
                    float(candles[e].o) - COST_BPS / 1e4
                kind, hb = "time", end - e
            else:
                res = simulate_exit(candles, r["i"], stop, h)
                if res is None:
                    continue
                net, kind, hb = res
            if kind == "stop":
                stop_hits += 1
            holds.append(hb)
            sims.append({"day": r["day"], "net": net})
        if not sims:
            continue
        vals = [x["net"] for x in sims]
        t, ci, ndays = day_block(sims, "net", args.boot, args.seed)
        row = {
            "n": len(sims), "days": ndays,
            "mean_net_bp": round(statistics.fmean(vals) * 100, 1),
            "wr_pct": round(100 * sum(v > 0 for v in vals) / len(vals), 1),
            "day_t": t, "day_ci_bp": ci,
            "stop_hit_pct": round(100 * stop_hits / len(sims), 1),
            "avg_hold_bars": round(statistics.fmean(holds), 1),
        }
        report[name] = row
        print(f"{name:18}{row['n']:>4}{ndays:>6}{row['mean_net_bp']:>9}"
              f"{row['wr_pct']:>7}{str(t):>7}{str(ci):>17}"
              f"{row['stop_hit_pct']:>7}{row['avg_hold_bars']:>8}")
    print("=" * 104)
    if args.save:
        json.dump({"params": {"top_pct": args.top_pct, "ext_max": EXT_MAX,
                              "rsi_min": RSI_MIN, "atr_pctile_min": ATR_Q,
                              "cost_bps": COST_BPS},
                   "n_gated": len(gated), "variants": report},
                  open(args.save, "w"), ensure_ascii=False, indent=2)
        print(f"saved -> {args.save}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
