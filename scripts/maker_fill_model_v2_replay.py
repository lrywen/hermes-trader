#!/usr/bin/env python3
"""maker 成交模型 v1 vs v2 离线回放对照（零资金、纯本地）。

目的（S1 验收）：在同一条合成/历史信号流上，并排比较
  v1 = 触及即成交 + 按 limit 成交（固定白拿 offset，post-fill drift 仅记账）；
  v2 = 条件成交（队列穿透概率）+ 把 adverse drift 计入成交价。

输出每模型的：
  - fill rate（成交 / (成交+撤单)）；
  - captured maker edge（成交价相对挂单 mid 的改善，bps）；
  - post-fill adverse drift（残留逆选择，bps）；
  - 净 maker 优势 ≈ captured_edge − residual_adverse_drift（bps）；
  - runaway 撤单率（价格擦到一次后跑掉、v1 却会成交的占比）。

预期方向（v2 更悲观、且逆选择被计价）：
  fill_rate_v2 <= fill_rate_v1；
  net edge_v2 < net edge_v1；
  v2 的 captured_edge 与 residual drift 相抵后净优势回到 ~0 附近。

信号流默认用带种子的随机游走合成（可复现、无网络）。如需在真实 1m K线上
回放，可传 --candles path.json（期望 [{"t","o","h","l","c","v"}, ...]）。

用法：
  python scripts/maker_fill_model_v2_replay.py
  python scripts/maker_fill_model_v2_replay.py --sims 2000 --seed 20261027
  python scripts/maker_fill_model_v2_replay.py --candles btc_1m.json --mc 200
"""
from __future__ import annotations

import argparse
import json
import random
import statistics
import sys
from typing import Optional

from hermes_trader.execution.maker_shadow import (
    V2_DEFAULT_ADV_DRIFT_KAPPA,
    V2_DEFAULT_BASE_FILL_PROB,
    V2_DEFAULT_FILL_PROB_DECAY,
    ShadowMakerOrder,
    simulate_shadow_order,
    simulate_shadow_order_v2,
)
from hermes_trader.models.types import Candle

OFFSET_BPS = 5.0
TTL_BARS = 30
BAR_MS = 60_000


# --------------------------------------------------------------------------- #
# 信号流
# --------------------------------------------------------------------------- #

def _synthetic_stream(n_signals: int, seed: int, *, drift_bps: float = 0.0,
                      vol_bps: float = 12.0) -> list[tuple[ShadowMakerOrder, list[Candle]]]:
    """随机游走生成买单信号 + post 后 TTL 窗口的 1m K线。

    drift_bps/bar 控制净趋势（正=慢牛、负=阴跌），vol_bps 为单根振幅。
    价格更常见"擦到下方限价后弹回/跑掉"，足以暴露两模型差异。
    """
    rng = random.Random(seed)
    out = []
    for _ in range(n_signals):
        mid = 100.0
        bars: list[Candle] = []
        # idx0 = 挂单 bar（已收盘），其后生成 ttl+1 根（含成交后一根，供算 drift）
        bars.append(Candle(t=0, o=mid, h=mid, l=mid, c=mid, v=1.0))
        px = mid
        for k in range(1, TTL_BARS + 2):
            shock = rng.gauss(drift_bps, vol_bps) / 1e4
            o = px
            c = max(0.01, px * (1.0 + shock))
            wick = abs(rng.gauss(0.0, vol_bps)) / 1e4
            h = max(o, c) * (1.0 + wick)
            l = min(o, c) * (1.0 - wick)
            bars.append(Candle(t=k * BAR_MS, o=o, h=h, l=l, c=c, v=1.0))
            px = c
        limit = mid * (1.0 - OFFSET_BPS / 1e4)
        order = ShadowMakerOrder(
            coin="SYNTH", is_buy=True, size=1.0, posted_bar_idx=0,
            limit_px=limit, post_mid_px=mid, ttl_bars=TTL_BARS)
        out.append((order, bars))
    return out


