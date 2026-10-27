#!/usr/bin/env python3
"""S2 promote 未决项②：xs panic gate 波动腿的跨币量纲校准（只读、历史数据）。

固定 atr24>=1.0% 对不同波动量级的币不公平（SOL 常态波动就高于 BTC），可能
系统性多触发小币。本脚本在历史 1h 上比较两类波动门：

  A. 固定阈值：atr24_pct >= c（c ∈ {0.8,1.0,1.2}）。
  B. 币内滚动分位：atr24_pct 处于该币**自身** trailing WIN(=2160, 90d) ATR
     分布的 top (1-q)（q ∈ {0.50,0.60,0.70}），完全零前视、自我归一化，
     与 ext 的百分位做法一致。

三种门都与共同的深回撤/非接刀条件（ext<=-8 且 rsi>=20）合取，只隔离"波动腿"
这一条的差异。对每个方案报告：分币触发数与占比（量纲偏斜）、门内独立信号日、
72h 净收益均值/WR/日块 t/CI、逐年表现。数据用 logs/binance_klines_cache。

用法：
  python scripts/xs_vol_gate_calibration.py
"""
from __future__ import annotations

import argparse
import bisect
import datetime as dt
import json
import math
import os
import random
import statistics
from typing import Optional

from scripts.xs_episode_stats import (
    HOLD_HOURS,
    deoverlap,
    load_candles,
    replay_coin,
    tstat,
)
from scripts.xs_regime_conditional import _atr24_pct

PCTILE_WIN = 2160
LOOKBACK_BARS = 72
EXT_MAX = -8.0
RSI_MIN = 20.0


def atr_series(candles, win: int = 24) -> list[float]:
    n = len(candles)
    out = [float("nan")] * n
    for i in range(n):
        out[i] = _atr24_pct(candles, i, win)
    return out


def episodes_with_atr_pctile(path: str, top_pct: float,
                             cost_bps: float) -> list[dict]:
    """重放 xs 独立 episode，并附 atr24 与币内 trailing 90d ATR 分位。"""
    coin = os.path.basename(path).split("_")[0]
    candles = load_candles(path, coin)
    base = deoverlap(replay_coin(candles, top_pct, cost_bps), HOLD_HOURS)
    atr = atr_series(candles)
    # 维护与决策点一致的 trailing ATR 分布（零前视：只含 i 及之前）。
    # 为每个 episode 独立取切片（episode 稀疏，开销可接受）。
    for r in base:
        i = r["i"]
        r["coin"] = coin
        r["year"] = dt.datetime.utcfromtimestamp(r["ts"] / 1000).year
        r["day"] = dt.datetime.utcfromtimestamp(r["ts"] / 1000).strftime("%Y-%m-%d")
        r["atr24_pct"] = round(atr[i], 4) if math.isfinite(atr[i]) else float("nan")
        lo = max(0, i - PCTILE_WIN + 1)
        win = sorted(v for v in atr[lo:i + 1] if math.isfinite(v))
        r["atr_pctile"] = (bisect.bisect_right(win, atr[i]) / len(win)
                           if win else float("nan"))
    return base


def day_stats(rows: list[dict], boot: int, seed: int) -> dict:
    if not rows:
        return {"n": 0, "days": 0}
    vals = [r["fwd72_net"] for r in rows]
    by_day: dict[str, list[float]] = {}
    for r in rows:
        by_day.setdefault(r["day"], []).append(r["fwd72_net"])
    days = [statistics.fmean(v) for v in by_day.values()]
    rng = random.Random(seed)
    nd = len(days)
    t = round(tstat(days), 2) if nd >= 2 else None
    if nd >= 2:
        sims = sorted(statistics.fmean(days[rng.randrange(nd)] for _ in range(nd))
                      for _ in range(boot))
        ci = [round(sims[int(boot * 0.025)] * 100, 1),
              round(sims[int(boot * 0.975)] * 100, 1)]
    else:
        ci = None
    return {
        "n": len(rows), "days": nd,
        "mean_bp": round(statistics.fmean(vals) * 100, 1),
        "wr_pct": round(100 * sum(1 for x in vals if x > 0) / len(vals), 1),
        "day_t": t,
        "day_ci_bp": ci,
    }


def per_coin_share(rows: list[dict], coins: list[str]) -> dict:
    tot = len(rows)
    return {c: (round(100 * sum(1 for r in rows if r["coin"] == c) / tot, 1)
                if tot else 0.0) for c in coins}


