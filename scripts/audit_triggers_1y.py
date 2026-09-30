#!/usr/bin/env python3
"""近1年 trigger 层指标事件研究回测（可复现部分）。

audit_gates_1y.py 审计的是"闸门拦截是否合理"；本脚本审计的是更上游的
perception triggers：每个 trigger 触发后，按其自身方向（up→做多 /
down→做空）持有固定 horizon，统计期望收益。用于判断：

  * 哪些 trigger 单独具备正期望（值得保留/加权）；
  * 哪些 trigger 长期负期望或仅观测价值（应降权/清理）；
  * 触发频率是否过密（指标冗余的旁证）。

数据复用 audit_gates_1y 的 Binance 现货缓存（HL 5m 仅~17天），不重复
拉数。在 5m 收盘点把真实 Candle 喂给生产 trigger 函数，保证口径与生产
一致。参数全部从 CANONICAL_DEFAULTS 的 trigger_thresholds 派生。
现货-永续基差/资金费偏差：结论用于方向/阈值判断，非精确盈亏。

用法：
    python3 scripts/audit_triggers_1y.py --days 365 \
        --coins BTC,ETH,SOL,DOGE,AVAX --write
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

os.environ.setdefault("HERMES_BACKTEST", "1")
_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO))

from hermes_trader.indicators import triggers as trigger_mod
from hermes_trader.models.types import Candle

# 复用闸门审计脚本的拉数与缓存。
from scripts.audit_gates_1y import (
    ROUND_TRIP_FEE_BPS,
    TEST_FRACTION,
    WARMUP_BARS,
    fetch_binance,
)

HORIZON_BARS = 12
SAMPLE_EVERY = 12


def _thresholds() -> dict[str, Any]:
    """从 CANONICAL_DEFAULTS 派生生产 trigger 阈值（snake→camel 映射）。"""
    from hermes_trader.agents.config_store import CANONICAL_DEFAULTS as _CD

    t = _CD["trigger_thresholds"]
    return {
        "sigmaThreshold": t["sigma_threshold"],
        "breakoutLookback": t["breakout_lookback"],
        "breakoutMinRvol": t["breakout_min_rvol"],
        "breakoutRvolWindow": t["breakout_rvol_window"],
        "breakoutAtrScoreMult": t["breakout_atr_score_mult"],
        "breakoutConfirmBars": t["breakout_confirm_bars"],
        "bbLength": t["bb_length"],
        "bbStdDev": t["bb_std_dev"],
        "adxPeriod": t["adx_period"],
        "momentumLookback": t["momentum_lookback"],
        "momentumPct": t["momentum_pct"],
        "volBuildupRatio": t["vol_buildup_ratio"],
        "trendFlipBars": t["trend_flip_bars"],
        "higherLowsRequired": t["higher_lows_required"],
        "trendMomentumLookback": t["trend_momentum_lookback"],
        "trendMomentumPct": t["trend_momentum_pct"],
    }


def _to_candles(rows: list[list[float]]) -> list[Candle]:
    return [Candle(t=r[0], o=r[1], h=r[2], l=r[3], c=r[4], v=r[5])
            for r in rows]


# (trigger名, 产出方向键)。None 表示该 trigger 不区分方向（结构性）。
def _run_all_hits(c5m: list[Candle], c1h: list[Candle],
                  th: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        trigger_mod.pct_move_spike(c5m, th["sigmaThreshold"]),
        trigger_mod.volume_spike(c5m, th["sigmaThreshold"]),
        trigger_mod.breakout(
            c5m, th["breakoutLookback"],
            min_rvol=th["breakoutMinRvol"],
            rvol_window=th["breakoutRvolWindow"],
            atr_score_mult=th["breakoutAtrScoreMult"],
            confirm_bars=th["breakoutConfirmBars"],
        ),
        trigger_mod.range_compression(c5m, th["bbLength"], th["bbStdDev"]),
        trigger_mod.trend_strength(c5m, th["adxPeriod"]),
        trigger_mod.momentum_burst(c5m, th["momentumLookback"], th["momentumPct"]),
        trigger_mod.volume_buildup_1h(c1h, th["volBuildupRatio"]),
        trigger_mod.trend_flip_1h(c1h, th["trendFlipBars"]),
        trigger_mod.higher_lows_1h(c1h, th["higherLowsRequired"]),
        trigger_mod.uptrend_momentum(c5m, th["trendMomentumLookback"],
                                     th["trendMomentumPct"]),
        trigger_mod.downtrend_momentum(c5m, th["trendMomentumLookback"],
                                       th["trendMomentumPct"]),
    ]


# trigger 名 → 方向来源："direction" 用 hit 的 direction 字段；
# "up"/"down" 表示该 trigger 名义方向固定（结构多头/空头）。
_DIR_SOURCE = {
    "pctMoveSpike": "direction",
    "volumeSpike": "up",          # 量能本身无方向，按多头基准（仅评EV符号参考）
    "breakout": "hit",            # 由 reason 推断上下
    "rangeCompression": "up",
    "trendStrength": "up",
    "momentumBurst": "direction",
    "volumeBuildup1h": "up",
    "trendFlip1h": "up",
    "higherLows1h": "up",
    "uptrendMomentum": "up",
    "downtrendMomentum": "down",
}


def _hit_direction(hit: dict[str, Any]) -> int:
    name = hit["name"]
    src = _DIR_SOURCE.get(name, "up")
    if src == "direction":
        return 1 if hit.get("direction") != "down" else -1
    if src == "hit":
        return -1 if "below" in str(hit.get("reason", "")) else 1
    return 1 if src == "up" else -1


def _forward(rows: list[list[float]], i: int, side: int,
             horizon: int) -> Optional[float]:
    exit_i = i + horizon
    if exit_i >= len(rows):
        return None
    entry = rows[i][4]
    if entry <= 0:
        return None
    fee = ROUND_TRIP_FEE_BPS / 10000.0
    return side * (rows[exit_i][4] - entry) / entry - fee


def _stats(vals: list[float]) -> dict[str, Any]:
    n = len(vals)
    if not n:
        return {"n": 0}
    wins = sum(1 for v in vals if v > 0)
    return {
        "n": n,
        "win_rate": round(wins / n, 4),
        "ev_pct": round(sum(vals) / n * 100, 4),
        "median_pct": round(sorted(vals)[n // 2] * 100, 4),
    }


NAMES = list(_DIR_SOURCE)


def run(*, days: int, coins: list[str], horizon: int,
        write: bool, out: str) -> dict[str, Any]:
    as_of = int(time.time() * 1000)
    step5m, step1h = 5 * 60_000, 60 * 60_000
    start5m = as_of - days * 86_400_000 - WARMUP_BARS * step5m
    start1h = as_of - days * 86_400_000 - 200 * step1h
    th = _thresholds()

    rows_out: list[dict[str, Any]] = []
    for ci, coin in enumerate(coins):
        t0 = time.time()
        raw5m = fetch_binance(coin, "5m", start5m, as_of, step5m)
        raw1h = fetch_binance(coin, "1h", start1h, as_of, step1h)
        if len(raw5m) < WARMUP_BARS + horizon + 10:
            print(f"[{ci+1}/{len(coins)}] {coin}: 5m不足，跳过")
            continue
        win_start = as_of - days * 86_400_000
        first_i = WARMUP_BARS + ((-WARMUP_BARS) % SAMPLE_EVERY)
        nc = 0
        for i in range(first_i, len(raw5m) - horizon, SAMPLE_EVERY):
            if raw5m[i][0] < win_start:
                continue
            cutoff = raw5m[i][0]
            w5m_rows = raw5m[max(0, i - 149):i + 1]
            j1h = sum(1 for r in raw1h if r[0] <= cutoff - step1h)
            w1h_rows = raw1h[max(0, j1h - 47):j1h]
            hits = _run_all_hits(_to_candles(w5m_rows), _to_candles(w1h_rows), th)
            fired = {h["name"]: h for h in hits if h.get("fired")}
            rec = {"t": raw5m[i][0], "coin": coin, "fired": list(fired)}
            ok = True
            for name, hit in fired.items():
                side = _hit_direction(hit)
                ret = _forward(raw5m, i, side, horizon)
                if ret is None:
                    ok = False
                    break
                rec[name] = ret
            if not ok:
                continue
            rows_out.append(rec)
            nc += 1
        print(f"[{ci+1}/{len(coins)}] {coin}: {nc}点 ({time.time()-t0:.0f}s)")

    if not rows_out:
        raise SystemExit("无有效数据点")

    tmin = min(r["t"] for r in rows_out)
    tmax = max(r["t"] for r in rows_out)
    test_start = tmax - (tmax - tmin) * TEST_FRACTION

    def table(segment: list[dict[str, Any]]) -> dict[str, Any]:
        out_t: dict[str, Any] = {}
        for name in NAMES:
            vals = [r[name] for r in segment if name in r]
            fired_n = len(vals)
            rate = round(fired_n / len(segment), 4) if segment else 0
            s = _stats(vals)
            s["fire_rate"] = rate
            out_t[name] = s
        return out_t

    train = [r for r in rows_out if r["t"] < test_start]
    test = [r for r in rows_out if r["t"] >= test_start]
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "days": days, "coins": coins, "horizon_5m_bars": horizon,
        "data_source": "Binance spot (HL perp basis/funding deviation)",
        "thresholds": th,
        "n_points": len(rows_out),
        "window": {
            "start": datetime.fromtimestamp(tmin / 1000, timezone.utc).date().isoformat(),
            "end": datetime.fromtimestamp(tmax / 1000, timezone.utc).date().isoformat(),
        },
        "triggers": {
            "all": table(rows_out),
            "train": table(train),
            "test": table(test),
        },
    }
    if write:
        tmp = out + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
        os.replace(tmp, out)
    report["_written"] = write
    return report


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=int, default=365)
    ap.add_argument("--coins", default="BTC,ETH,SOL")
    ap.add_argument("--horizon", type=int, default=HORIZON_BARS)
    ap.add_argument("--out", default="/tmp/trigger_audit_1y.json")
    ap.add_argument("--write", action="store_true")
    args = ap.parse_args()
    coins = [c.strip() for c in args.coins.split(",") if c.strip()]
    rep = run(days=args.days, coins=coins, horizon=args.horizon,
              write=args.write, out=args.out)
    print(f"\n=== trigger 审计 {args.days}d {coins} 共{rep['n_points']}点 ===")
    for seg in ("train", "test"):
        print(f"\n-- {seg} --")
        for name, s in rep["triggers"][seg].items():
            print(f"  {name:<18} n={s.get('n',0):>5} fire={s.get('fire_rate')}"
                  f" ev={s.get('ev_pct')}% wr={s.get('win_rate')}")
    print(f"\n报告{'已写入 ' + args.out if args.write else '（dry-run；--write保存）'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