def _load_candle_file(path: str) -> tuple[str, list[Candle]]:
    """读取 K线文件，返回 (interval_ms, bars)。

    支持两种布局：
      - list[[t,o,h,l,c,v], ...]（Binance klines 缓存原始格式）；
      - list[{"t","o","h","l","c","v"}, ...]（--json 导出）；
      - {"interval_minutes": M, "bars": [...]}（显式粒度，缺省时按中位时间差）。
    """
    with open(path) as fh:
        raw = json.load(fh)
    interval_min = None
    rows = raw
    if isinstance(raw, dict):
        interval_min = raw.get("interval_minutes") or raw.get("interval_min")
        rows = raw.get("bars") or raw.get("candles")

    def _candle(b):
        if isinstance(b, dict):
            return Candle(t=float(b["t"]), o=float(b["o"]), h=float(b["h"]),
                          l=float(b["l"]), c=float(b["c"]), v=float(b.get("v", 1.0)))
        return Candle(t=float(b[0]), o=float(b[1]), h=float(b[2]),
                      l=float(b[3]), c=float(b[4]), v=float(b[5]) if len(b) > 5 else 1.0)

    bars = sorted((_candle(b) for b in rows), key=lambda x: x.t)
    if interval_min is None and len(bars) > 1:
        diffs = sorted(bars[i + 1].t - bars[i].t
                       for i in range(len(bars) - 1))
        interval_min = diffs[len(diffs) // 2] / 60_000.0
    return float(interval_min or 1.0), bars


def _stream_from_candles(path: str) -> tuple[list[tuple[ShadowMakerOrder, list[Candle]]], float]:
    interval_min, bars = _load_candle_file(path)
    # TTL 以"根数"表示并随粒度缩放：maker 目标 resting 30 分钟 → 30/M 根。
    ttl_bars = max(2, round(TTL_BARS * 1.0 / interval_min))
    window_len = ttl_bars + 2
    out = []
    for start in range(0, len(bars) - window_len, ttl_bars + 1):
        window = bars[start:start + window_len]
        if len(window) < window_len:
            break
        # 只做价格归一（首根收盘=100）；时间重映射为连续根号 0..N（simulator
        # 仅比较 posted/fill 的相对根号，不依赖真实 ms 间隔），避免把 5m 时间轴
        # 错误压缩成 1m 导致 post-fill 取到窗口外。
        rebase = [
            Candle(t=float(i),
                   o=window[i].o / window[0].c * 100.0,
                   h=window[i].h / window[0].c * 100.0,
                   l=window[i].l / window[0].c * 100.0,
                   c=window[i].c / window[0].c * 100.0,
                   v=window[i].v)
            for i in range(len(window))]
        mid = rebase[0].c
        order = ShadowMakerOrder(
            coin="BTC", is_buy=True, size=1.0, posted_bar_idx=0,
            limit_px=mid * (1.0 - OFFSET_BPS / 1e4), post_mid_px=mid,
            ttl_bars=ttl_bars)
        out.append((order, rebase))
    return out, interval_min


# --------------------------------------------------------------------------- #
# 聚合
# --------------------------------------------------------------------------- #

def _mean(xs):
    return statistics.fmean(xs) if xs else float("nan")


def evaluate(stream, *, mc: int, seed: int,
             base: float, decay: float, kappa: float) -> dict:
    """v1 单次（确定性）；v2 做 mc 次蒙特卡洛取期望（v1 与流无关，只算一次）。"""
    v1_edges, v1_drift, v1_decided, v1_filled = [], [], 0, 0
    v2_edges, v2_drift, v2_decided, v2_filled = [], [], 0, 0
    v2_runaway_cancel = 0
    v1_verdict = {}
    for order, bars in stream:
        f1 = simulate_shadow_order(order, bars)
        v1_verdict[id(order)] = f1
        v1_decided += 1
        if f1.filled:
            v1_filled += 1
            if f1.maker_edge_bps is not None:
                v1_edges.append(f1.maker_edge_bps)
            if f1.post_fill_mid_drift_bps is not None:
                v1_drift.append(f1.post_fill_mid_drift_bps)

    # v2 蒙特卡洛：同一流跑 mc 遍，统计平均成交率/指标。runaway 率按"每次
    # MC 运行内 v2 撤单数 / v1 成交数"计算后再对运行取均值（避免跨次累加）。
    runaway_ratios = []
    for r in range(mc):
        rng = random.Random(seed + r)
        run_runaway = 0
        for order, bars in stream:
            f1 = v1_verdict[id(order)]
            f2 = simulate_shadow_order_v2(
                order, bars, base_fill_prob=base, fill_prob_decay=decay,
                adv_drift_kappa=kappa, deterministic=False, rng=rng)
            v2_decided += 1
            if f2.filled:
                v2_filled += 1
                if f2.maker_edge_bps is not None:
                    v2_edges.append(f2.maker_edge_bps)
                if f2.post_fill_mid_drift_bps is not None:
                    v2_drift.append(f2.post_fill_mid_drift_bps)
            elif f1.filled and f2.touch_bar_idx is not None:
                # v1 会成交但 v2 撤单 = runaway/未排到（被 v1 虚高的部分）。
                v2_runaway_cancel += 1
                run_runaway += 1
        if v1_filled:
            runaway_ratios.append(100.0 * run_runaway / v1_filled)
    v2_runaway_cancel_pct = _mean(runaway_ratios)
    v2_runaway_cancel_pct = (
        float("nan") if v2_runaway_cancel_pct != v2_runaway_cancel_pct
        else round(v2_runaway_cancel_pct, 2))

    def pack(decided, filled, edges, drift):
        fr = 100.0 * filled / decided if decided else 0.0
        e, d = _mean(edges), _mean(drift)
        return {
            "fill_rate_pct": round(fr, 2),
            "avg_captured_edge_bps": None if e != e else round(e, 3),
            "avg_residual_adverse_drift_bps": None if d != d else round(d, 3),
            "net_maker_advantage_bps": (
                None if (e != e or d != d) else round(e - d, 3)),
        }

    v1_cancels = v1_decided - v1_filled
    return {
        "n_signals": len(stream),
        "v2_mc_runs": mc,
        "params": {"base_fill_prob": base, "fill_prob_decay": decay,
                   "adv_drift_kappa": kappa, "offset_bps": OFFSET_BPS},
        "v1": pack(v1_decided, v1_filled, v1_edges, v1_drift),
        "v2": pack(v2_decided, v2_filled, v2_edges, v2_drift),
        "v2_runaway_cancel_pct_of_v1_fills": v2_runaway_cancel_pct,
        "v1_cancels": v1_cancels,
    }


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sims", type=int, default=2000, help="合成信号数")
    ap.add_argument("--seed", type=int, default=20261027)
    ap.add_argument("--drift-bps", type=float, default=0.0,
                    help="合成流每根 bar 净漂移（正慢牛/负阴跌）")
    ap.add_argument("--vol-bps", type=float, default=12.0, help="单根波动")
    ap.add_argument("--candles", default=None, help="真实 1m K线 JSON（可选）")
    ap.add_argument("--mc", type=int, default=200, help="v2 蒙特卡洛次数")
    ap.add_argument("--base", type=float, default=V2_DEFAULT_BASE_FILL_PROB)
    ap.add_argument("--decay", type=float, default=V2_DEFAULT_FILL_PROB_DECAY)
    ap.add_argument("--kappa", type=float, default=V2_DEFAULT_ADV_DRIFT_KAPPA)
    ap.add_argument("--json", action="store_true", help="仅输出 JSON")
    args = ap.parse_args(argv)

    if args.candles:
        stream, interval_min = _stream_from_candles(args.candles)
        src = f"candles={args.candles} interval={interval_min:g}m"
    else:
        stream = _synthetic_stream(args.sims, args.seed,
                                   drift_bps=args.drift_bps, vol_bps=args.vol_bps)
        src = f"synthetic n={args.sims} drift={args.drift_bps}bp vol={args.vol_bps}bp"

    if not stream:
        print("ERROR: no signals (candles too short?)", file=sys.stderr)
        return 2

    res = evaluate(stream, mc=max(1, args.mc), seed=args.seed,
                   base=args.base, decay=args.decay, kappa=args.kappa)
    res["source"] = src

    if args.json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0

    print("=" * 72)
    print("maker fill model v1 vs v2 — offline replay (no funds)")
    print(f"source: {src} | v2 MC runs={res['v2_mc_runs']} | signals={res['n_signals']}")
    print(f"params: base={args.base} decay={args.decay} kappa={args.kappa} "
          f"offset={OFFSET_BPS}bp ttl={TTL_BARS}m")
    print("-" * 72)
    for name in ("v1", "v2"):
        m = res[name]
        print(f"[{name}] fill_rate={m['fill_rate_pct']}%  "
              f"captured_edge={m['avg_captured_edge_bps']}bp  "
              f"resid_adverse_drift={m['avg_residual_adverse_drift_bps']}bp  "
              f"NET={m['net_maker_advantage_bps']}bp")
    print("-" * 72)
    print(f"v2 runaway/queue cancel ≈ {res['v2_runaway_cancel_pct_of_v1_fills']}% "
          "of v1 fills (the inflated fills v1 books but v2 does not)")
    print("=" * 72)
    print("acceptance direction: v2 fill_rate <= v1 AND v2 NET < v1 NET;")
    print("with kappa=1 v2 NET should sit near 0 (captured edge offsets drift).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
