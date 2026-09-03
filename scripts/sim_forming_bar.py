#!/usr/bin/env python3
"""模拟某根暴涨 5m K 线在"未收盘形成中"各进度下的 composite 分数。

背景：扫描在 12:42(UTC) 抓到的 12:40 那根 5m bar 仍在形成中
(12:40–12:45)，此时 close 是盘中价、volume 是累计成交量，
都不是最终收盘值。本脚本用最终收盘的那根 bar 当基准，按进度
p∈[0,1] 线性插值 close、按比例缩放 volume，验证在什么进度下
composite 才能越过 gate 54，并复现线上日志的 36.4。

用法:
    python scripts/sim_forming_bar.py
    python scripts/sim_forming_bar.py --coin BTC --bar-time "2026-08-19 12:40"
    python scripts/sim_forming_bar.py --vol-skew 2.0   # 量能前置(爆发更快)
"""
from __future__ import annotations

import argparse
import datetime as dt
from typing import Any, Dict, List

from hermes_trader.agents.config import get_config
from hermes_trader.client.hl_client import fetch_hl_candles
from hermes_trader.indicators import triggers as trigger_mod

# 复用与 perception._scan_single_market 完全一致的权重/阈值
CFG = get_config()
WEIGHTS: Dict[str, float] = CFG["weights"]
TH: Dict[str, Any] = CFG["thresholds"]
GATE = CFG["scan"]["minCompositeScore"]


def _eval(candles: List[Any]) -> tuple[float, List[tuple[str, float, bool]]]:
    """复刻 perception 里的 5m 触发器组合，返回 (composite, fired 明细)。"""
    hits = [
        trigger_mod.pct_move_spike(candles, TH["sigmaThreshold"]),
        trigger_mod.volume_spike(candles, TH["sigmaThreshold"]),
        trigger_mod.breakout(
            candles,
            TH["breakoutLookback"],
            min_rvol=TH["breakoutMinRvol"],
            rvol_window=TH["breakoutRvolWindow"],
            atr_score_mult=TH["breakoutAtrScoreMult"],
        ),
        trigger_mod.range_compression(candles, TH["bbLength"], TH["bbStdDev"]),
        trigger_mod.trend_strength(candles, TH["adxPeriod"]),
        trigger_mod.momentum_burst(candles, TH["momentumLookback"], TH["momentumPct"]),
        # 1h 触发器(weight 0)对 composite 无影响，此处略过；
        # trendFlip1h/higherLows1h 只影响 fired 列表展示，不计分。
    ]
    score = trigger_mod.composite_score(hits, WEIGHTS)
    fired = [(h["name"], round(h["score"], 2), h.get("fired")) for h in hits
             if h.get("fired") or h.get("score", 0) > 0]
    return score, fired


