#!/usr/bin/env python3
"""Volume Profile 与 CVD 信号的历史数据验证。

口径说明：
  * 使用 historical_candles 的 point-in-time 窗口，避免未来函数；
  * Volume Profile 使用真实历史 OHLCV 计算；
  * Hyperliquid 公共 trades 没有可回放历史接口，因此 CVD 使用 OHLCV
    近似买卖压力，不等同真实逐笔 taker CVD；
  * 默认只打印汇总，不写文件。
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

os.environ.setdefault("HERMES_BACKTEST", "1")
_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO))

from hermes_trader.agents.microstructure import detect_cvd_divergence
from hermes_trader.client.universe import get_universe
from hermes_trader.data import historical_candles as hc
from hermes_trader.indicators.volume_profile import volume_profile
from hermes_trader.models.types import Candle

ROUND_TRIP_FEE_BPS = 5.0


def select_coins(coins: int, explicit: Optional[list[str]] = None) -> list[str]:
    if explicit:
        return [c.strip().upper() for c in explicit if c.strip()]
    universe = get_universe()
    perps = [
        m for m in universe
        if m.get("type") == "perp" and not str(m.get("coin", "")).startswith("@")
    ]
    ranked = sorted(perps, key=lambda m: m.get("dayNtlVlm", 0) or 0, reverse=True)
    return [str(m["coin"]) for m in ranked[:coins]]


def approximate_cvd(bars: list[Candle]) -> list[tuple[float, float]]:
    """用收盘位置和成交量构造 OHLCV 近似 CVD。"""
    cvd = 0.0
    out: list[tuple[float, float]] = []
    for b in bars:
        rng = b.h - b.l
        if rng > 0:
            close_pos = (b.c - b.l) / rng
            signed = b.v * (close_pos * 2.0 - 1.0)
        else:
            signed = 0.0
        cvd += signed
        out.append((float(b.t), cvd))
    return out


def _future_returns(bars: list[Candle], i: int, horizon: int,
                    direction: int) -> Optional[tuple[float, float]]:
    if i + horizon >= len(bars):
        return None
    entry = bars[i].c
    exit_px = bars[i + horizon].c
    fee_pct = ROUND_TRIP_FEE_BPS / 10000.0
    raw_return = (exit_px - entry) / entry
    strategy_return = direction * raw_return - fee_pct
    return raw_return, strategy_return


def validate_coin(
    coin: str,
    *,
    interval: str,
    start_ms: int,
    end_ms: int,
    lookback: int,
    horizon: int,
    bins: int,
    use_cache: bool,
) -> dict[str, Any]:
    bars = hc.closed_bars_as_of(coin, interval, start_ms, end_ms,
                                use_cache=use_cache)
    records: list[dict[str, Any]] = []
    if len(bars) <= lookback + horizon:
        return {"coin": coin, "bars": len(bars), "records": records}

    prev_signal_at: Optional[int] = None
    for i in range(lookback, len(bars) - horizon):
        window = bars[i - lookback:i + 1]
        vp = volume_profile(window, bins=bins)
        if vp is None:
            continue
        approx_cvd = approximate_cvd(window)
        price_points = [(float(b.t), float(b.c)) for b in window]
        divergence = detect_cvd_divergence(price_points, approx_cvd)
        signal = None
        direction = 0
        divergence_strength_pct = 0.0
        if divergence.bearish:
            signal, direction = "bearish_cvd_divergence", -1
            divergence_strength_pct = divergence.strength_pct
        elif divergence.bullish:
            signal, direction = "bullish_cvd_divergence", 1
            divergence_strength_pct = divergence.strength_pct
        else:
            pos = vp.position_pct(bars[i].c)
            if pos is not None:
                if pos <= 20.0:
                    signal, direction = "near_value_area_low", 1
                elif pos >= 80.0:
                    signal, direction = "near_value_area_high", -1
        if signal is None:
            continue
        # 同一币种同一类信号至少间隔 lookback，降低重复样本相关性。
        if prev_signal_at is not None and bars[i].t - prev_signal_at < lookback * hc.INTERVAL_MS[interval]:
            continue
        returns = _future_returns(bars, i, horizon, direction)
        if returns is None:
            continue
        raw_return, strategy_return = returns
        prev_signal_at = bars[i].t
        records.append({
            "coin": coin,
            "ts": bars[i].t,
            "signal": signal,
            "expected_direction": "short" if direction < 0 else "long",
            "horizon_bars": horizon,
            "raw_price_return_pct": round(raw_return * 100.0, 4),
            "return_pct": round(strategy_return * 100.0, 4),
            "win": strategy_return > 0,
            "poc": vp.poc,
            "vah": vp.vah,
            "val": vp.val,
            "divergence_strength_pct": divergence_strength_pct,
            "cvd_source": "ohlcv_approx",
        })
    return {"coin": coin, "bars": len(bars), "records": records}


def summarize(records: list[dict[str, Any]]) -> dict[str, Any]:
    by_signal: dict[str, list[dict[str, Any]]] = {}
    for r in records:
        by_signal.setdefault(r["signal"], []).append(r)

    summary: dict[str, Any] = {}
    for signal, rows in sorted(by_signal.items()):
        returns = [float(r["return_pct"]) for r in rows]
        wins = [bool(r["win"]) for r in rows]
        summary[signal] = {
            "samples": len(rows),
            "win_rate_pct": round(100.0 * sum(wins) / len(wins), 2),
            "mean_return_pct": round(statistics.fmean(returns), 4),
            "median_return_pct": round(statistics.median(returns), 4),
        }
    return summary


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=float, default=30)
    ap.add_argument("--interval", default="5m", choices=sorted(hc.INTERVAL_MS.keys()))
    ap.add_argument("--coins", type=int, default=10)
    ap.add_argument("--coin", nargs="+", default=None)
    ap.add_argument("--lookback", type=int, default=48)
    ap.add_argument("--horizon", type=int, default=12)
    ap.add_argument("--bins", type=int, default=50)
    ap.add_argument("--out", default=None)
    ap.add_argument("--no-cache", action="store_true")
    ap.add_argument("--sleep", type=float, default=0.0)
    return ap.parse_args()


def main() -> int:
    args = parse_args()
    end_ms = int(time.time() * 1000)
    start_ms = end_ms - int(args.days * 86_400_000)
    coins = select_coins(args.coins, args.coin)
    all_records: list[dict[str, Any]] = []
    per_coin: list[dict[str, Any]] = []

    for coin in coins:
        result = validate_coin(
            coin,
            interval=args.interval,
            start_ms=start_ms,
            end_ms=end_ms,
            lookback=args.lookback,
            horizon=args.horizon,
            bins=args.bins,
            use_cache=not args.no_cache,
        )
        per_coin.append({"coin": coin, "bars": result["bars"],
                         "samples": len(result["records"])})
        all_records.extend(result["records"])
        if args.sleep:
            time.sleep(args.sleep)

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "interval": args.interval,
        "days": args.days,
        "lookback_bars": args.lookback,
        "horizon_bars": args.horizon,
        "cvd_source": "ohlcv_approx_not_taker_trades",
        "per_coin": per_coin,
        "summary": summarize(all_records),
        "records": all_records,
    }
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    if args.out:
        Path(args.out).write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
