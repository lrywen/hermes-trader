#!/usr/bin/env python3
"""震荡市(chop)低吸策略近1年验证。

假设：在无方向震荡市，价格回到近期 Volume Profile 价值区下沿(VAL)且短期
RSI 超卖时做多（区间下沿回归），比在涨高后追多更合理。本脚本用近1年
Binance K线检验该组合是否真有 edge，并对比"只看RSI超卖""只看VAL"两个
单因子，判断 edge 来自哪里。

PIT：每点只用已收盘K线；5m每小时抽样；持有多档 horizon；扣5bps。
regime 用 EMA8 vs EMA21 近似（chop=二者缠绕/无持续方向），不依赖生产
外部状态，保证可长窗口回放。

用法：
    python3 scripts/backtest_chop_buythedip.py --days 365 \
        --coins BTC,ETH,SOL,XRP,DOGE --write
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

import importlib.util

_spec = importlib.util.spec_from_file_location(
    "aud", str(_REPO / "scripts" / "audit_gates_1y.py"))
aud = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(aud)

from hermes_trader.indicators.volume_profile import volume_profile

FEE = 5.0 / 10000.0
VP_LOOKBACK = 48          # 用近48根5m构建profile（约4h）
VAL_TOL_PCT = 25.0        # position_pct <= 25 视为靠近VAL
RSI_OS = 35.0             # RSI5m <= 35 视为短期超卖
HORIZONS = (6, 12, 24)    # 30m / 1h / 2h
SAMPLE_EVERY = 12
TEST_FRACTION = 0.25


def is_chop(window) -> bool:
    """近似 chop：EMA8 与 EMA21 差距很小（<0.3 ATR），无持续方向。"""
    from hermes_trader.indicators.math import ema
    if len(window) < 35:
        return False
    atr = aud._atr(window)
    e8 = ema([b.c for b in window], 8)
    e21 = ema([b.c for b in window], 21)
    if not atr or atr <= 0 or not e8 or not e21:
        return False
    return abs(e8[-1] - e21[-1]) / atr < 0.5


def signal_flags(bars, i):
    """返回 (chop, near_val, rsi_os, combined)。"""
    window = bars[max(0, i - 99):i + 1]
    chop = is_chop(window)
    rsi = aud._rsi(window)
    rsi_os = bool(rsi is not None and rsi <= RSI_OS)

    near_val = False
    vp_window = bars[max(0, i - VP_LOOKBACK + 1):i + 1]
    if len(vp_window) >= 10:
        vp = volume_profile(vp_window, bins=30)
        if vp is not None:
            pos = vp.position_pct(bars[i].c)
            near_val = bool(pos is not None and pos <= VAL_TOL_PCT)

    combined = chop and near_val and rsi_os
    return chop, near_val, rsi_os, combined


def forward(bars, i, side, horizon):
    j = i + horizon
    if j >= len(bars):
        return None
    e = bars[i].c
    if e <= 0:
        return None
    return side * (bars[j].c - e) / e - FEE


def stats(vals):
    n = len(vals)
    if not n:
        return {"n": 0}
    wins = sum(1 for v in vals if v > 0)
    return {"n": n, "win_rate": round(wins / n, 4),
            "ev_pct": round(sum(vals) / n * 100, 4),
            "median_pct": round(sorted(vals)[n // 2] * 100, 4)}


SETS = {
    "combined_chop_val_rsi": lambda f: f[3],
    "near_val_only": lambda f: f[1],
    "rsi_os_only": lambda f: f[2],
    "chop_and_rsi": lambda f: f[0] and f[2],
}


def run(*, days, coins, write, out):
    as_of = int(time.time() * 1000)
    step5m = 300_000
    start = as_of - (days * 86_400_000) - 120 * step5m
    # results[name][horizon] = list[float]；并保留时间戳供切分
    raw = {name: {h: [] for h in HORIZONS} for name in SETS}
    ts_rows = []

    for ci, coin in enumerate(coins):
        t0 = time.time()
        rawc = aud.fetch_binance(coin, "5m", start, as_of, step5m)
        bars = [aud._B(r) for r in rawc]
        win_start = as_of - days * 86_400_000
        nc = 0
        for i in range(120, len(bars) - max(HORIZONS), SAMPLE_EVERY):
            if bars[i].t < win_start:
                continue
            flags = signal_flags(bars, i)
            rets = {h: forward(bars, i, 1, h) for h in HORIZONS}
            if any(v is None for v in rets.values()):
                continue
            ts_rows.append(bars[i].t)
            for name, pred in SETS.items():
                if pred(flags):
                    for h in HORIZONS:
                        raw[name][h].append((bars[i].t, rets[h]))
            if flags[3]:
                nc += 1
        print(f"[{ci+1}/{len(coins)}] {coin}: combined信号{nc} "
              f"({time.time()-t0:.0f}s)")

    if not ts_rows:
        raise SystemExit("无数据")
    tmin, tmax = min(ts_rows), max(ts_rows)
    test_start = tmax - (tmax - tmin) * TEST_FRACTION

    def seg_table(items, t_lo, t_hi):
        vals = [v for t, v in items if t_lo <= t < t_hi]
        return stats(vals)

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "days": days, "coins": coins,
        "params": {"vp_lookback": VP_LOOKBACK, "val_tol_pct": VAL_TOL_PCT,
                   "rsi_os": RSI_OS, "horizons": HORIZONS},
        "window": {"start": datetime.fromtimestamp(tmin/1000, timezone.utc).date().isoformat(),
                   "end": datetime.fromtimestamp(tmax/1000, timezone.utc).date().isoformat()},
        "results": {},
    }
    for name in SETS:
        report["results"][name] = {
            "all": {str(h): seg_table(raw[name][h], tmin, tmax + 1)
                    for h in HORIZONS},
            "train": {str(h): seg_table(raw[name][h], tmin, test_start)
                      for h in HORIZONS},
            "test": {str(h): seg_table(raw[name][h], test_start, tmax + 1)
                     for h in HORIZONS},
        }
    if write:
        tmp = out + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
        os.replace(tmp, out)
    return report


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=int, default=365)
    ap.add_argument("--coins", default="BTC,ETH,SOL,XRP,DOGE")
    ap.add_argument("--out", default="/tmp/chop_dip_1y.json")
    ap.add_argument("--write", action="store_true")
    args = ap.parse_args()
    coins = [c.strip() for c in args.coins.split(",") if c.strip()]
    rep = run(days=args.days, coins=coins, write=args.write, out=args.out)
    print(f"\n=== chop低吸 {args.days}d ===")
    for name, segs in rep["results"].items():
        print(f"\n● {name}")
        for seg in ("train", "test"):
            cells = segs[seg]
            line = "  ".join(
                f"{h}h: n={cells[h]['n']} wr={cells[h].get('win_rate')} "
                f"ev={cells[h].get('ev_pct')}%" for h in ("6", "12", "24"))
            print(f"  [{seg}] {line}")
    print(f"\n报告{'已写 ' + args.out if args.write else '(dry-run)'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