def _ts_minute(s: str) -> int:
    # 解析为 UTC 时间戳(秒)。容器/交易所均为 UTC。
    d = dt.datetime.strptime(s, "%Y-%m-%d %H:%M").replace(tzinfo=dt.timezone.utc)
    return int(d.timestamp())


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--coin", default="BTC")
    ap.add_argument("--bar-time", default="2026-08-19 12:40",
                    help="目标暴涨 5m bar 的开盘时间(UTC, 'YYYY-MM-DD HH:MM')")
    ap.add_argument("--count", type=int, default=100, help="拉取的 5m K 线数量")
    ap.add_argument("--vol-skew", type=float, default=1.0,
                    help="量能前置系数:>1 表示形成早期放量更快(真实爆发形态)")
    args = ap.parse_args()

    target_ts = _ts_minute(args.bar_time)
    candles = list(fetch_hl_candles(args.coin, "5m", args.count))

    def _bar_ts(c) -> int:
        return int(getattr(c, "t")) // 1000

    # 定位目标 bar
    idx = None
    for i, c in enumerate(candles):
        if _bar_ts(c) == target_ts:
            idx = i
            break
    if idx is None:
        have = [dt.datetime.utcfromtimestamp(_bar_ts(c)).strftime("%m-%d %H:%M")
                for c in (candles[0], candles[-1])]
        raise SystemExit(
            f"[error] 未找到 {args.bar_time} UTC 的 bar。当前数据范围 "
            f"{have[0]} ~ {have[1]}，请加大 --count（如 --count 500）")

    target = candles[idx]

    def _g(c, k):
        return c[k] if isinstance(c, dict) else getattr(c, k)

    def _sim_bar(p: float):
        """构造形成进度 p 下的那根 bar(close 线性推进、vol 按份额)。"""
        sim_close = open_ + (final_close - open_) * p
        if args.vol_skew != 1.0:
            share = p ** (1.0 / args.vol_skew)
        else:
            share = p
        sim_vol = final_vol * min(1.0, share)
        hi = max(open_, sim_close)
        lo = min(open_, sim_close)
        if isinstance(target, dict):
            return {**target, "o": open_, "h": hi, "l": lo,
                    "c": sim_close, "v": sim_vol}, sim_close, sim_vol
        # Pydantic Candle
        return (target.model_copy(update={"o": open_, "h": hi, "l": lo,
                                          "c": sim_close, "v": sim_vol}),
                sim_close, sim_vol)

    prev_close = float(_g(candles[idx - 1], "c"))
    final_close = float(_g(target, "c"))
    final_vol = float(_g(target, "v"))
    open_ = float(_g(target, "o"))

    print("=" * 78)
    print(f"目标 bar @ {args.bar_time} UTC  (开盘 {dt.datetime.utcfromtimestamp(target_ts)})")
    print(f"  O={open_:.1f}  C(final)={final_close:.1f}  V(final)={final_vol:.1f}")
    print(f"  上一根收盘={prev_close:.1f}  涨幅={ (final_close-prev_close)/prev_close*100:+.2f}%")
    print(f"  gate={GATE}  weights={ {k:v for k,v in WEIGHTS.items() if v>0} }")
    print(f"  vol_skew={args.vol_skew}")
    print("=" * 78)

    # 以目标 bar 及之前的 K 线构成窗口(与生产 candleCount=100 一致时取最后100根)。
    # 形成中的 bar 是窗口最后一根；这里用"截至目标 bar"的窗口来替换最后一根。
    window_base = candles[max(0, idx - args.count + 1): idx + 1]

    # 关键对照：上一根已收盘 bar 的分数(扫描若落在 bar 刚开盘时)
    prev_window = candles[max(0, idx - args.count): idx]  # 不含目标 bar
    if len(prev_window) >= 50:
        ps, pf = _eval(prev_window)
        print(f"\n[上一根收盘 {args.bar_time} 之前] composite={ps:.1f}  fired={pf}")

    print(f"\n{'进度':>6} {'close':>10} {'vol':>9} {'RVOL':>6} "
          f"{'composite':>10} {'过gate':>6}  fired(score)")
    print("-" * 78)

    # 形成进度扫描：close 在 open→final_close 间线性插值；
    # volume 按比例(配合 vol_skew 做前置)，封顶 final_vol。
    steps = [0.05, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 0.95, 1.0]
    first_pass = None
    for p in steps:
        sim_bar, sim_close, sim_vol = _sim_bar(p)
        win = window_base[:-1] + [sim_bar]
        s, fired = _eval(win)

        # RVOL 显示
        bk = next((f for f in fired if f[0] == "breakout"), None)
        rvol_str = ""
        if bk:
            # 从 reason 里取 RVOL；简单重算
            vols = [float(_g(c, "v")) for c in win]
            w = TH["breakoutRvolWindow"]
            avg = sum(vols[-w-1:-1]) / w
            rvol_str = f"{sim_vol/avg:.2f}" if avg else "n/a"

        pass_flag = "YES" if s >= GATE else "-"
        if s >= GATE and first_pass is None:
            first_pass = p
        fired_str = ", ".join(f"{n}={sc}" for n, sc, _ in fired)
        print(f"{p*100:>5.0f}% {sim_close:>10.1f} {sim_vol:>9.1f} {rvol_str:>6} "
              f"{s:>10.1f} {pass_flag:>6}  {fired_str}")

    print("-" * 78)
    if first_pass is not None:
        print(f"\n结论：在 vol_skew={args.vol_skew} 下，形成进度达到 "
              f"{first_pass*100:.0f}% (即开盘后约 {first_pass*5:.1f} 分钟) "
              f"composite 才越过 {GATE}。")
    else:
        print(f"\n结论：即使该 bar 完全收盘(100%)，composite 也未越过 {GATE}。")

    # 复盘线上日志 12:42 的 36.4：扫描发生在 bar 开盘后约 2 分钟(~40% 进度)
    print("\n[对照线上日志] 12:42 扫描 ≈ bar 开盘后 2 分钟(进度 ~40%)：")
    for p in (0.3, 0.4, 0.5):
        sim_bar, sim_close, sim_vol = _sim_bar(p)
        s, fired = _eval(window_base[:-1] + [sim_bar])
        print(f"  进度 {p*100:.0f}%: composite={s:.1f}  fired={[n for n,_,_ in fired]}")


if __name__ == "__main__":
    main()
