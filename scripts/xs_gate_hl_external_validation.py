#!/usr/bin/env python3
"""S2 外生验证：在 Hyperliquid 1h 数据上复核 xs 组合门（只读，无下单）。

最关键的一关：Binance 数据上发现的"超跌反弹 + 恐慌度门"edge 必须在**另一个
交易所/数据源**（Hyperliquid，生产实际成交场所）上复现，否则可能只是 Binance
数据或单一微结构的产物。

数据约束（已实测）：HL info API 的 1h candleSnapshot 绝对时间窗最早只能回溯到
约 2026-04（更早返回空）。本地 logs/hl_klines_cache/{COIN}_1h.json 覆盖
2026-02-24 → 2026-09-21（5002 根/币，约 208 天），是当前能拿到的最长 1h HL
历史，故本脚本用它。结论因此是 **2026 年内、跨数据源**的外生检验（无法做
2023–25 跨年，HL 不可得）；对照基准是 Binance 同币种在**同日期区间**的子集，
而非其 3.4 年全样本，保证两边可比。

HL 缓存是 list[dict]，字段为字符串：{t,o,h,l,c,v,...}，与 Binance 数组布局
不同，故单独写加载器；信号/去重叠/打分一律复用 xs_episode_stats 与
xs_regime_conditional 的生产对齐原语，保证口径一致。

用法：
  python scripts/xs_gate_hl_external_validation.py
  python scripts/xs_gate_hl_external_validation.py --coins BTC ETH SOL --top-pct 85
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
from typing import Optional

from hermes_trader.models.types import Candle
from scripts.xs_episode_stats import (
    HOLD_HOURS,
    deoverlap,
    replay_coin,
)
from scripts.xs_regime_conditional import _stats, episodes_with_features

HL_CACHE = "logs/hl_klines_cache"
BINANCE_CACHE = "logs/binance_klines_cache"
# 与 Binance 发现阶段冻结的组合门完全一致（不在 HL 上重新调参）。
GATE_EXT = -8.0
GATE_ATR = 1.0
GATE_RSI = 20.0


def load_hl_candles(coin: str) -> list[Candle]:
    path = os.path.join(HL_CACHE, f"{coin}_1h.json")
    with open(path) as fh:
        raw = json.load(fh)
    out = [Candle(t=float(r["t"]), o=float(r["o"]), h=float(r["h"]),
                  l=float(r["l"]), c=float(r["c"]), v=float(r["v"]),
                  coin=coin) for r in raw]
    out.sort(key=lambda c: c.t)
    return out


def load_binance_candles(sym: str) -> list[Candle]:
    path = os.path.join(BINANCE_CACHE, f"{sym}_1h.json")
    with open(path) as fh:
        raw = json.load(fh)
    out = [Candle(t=float(b[0]), o=float(b[1]), h=float(b[2]),
                  l=float(b[3]), c=float(b[4]), v=float(b[5]), coin=sym)
           for b in raw]
    out.sort(key=lambda c: c.t)
    return out


def hl_episodes(coins: list[str], top_pct: float,
                cost_bps: float) -> list[dict]:
    """在 HL K线上重放（HL 没有独立 BTC 市场序列，beta 标签留空）。"""
    rows = []
    for coin in coins:
        candles = load_hl_candles(coin)
        # replay_coin 需要 coin 仅用于标记；直接复用同一纯函数。
        ep = deoverlap(replay_coin(candles, top_pct, cost_bps), HOLD_HOURS)
        for r in ep:
            i = r["i"]
            r["coin"] = coin
            r["year"] = dt.datetime.utcfromtimestamp(r["ts"] / 1000).year
            r["day"] = dt.datetime.utcfromtimestamp(
                r["ts"] / 1000).strftime("%Y-%m-%d")
            from scripts.xs_regime_conditional import _atr24_pct
            r["atr24_pct"] = round(_atr24_pct(candles, i), 4)
            r["btc_ret24_pct"] = float("nan")
        rows.extend(ep)
    return rows


def gate(r: dict) -> bool:
    return (r["ext_pct"] <= GATE_EXT and r["atr24_pct"] >= GATE_ATR
            and r["rsi"] >= GATE_RSI)


def _span(rows: list[dict]) -> tuple[str, str]:
    ts = [r["ts"] for r in rows]
    return (dt.datetime.utcfromtimestamp(min(ts) / 1000).strftime("%Y-%m-%d"),
            dt.datetime.utcfromtimestamp(max(ts) / 1000).strftime("%Y-%m-%d"))


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--coins", nargs="+", default=["BTC", "ETH", "SOL"],
                    help="HL 币种名（无 USDT 后缀）")
    ap.add_argument("--binance-symbols", nargs="+",
                    default=["BTCUSDT", "ETHUSDT", "SOLUSDT"])
    ap.add_argument("--top-pct", type=float, default=85.0)
    ap.add_argument("--cost-bps", type=float, default=9.26)
    ap.add_argument("--boot", type=int, default=5000)
    ap.add_argument("--seed", type=int, default=20261027)
    ap.add_argument("--save", default=None)
    args = ap.parse_args(argv)

    # ---- HL：外生数据源 ----
    hl_rows = hl_episodes(args.coins, args.top_pct, args.cost_bps)
    hl_span = _span(hl_rows)
    hl_all = _stats(hl_rows, args.boot, args.seed)
    hl_gated = [r for r in hl_rows if gate(r)]
    hl_gate_stats = _stats(hl_gated, args.boot, args.seed)

    # 邻域稳健性（在 HL 上只验证，不重选阈值）
    grid = {}
    for ext_t in (-7.0, -8.0, -9.0):
        for atr_t in (0.8, 1.0, 1.2):
            ss = [r for r in hl_rows if r["ext_pct"] <= ext_t
                  and r["atr24_pct"] >= atr_t and r["rsi"] >= GATE_RSI]
            s = _stats(ss, args.boot, args.seed)
            grid[f"ext<={ext_t}|atr>={atr_t}"] = {
                "n": s.get("n_ep", 0), "mean_bp": s.get("mean_bp"),
                "t": s.get("day_t"), "ci_bp": s.get("day_ci_bp")}

    # ---- Binance 同日期区间对照（公平比较）----
    lo_ms = min(r["ts"] for r in hl_rows)
    hi_ms = max(r["ts"] for r in hl_rows)
    btc_c = load_binance_candles("BTCUSDT")
    btc_map = {"ts": [c.t for c in btc_c], "c": [c.c for c in btc_c]}
    bn_rows = []
    for sym in args.binance_symbols:
        p = os.path.join(BINANCE_CACHE, f"{sym}_1h.json")
        if os.path.exists(p):
            rs = episodes_with_features(p, args.top_pct, args.cost_bps, btc_map)
            bn_rows.extend(r for r in rs if lo_ms <= r["ts"] <= hi_ms)
    bn_all = _stats(bn_rows, args.boot, args.seed)
    bn_gate_stats = _stats([r for r in bn_rows if gate(r)], args.boot, args.seed)

    # 逐币 HL 明细（样本量）
    per_coin = {}
    for c in args.coins:
        cr = [r for r in hl_rows if r["coin"] == c]
        cg = [r for r in cr if gate(r)]
        per_coin[c] = {"all": _stats(cr, 1000, args.seed),
                       "gated": _stats(cg, 1000, args.seed)}

    payload = {
        "test": "xs combined gate external validation on Hyperliquid",
        "gate": {"ext_pct<=": GATE_EXT, "atr24_pct>=": GATE_ATR,
                 "rsi>=": GATE_RSI, "frozen_from": "binance_discovery"},
        "hl_span": list(hl_span), "top_pct": args.top_pct,
        "cost_bps": args.cost_bps,
        "hl": {"span": hl_span, "n_rows": len(hl_rows),
               "all_candidates": hl_all, "gated": hl_gate_stats,
               "per_coin": per_coin, "neighbor_grid": grid},
        "binance_same_window": {"span": hl_span, "n_rows": len(bn_rows),
                                "all_candidates": bn_all, "gated": bn_gate_stats},
    }
    # 通过判据分两级（关键：方向复现 ≠ 统计确认）：
    #  direction  : HL gated 均值为正且方向与 Binance 同期一致（跨数据源存在性）。
    #  statistical: 在方向基础上，HL 独立信号日 >=20 且日块 CI 下界>0、t>=2。
    # HL API 1h 仅回溯 ~2026-04、本地缓存 7 个月，组合门触发极稀疏（个位数），
    # 因此当前最多只能给"方向复现"，统计确认必须靠继续影子采样累积独立日。
    hlci = hl_gate_stats.get("day_ci_bp") or [float("nan"), float("nan")]
    bnci = bn_gate_stats.get("day_ci_bp") or [float("nan"), float("nan")]
    hl_days = hl_gate_stats.get("n_days") or 0
    payload["verdict"] = {
        "hl_gated_n_episodes": hl_gate_stats.get("n_ep", 0),
        "hl_gated_n_signal_days": hl_days,
        "hl_gated_t_ge_2": (hl_gate_stats.get("day_t") or -9) >= 2.0,
        "hl_gated_ci_lo_gt_0": hlci[0] > 0,
        "hl_gated_mean_pos": (hl_gate_stats.get("mean_bp") or -1) > 0,
        "binance_same_window_mean_pos": (bn_gate_stats.get("mean_bp") or -1) > 0,
        "binance_same_window_ci_lo_gt_0": bnci[0] > 0,
        "min_independent_days_for_confirm": 20,
    }
    v = payload["verdict"]
    v["direction_replicated"] = bool(
        v["hl_gated_mean_pos"] and v["binance_same_window_mean_pos"])
    v["statistically_confirmed"] = bool(
        v["direction_replicated"] and hl_days >= 20
        and v["hl_gated_ci_lo_gt_0"] and v["hl_gated_t_ge_2"])
    payload["verdict"]["external_validation_pass"] = v["statistically_confirmed"]

    print("=" * 86)
    print("xs combined gate — EXTERNAL validation on Hyperliquid 1h")
    print(f"HL candles span {list(hl_span)} (cache 2026-02-24->09-21, ~208d; "
          f"2026 only) first/last xs SIGNAL {_span(hl_rows)}  "
          f"top_pct={args.top_pct} cost={args.cost_bps}bp")
    print("-" * 86)
    print(f"HL  all candidates : {hl_all}")
    print(f"HL  gated          : {hl_gate_stats}")
    print("  per-coin gated:")
    for c, s in per_coin.items():
        print(f"    {c:4} all={s['all']}  gated={s['gated']}")
    print("  neighbor grid (frozen thresholds, validation only):")
    for k, s in grid.items():
        print(f"    {k:18} n={s['n']:3} mean={s['mean_bp']}bp t={s['t']} CI={s['ci_bp']}")
    print("-" * 86)
    print(f"Binance SAME window all: {bn_all}")
    print(f"Binance SAME window gated: {bn_gate_stats}")
    print("-" * 86)
    print("verdict:", json.dumps(payload["verdict"], ensure_ascii=False))
    print("=" * 86)

    if args.save:
        with open(args.save, "w") as fh:
            # 严格 JSON：拒绝 NaN/Infinity（n<2 独立日时 t 统计无定义，应为
            # None 而非裸 NaN），保证 jq/JS 可解析。
            json.dump(payload, fh, ensure_ascii=False, indent=2, allow_nan=False)
        print(f"saved -> {args.save}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
