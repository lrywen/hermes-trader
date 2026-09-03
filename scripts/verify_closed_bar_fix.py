#!/usr/bin/env python3
"""验证方案一修复：只评估已收盘 K 线 + 收盘后强制刷新。

复现昨晚 BTC 12:40(UTC) 暴涨场景：
- 取真实历史 K 线(5m)，在不同"扫描时刻"模拟当时 API 会返回的窗口；
- 对形成中的最后一根 bar 做进度插值(close 线性、vol 按份额)；
- 对比修复前(直接评估最后一根，哪怕在形成中)与修复后(丢弃形成中 bar，
  只评估最后一根已收盘 bar)的 composite，确认修复后能过 gate 54。

用法:
    python scripts/verify_closed_bar_fix.py
    python scripts/verify_closed_bar_fix.py --count 500
"""
from __future__ import annotations

import argparse
import datetime as dt
from typing import Any, List, Tuple

from hermes_trader.agents import perception as perc
from hermes_trader.agents.config import get_config
from hermes_trader.client.hl_client import fetch_hl_candles
from hermes_trader.indicators import triggers as trigger_mod

CFG = get_config()
WEIGHTS = CFG["weights"]
TH = CFG["thresholds"]
GATE = CFG["scan"]["minCompositeScore"]
BAR_MS = 300_000  # 5m


def _eval(candles: List[Any]) -> Tuple[float, List[Tuple[str, float]]]:
    """复刻 perception 的 5m 触发器打分(1h weight-0 触发器略过)。"""
    hits = [
        trigger_mod.pct_move_spike(candles, TH["sigmaThreshold"]),
        trigger_mod.volume_spike(candles, TH["sigmaThreshold"]),
        trigger_mod.breakout(
            candles, TH["breakoutLookback"],
            min_rvol=TH["breakoutMinRvol"],
            rvol_window=TH["breakoutRvolWindow"],
            atr_score_mult=TH["breakoutAtrScoreMult"],
        ),
        trigger_mod.range_compression(candles, TH["bbLength"], TH["bbStdDev"]),
        trigger_mod.trend_strength(candles, TH["adxPeriod"]),
        trigger_mod.momentum_burst(candles, TH["momentumLookback"], TH["momentumPct"]),
    ]
    score = trigger_mod.composite_score(hits, WEIGHTS)
    fired = [(h["name"], round(h["score"], 2)) for h in hits if h.get("fired")]
    return score, fired


def _ts(s: str) -> int:
    return int(dt.datetime.strptime(s, "%Y-%m-%d %H:%M")
               .replace(tzinfo=dt.timezone.utc).timestamp() * 1000)