def yearly(rows: list[dict]) -> dict:
    out = {}
    for y in sorted({r["year"] for r in rows}):
        v = [r["fwd72_net"] for r in rows if r["year"] == y]
        out[y] = {"n": len(v),
                  "mean_bp": round(100 * statistics.fmean(v), 1) if v else None,
                  "wr_pct": round(100 * sum(x > 0 for x in v) / len(v), 1) if v else None}
    return out


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--symbols", nargs="+",
                    default=["BTCUSDT", "ETHUSDT", "SOLUSDT"])
    ap.add_argument("--cache-dir", default="logs/binance_klines_cache")
    ap.add_argument("--top-pct", type=float, default=85.0)
    ap.add_argument("--cost-bps", type=float, default=9.26)
    ap.add_argument("--boot", type=int, default=5000)
    ap.add_argument("--seed", type=int, default=20261027)
    ap.add_argument("--save", default=None)
    args = ap.parse_args(argv)

    coins, all_rows = [], []
    for sym in args.symbols:
        p = os.path.join(args.cache_dir, f"{sym}_1h.json")
        if os.path.exists(p):
            coins.append(sym)
            all_rows.extend(episodes_with_atr_pctile(p, args.top_pct, args.cost_bps))

    # 共同前置：is_candidate（已是 downtrend+RSI[15,35)）∧ ext 深回撤 ∧ RSI>=20。
    deep = [r for r in all_rows if r["ext_pct"] <= EXT_MAX and r["rsi"] >= RSI_MIN]

    variants = {}
    # A. 固定 ATR 阈值
    for c in (0.8, 1.0, 1.2):
        sel = [r for r in deep if r["atr24_pct"] >= c]
        variants[f"fixed_atr>={c}%"] = sel
    # B. 币内滚动 ATR 分位（top 1-q）
    for q in (0.50, 0.60, 0.70):
        sel = [r for r in deep if r["atr_pctile"] >= q]
        variants[f"atr_pctile>={q}"] = sel

    # 参考：完全不加波动腿（只 ext+rsi），看波动腿贡献与触发基数。
    variants["__no_vol_leg__"] = deep

    report = {}
    print("=" * 92)
    print(f"xs vol-leg calibration | common: ext<={EXT_MAX} & rsi>={RSI_MIN} "
          f"| episodes={len(all_rows)} deep={len(deep)} | coin shares of DEEP: "
          f"{per_coin_share(deep, coins)}")
    print("-" * 92)
    print(f"{'variant':18}{'n':>5}{'days':>6}{'mean_bp':>9}{'WR%':>7}"
          f"{'day_t':>7}  {'dayCI_bp':>16}  per-coin share % / y2026(mean,n)")
    for name, sel in variants.items():
        s = day_stats(sel, args.boot, args.seed)
        share = per_coin_share(sel, coins)
        y26 = [r for r in sel if r["year"] == 2026]
        y26m = (round(100 * statistics.fmean(
            [r["fwd72_net"] for r in y26]), 1) if y26 else None)
        report[name] = {"stats": s, "coin_share_pct": share,
                        "yearly": yearly(sel), "y2026_n": len(y26),
                        "y2026_mean_bp": y26m}
        ci = s.get("day_ci_bp")
        print(f"{name:18}{s.get('n',0):>5}{s.get('days',0):>6}"
              f"{str(s.get('mean_bp')):>9}{str(s.get('wr_pct')):>7}"
              f"{str(s.get('day_t')):>7}  {str(ci):>16}  "
              f"{share}  ({y26m},{len(y26)})")
    print("=" * 92)

    # 量纲偏斜诊断：固定门 vs 分位门，各币占比的极差（max-min）。
    skew = {}
    for name in ("fixed_atr>=1.0%", "atr_pctile>=0.6"):
        sh = report[name]["coin_share_pct"]
        skew[name] = round(max(sh.values()) - min(sh.values()), 1)
    print("coin-share spread (max-min pct points; lower=fairer across coins):",
          skew)

    if args.save:
        with open(args.save, "w") as fh:
            json.dump({"params": {"ext_max": EXT_MAX, "rsi_min": RSI_MIN,
                                  "atr_pctile_win": PCTILE_WIN},
                       "deep_n": len(deep), "variants": report,
                       "coin_share_spread": skew}, fh, ensure_ascii=False, indent=2)
        print(f"saved -> {args.save}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
