#!/usr/bin/env python3
"""S2 续：xs_reversal 的 regime 条件面板（只读，离线，无下单）。

S2 主结论：xs 超跌反弹 72h edge 在 2023–2025 稳定为正、2026 消失，跨币独立日
面板 t=1.93 / CI 含零。本脚本回答决策性问题：**这个 edge 能否用一个信号时点
可知的 regime/风险变量稳定地分离出来？**
  - 若存在一个先验可知、样本充足、跨年份（尤其 2026）都为正且日块 CI>0 的
    子集 → 给该臂加 regime 门续命，进 P1b/P1c；
  - 若任何分层在 2026 都不恢复，或仅在历史年份显著 → 正式归档该臂。

每个独立 episode（72h 去重叠）在信号时点打标（全部只用 t0 及之前数据，零
前视）：
  * vol24_pct   : 近 24h 收益率绝对值均值（bar-to-bar）年化前的日度实现波动代理；
  * atr24_pct   : 近 24 根 1h 的 TR/close 均值（百分比，ATR 波动代理）；
  * ext_pct     : 距 72h 高点回撤（信号本身已有，越深越超卖）；
  * rsi14       : 信号 RSI；
  * btc_trend24 : 信号时点 BTC 近 24h 收益（市场 beta/风险偏好）；
  * year        : 年份（用于检查分层是否跨年稳健）。

分层方式：对每个连续变量按全样本三分位（用各自币+全历史的分位，避免未来
函数——分位阈值是研究标定，上线需冻结，但此处只做可行性判定）切组，比较
各组 72h 净收益的独立日块均值/t/CI；并单列 2026 子样本。

用法：
  python scripts/xs_regime_conditional.py
  python scripts/xs_regime_conditional.py --top-pct 85 --cost-bps 9.26
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

from hermes_trader.models.types import Candle
from scripts.xs_episode_stats import (
    HOLD_HOURS,
    deoverlap,
    load_candles,
    replay_coin,
    tstat,
)

LOOKBACK_BARS = 72
PCTILE_WIN = 2160


def _atr24_pct(candles: list[Candle], i: int, win: int = 24) -> float:
    lo = max(1, i - win + 1)
    trs = []
    for j in range(lo, i + 1):
        c_prev = candles[j - 1].c
        tr = max(candles[j].h - candles[j].l,
                 abs(candles[j].h - c_prev),
                 abs(candles[j].l - c_prev))
        if candles[j].c > 0:
            trs.append(tr / candles[j].c * 100.0)
    return statistics.fmean(trs) if trs else float("nan")


def _vol24_pct(candles: list[Candle], i: int, win: int = 24) -> float:
    lo = max(1, i - win + 1)
    rets = [abs(candles[j].c / candles[j - 1].c - 1.0) * 100.0
            for j in range(lo, i + 1) if candles[j - 1].c > 0]
    return statistics.fmean(rets) if rets else float("nan")


def episodes_with_features(path: str, top_pct: float,
                           cost_bps: float,
                           btc_by_t: Optional[dict] = None) -> list[dict]:
    coin = os.path.basename(path).split("_")[0]
    candles = load_candles(path, coin)
    ep = deoverlap(replay_coin(candles, top_pct, cost_bps), HOLD_HOURS)
    closes = [c.c for c in candles]
    for r in ep:
        i = r["i"]
        r["coin"] = coin
        r["year"] = dt.datetime.utcfromtimestamp(r["ts"] / 1000).year
        r["atr24_pct"] = round(_atr24_pct(candles, i), 4)
        r["vol24_pct"] = round(_vol24_pct(candles, i), 4)
        r["day"] = dt.datetime.utcfromtimestamp(r["ts"] / 1000).strftime("%Y-%m-%d")
        if btc_by_t is not None:
            # 最近的已收盘 BTC 1h bar 的 24h 收益（市场 beta）。
            t = candles[i].t
            k = bisect.bisect_right(list(btc_by_t["ts"]), t) - 1
            ts_arr, c_arr = btc_by_t["ts"], btc_by_t["c"]
            if k >= 24 and c_arr[k - 24] > 0:
                r["btc_ret24_pct"] = round(
                    (c_arr[k] / c_arr[k - 24] - 1.0) * 100.0, 3)
            else:
                r["btc_ret24_pct"] = float("nan")
    return ep


def _day_block_ci(rows: list[dict], field: str, boot: int,
                  seed: int) -> tuple[float, float]:
    by_day: dict[str, list[float]] = {}
    for r in rows:
        if math.isfinite(r.get(field, float("nan"))):
            by_day.setdefault(r["day"], []).append(r["fwd72_net"])
    days = [statistics.fmean(v) for v in by_day.values()]
    if len(days) < 2:
        return float("nan"), float("nan")
    rng = random.Random(seed)
    n = len(days)
    sims = sorted(statistics.fmean(days[rng.randrange(n)] for _ in range(n))
                  for _ in range(boot))
    return sims[int(boot * 0.025)], sims[int(boot * 0.975)]


def _stats(rows: list[dict], boot: int, seed: int) -> dict:
    if not rows:
        return {"n": 0}
    vals = [r["fwd72_net"] for r in rows]
    by_day: dict[str, list[float]] = {}
    for r in rows:
        by_day.setdefault(r["day"], []).append(r["fwd72_net"])
    days = [statistics.fmean(v) for v in by_day.values()]
    lo, hi = _day_block_ci(rows, "fwd72_net", boot, seed)
    return {
        "n_ep": len(rows), "n_days": len(days),
        "mean_bp": round(statistics.fmean(vals) * 100, 1),
        "wr_pct": round(100 * sum(1 for x in vals if x > 0) / len(vals), 1),
        "day_t": round(tstat(days), 2) if len(days) >= 2 else None,
        "day_ci_bp": [round(lo * 100, 1), round(hi * 100, 1)] if lo == lo else None,
        "y2026_n": sum(1 for r in rows if r["year"] == 2026),
        "y2026_mean_bp": (round(statistics.fmean(
            [r["fwd72_net"] for r in rows if r["year"] == 2026]) * 100, 1)
            if any(r["year"] == 2026 for r in rows) else None),
    }


def _tertile_rows(rows: list[dict], feat: str) -> list[tuple[str, list[dict]]]:
    vals = sorted(r[feat] for r in rows if math.isfinite(r.get(feat, float("nan"))))
    if len(vals) < 6:
        return [(f"all({feat})", rows)]
    q1 = vals[len(vals) // 3]
    q2 = vals[2 * len(vals) // 3]
    lo = [r for r in rows if r[feat] <= q1]
    mid = [r for r in rows if q1 < r[feat] <= q2]
    hi = [r for r in rows if r[feat] > q2]
    return [(f"{feat} T1(low)<={q1:.3g}", lo),
            (f"{feat} T2 ({q1:.3g},{q2:.3g}]", mid),
            (f"{feat} T3(high)>{q2:.3g}", hi)]


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--symbols", nargs="*",
                    default=["BTCUSDT", "ETHUSDT", "SOLUSDT"])
    ap.add_argument("--cache-dir", default="logs/binance_klines_cache")
    ap.add_argument("--top-pct", type=float, default=85.0)
    ap.add_argument("--cost-bps", type=float, default=9.26)
    ap.add_argument("--boot", type=int, default=5000)
    ap.add_argument("--seed", type=int, default=20261027)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--save", default=None)
    args = ap.parse_args(argv)

    # BTC 市场序列（用于 beta 打标）。
    btc_path = os.path.join(args.cache_dir, "BTCUSDT_1h.json")
    btc_map = None
    if os.path.exists(btc_path):
        bc = load_candles(btc_path, "BTCUSDT")
        btc_map = {"ts": [c.t for c in bc], "c": [c.c for c in bc]}

    all_rows = []
    for sym in args.symbols:
        p = os.path.join(args.cache_dir, f"{sym}_1h.json")
        if os.path.exists(p):
            all_rows.extend(episodes_with_features(
                p, args.top_pct, args.cost_bps, btc_map))

    overall = _stats(all_rows, args.boot, args.seed)
    y26 = _stats([r for r in all_rows if r["year"] == 2026], args.boot, args.seed)
    y_pre = _stats([r for r in all_rows if r["year"] < 2026], args.boot, args.seed)

    strata = {}
    for feat in ("atr24_pct", "vol24_pct", "ext_pct", "rsi", "btc_ret24_pct"):
        if not all(math.isfinite(r.get(feat, float("nan"))) for r in all_rows[:1]):
            pass
        rows_with = [r for r in all_rows
                     if math.isfinite(r.get(feat, float("nan")))]
        out = []
        for label, grp in _tertile_rows(rows_with, feat):
            s = _stats(grp, args.boot, args.seed)
            s["label"] = label
            # 该分层在 2026 是否仍为正（regime 续命的关键）。
            g26 = [r for r in grp if r["year"] == 2026]
            s["y2026_mean_bp"] = (round(statistics.fmean(
                [r["fwd72_net"] for r in g26]) * 100, 1) if g26 else None)
            s["y2026_n"] = len(g26)
            out.append(s)
        strata[feat] = out

    payload = {"top_pct": args.top_pct, "cost_bps": args.cost_bps,
               "overall": overall, "pre2026": y_pre, "y2026": y26,
               "strata": strata}

    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print("=" * 88)
        print(f"xs regime-conditional panel | top_pct={args.top_pct} "
              f"cost={args.cost_bps}bp | 72h net, independent episodes")
        for name, s in (("OVERALL", overall), ("<2026", y_pre), ("2026", y26)):
            print(f"  {name:8} ep={s.get('n_ep',0):3} days={s.get('n_days',0):3} "
                  f"mean={s.get('mean_bp')}bp WR={s.get('wr_pct')}% "
                  f"day_t={s.get('day_t')} dayCI={s.get('day_ci_bp')}")
        print("-" * 88)
        print("tertile strata (each reports overall + its 2026 subset):")
        for feat, groups in strata.items():
            print(f"  [{feat}]")
            for s in groups:
                print(f"    {s['label']:34} ep={s['n_ep']:3} days={s['n_days']:3} "
                      f"mean={s['mean_bp']:7.1f}bp t={s['day_t']:6.2f} "
                      f"CI={s['day_ci_bp']} | 2026: n={s['y2026_n']:3} "
                      f"mean={s['y2026_mean_bp']}bp")
        print("=" * 88)
    if args.save:
        with open(args.save, "w") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
        if not args.json:
            print(f"saved -> {args.save}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