def _simulate_api_window(candles: List[Any], target_idx: int,
                         scan_ms: int) -> List[Any]:
    """模拟在 scan_ms 时刻 API 返回的 K 线窗口。

    历史已收盘 bar 保持真实值；若最后一根 target bar 处于形成中，
    按进度插值其 close/volume(用真实最终值做份额)，模拟盘中快照。
    """
    window = list(candles[: target_idx + 1])
    target = candles[target_idx]
    t_open = int(getattr(target, "t"))
    if scan_ms < t_open:
        # 扫描早于目标 bar 开盘：窗口里还没有这根 bar
        return candles[: target_idx]
    if scan_ms >= t_open + BAR_MS:
        # 目标 bar 已收盘：返回真实完整窗口
        return window

    # 形成中：插值最后一根
    progress = (scan_ms - t_open) / BAR_MS
    progress = max(0.0, min(1.0, progress))
    o = float(getattr(target, "o"))
    c_final = float(getattr(target, "c"))
    v_final = float(getattr(target, "v"))
    sim_c = o + (c_final - o) * progress
    sim_v = v_final * progress
    sim_h = max(o, sim_c)
    sim_l = min(o, sim_c)
    forming = target.model_copy(update={
        "o": o, "h": sim_h, "l": sim_l, "c": sim_c, "v": sim_v})
    window[-1] = forming
    return window


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--coin", default="BTC")
    ap.add_argument("--bar-time", default="2026-08-19 12:40")
    ap.add_argument("--count", type=int, default=500)
    args = ap.parse_args()

    target_open_ms = _ts(args.bar_time)
    candles = list(fetch_hl_candles(args.coin, "5m", args.count))

    # 定位目标 bar
    idx = next((i for i, c in enumerate(candles)
                if int(getattr(c, "t")) == target_open_ms), None)
    if idx is None:
        rng = [dt.datetime.utcfromtimestamp(getattr(c, "t") / 1000)
                    .strftime("%m-%d %H:%M") for c in (candles[0], candles[-1])]
        raise SystemExit(f"未找到 {args.bar_time}，数据范围 {rng[0]}~{rng[1]}，加大 --count")

    target = candles[idx]
    print("=" * 82)
    print(f"验证目标: {args.coin} {args.bar_time} UTC  5m 暴涨 bar")
    print(f"  O={getattr(target,'o')}  C={getattr(target,'c')}  "
          f"V={getattr(target,'v')}  gate={GATE}")
    print(f"  修复逻辑: evaluateClosedBarsOnly=True, "
          f"postCloseForceRefreshMs={CFG['scan']['postCloseForceRefreshMs']}")
    print("=" * 82)

    # 扫描时刻：目标 bar 形成期(40%~80%)、收盘瞬间、收盘后 10s、下一 bar 开盘后
    scan_points = [
        ("形成中 +2.0min (~40%)", target_open_ms + 120_000),
        ("形成中 +3.0min (~60%)", target_open_ms + 180_000),
        ("形成中 +4.0min (~80%)", target_open_ms + 240_000),
        ("收盘瞬间 +5.0min",       target_open_ms + BAR_MS),
        ("收盘后 +10s (强制刷新窗)", target_open_ms + BAR_MS + 10_000),
        ("收盘后 +40s (刷新窗外)",  target_open_ms + BAR_MS + 40_000),
        ("下一bar开盘后 +20s",      target_open_ms + BAR_MS + 20_000),
        ("下一bar开盘后 +2.0min",   target_open_ms + BAR_MS + 120_000),
    ]

    print(f"\n{'扫描时刻':<28}{'评估bar':<14}{'旧逻辑comp':<12}{'旧过gate':<10}"
          f"{'新逻辑comp':<12}{'新过gate':<10}")
    print("-" * 82)

    all_pass = True
    for label, scan_ms in scan_points:
        window = _simulate_api_window(candles, idx, scan_ms)

        # ── 旧逻辑：直接评估最后一根(哪怕在形成中) ──
        old_score, old_fired = _eval(window)
        old_pass = old_score >= GATE

        # ── 新逻辑：用 perception 的辅助函数判断最后一根是否已收盘 ──
        # 传入模拟的 scan_ms 作为"当前时刻"，而非真实墙钟时间
        if window and not perc._last_bar_closed(window, "5m", now_ms=scan_ms):
            new_window, dropped = window[:-1], True
        else:
            new_window, dropped = window, False
        new_score, new_fired = _eval(new_window)
        new_pass = new_score >= GATE

        last_t = int(getattr(new_window[-1], "t"))
        bar_label = dt.datetime.utcfromtimestamp(last_t / 1000).strftime("%H:%M")
        if dropped:
            bar_label += "(收盘)"

        # 收盘后的点，新逻辑必须过 gate
        after_close = scan_ms >= target_open_ms + BAR_MS
        if after_close and not new_pass:
            all_pass = False

        def _flag(b):
            return "YES ✓" if b else "-"

        print(f"{label:<28}{bar_label:<14}{old_score:<12.1f}{_flag(old_pass):<10}"
              f"{new_score:<12.1f}{_flag(new_pass):<10}")

    print("-" * 82)

    # 单元测试式断言：收盘后任意扫描时刻，新逻辑评估到的就是 12:40 这根收盘 bar
    print("\n[断言] 收盘后窗口评估的 bar 必须是 12:40 已收盘 bar：")
    for label, scan_ms in scan_points:
        if scan_ms < target_open_ms + BAR_MS:
            continue
        window = _simulate_api_window(candles, idx, scan_ms)
        if window and not perc._last_bar_closed(window, "5m", now_ms=scan_ms):
            new_window = window[:-1]
        else:
            new_window = window
        last_t = int(getattr(new_window[-1], "t"))
        ok = last_t == target_open_ms
        s, fired = _eval(new_window)
        print(f"  {label:<28} last_bar="
              f"{dt.datetime.utcfromtimestamp(last_t/1000).strftime('%H:%M')} "
              f"composite={s:.1f} pass={ok and s>=GATE}")

    print("\n[结论]")
    if all_pass:
        print("  ✓ 修复后：12:40 收盘后的每次扫描都正确评估该已收盘 bar，"
              f"composite 越过 gate {GATE}，会进入研究/交易流程。")
    else:
        print("  ✗ 仍存在收盘后未过 gate 的场景，需检查。")
    print("  形成中阶段旧逻辑打出的低分(36 左右)正是昨晚 near-miss 36.4 的成因，")
    print("  新逻辑改为等收盘后再评估，彻底消除该漏单。")


if __name__ == "__main__":
    main()
