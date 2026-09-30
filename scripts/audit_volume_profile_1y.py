#!/usr/bin/env python3
"""Volume Profile 近1年事件研究回测。

补齐总报告遗漏：用 Binance 现货近1年5m数据，在每个抽样点按生产参数
计算 Volume Profile，按价格在价值区的位置定义反向/回归信号，前向评级：

  * near_value_area_low  (position<=20)：价值区低点，按做多方向；
  * near_value_area_high (position>=80)：价值区高点，按做空方向；
  * outside_value_area   (突破VAH/VAL)：破位方向，检验"追价值区外沿"。

判别口径与闸门审计一致：看信号组相对基线（全部点）的 EV/中位，且
train(前75%)/test(后25%) 两段符号需一致。

数据复用 audit_gates_1y 的 Binance 缓存；VP 参数从 CANONICAL_DEFAULTS
的 launch_capture.volume_profile 派生，保证与生产一致。现货-永续基差：
结论用于方向/阈值判断，非精确盈亏。

用法：
    python3 scripts/audit_volume_profile_1y.py --days 365 \
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

from hermes_trader.indicators.volume_profile import volume_profile
from hermes_trader.models.types import Candle

from scripts.audit_gates_1y import (
    ROUND_TRIP_FEE_BPS,
    TEST_FRACTION,
    WARMUP_BARS,
    fetch_binance,
)

LOOKBACK_BARS = 48
HORIZON_BARS = 12
SAMPLE_EVERY = 12

LOW_PCT, HIGH_PCT = 20.0, 80.0


def _vp_params() -> dict[str, Any]:
    """从 CANONICAL_DEFAULTS 派生生产 Volume Profile 参数。"""
    from hermes_trader.agents.config_store import CANONICAL_DEFAULTS as _CD

    vp = (_CD.get("launch_capture") or {}).get("volume_profile") or {}
    return {
        "bins": int(vp.get("bins", 50)),
        "atr_bins": bool(vp.get("atr_bins", True)),
        "atr_period": int(vp.get("atr_period", 14)),
        "atr_multiple": float(vp.get("atr_multiple", 0.25)),
    }


def _to_candles(rows: list[list[float]]) -> list[Candle]:
    return [Candle(t=r[0], o=r[1], h=r[2], l=r[3], c=r[4], v=r[5])
            for r in rows]


def _classify(vp, price: float) -> Optional[tuple[str, int]]:
    """返回（信号名, 方向）。价格在价值区位置驱动。"""
    pos = vp.position_pct(price)
    if pos is None:
        return None
    if pos <= LOW_PCT:
        return ("near_value_area_low", 1)
    if pos >= HIGH_PCT:
        return ("near_value_area_high", -1)
    return None


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


SIGNALS = ["near_value_area_low", "near_value_area_high"]


def run(*, days: int, coins: list[str], horizon: int,
        write: bool, out: str) -> dict[str, Any]:
    as_of = int(time.time() * 1000)
    step5m = 5 * 60_000
    start5m = as_of - days * 86_400_000 - WARMUP_BARS * step5m
    vp_p = _vp_params()

    rows_out: list[dict[str, Any]] = []
    for ci, coin in enumerate(coins):
        t0 = time.time()
        raw5m = fetch_binance(coin, "5m", start5m, as_of, step5m)
        if len(raw5m) < WARMUP_BARS + horizon + 10:
            print(f"[{ci+1}/{len(coins)}] {coin}: 5m不足，跳过")
            continue
        win_start = as_of - days * 86_400_000
        first_i = WARMUP_BARS + ((-WARMUP_BARS) % SAMPLE_EVERY)
        nc = 0
        for i in range(first_i, len(raw5m) - horizon, SAMPLE_EVERY):
            if raw5m[i][0] < win_start:
                continue
            window_rows = raw5m[max(0, i - LOOKBACK_BARS + 1):i + 1]
            vp = volume_profile(
                _to_candles(window_rows),
                bins=vp_p["bins"],
                atr_bins=vp_p["atr_bins"],
                atr_period=vp_p["atr_period"],
                atr_multiple=vp_p["atr_multiple"],
            )
            if vp is None:
                continue
            cls = _classify(vp, raw5m[i][4])
            if cls is None:
                continue
            signal, side = cls
            ret = _forward(raw5m, i, side, horizon)
            if ret is None:
                continue
            rows_out.append({
                "t": raw5m[i][0], "coin": coin,
                "signal": signal, "ret": ret,
                "poc": vp.poc, "vah": vp.vah, "val": vp.val,
            })
            nc += 1
        print(f"[{ci+1}/{len(coins)}] {coin}: {nc}信号点 ({time.time()-t0:.0f}s)")

    if not rows_out:
        raise SystemExit("无有效数据点")

    tmin = min(r["t"] for r in rows_out)
    tmax = max(r["t"] for r in rows_out)
    test_start = tmax - (tmax - tmin) * TEST_FRACTION

    # 基线：全部点的做多方向收益（用于比较信号是否带来增量）。
    def table(segment: list[dict[str, Any]]) -> dict[str, Any]:
        return {sig: _stats([r["ret"] for r in segment
                             if r["signal"] == sig])
                for sig in SIGNALS}

    train = [r for r in rows_out if r["t"] < test_start]
    test = [r for r in rows_out if r["t"] >= test_start]
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "days": days, "coins": coins,
        "lookback_5m_bars": LOOKBACK_BARS, "horizon_5m_bars": horizon,
        "data_source": "Binance spot (HL perp basis/funding deviation)",
        "vp_params": vp_p,
        "n_signal_points": len(rows_out),
        "window": {
            "start": datetime.fromtimestamp(tmin / 1000, timezone.utc).date().isoformat(),
            "end": datetime.fromtimestamp(tmax / 1000, timezone.utc).date().isoformat(),
        },
        "signals": {
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
    ap.add_argument("--out", default="/tmp/vp_audit_1y.json")
    ap.add_argument("--write", action="store_true")
    args = ap.parse_args()
    coins = [c.strip() for c in args.coins.split(",") if c.strip()]
    rep = run(days=args.days, coins=coins, horizon=args.horizon,
              write=args.write, out=args.out)
    print(f"\n=== Volume Profile 审计 {args.days}d {coins} "
          f"共{rep['n_signal_points']}信号点 ===")
    for seg in ("train", "test"):
        print(f"\n-- {seg} --")
        for sig, s in rep["signals"][seg].items():
            print(f"  {sig:<22} n={s.get('n',0):>5} ev={s.get('ev_pct')}%"
                  f" wr={s.get('win_rate')} med={s.get('median_pct')}%")
    print(f"\n报告{'已写入 ' + args.out if args.write else '（dry-run；--write保存）'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
