#!/usr/bin/env python3
"""Personal Trader Quick Backtest — 个人交易者快速回测

极简回测：7日、10币、基础成本模型
移除：多轮分层回测、Walk-Forward OOS、Regime Replay、Tick Confirmation、Memory Calibration

Usage:
    python3 scripts/backtest_quick.py                    # 7天，10币
    python3 scripts/backtest_quick.py --days 14         # 14天
    python3 scripts/backtest_quick.py --coins BTC,ETH   # 指定币种
"""
from __future__ import annotations

import argparse
import os
import sys
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import List

# Mark as backtest before any hermes imports
os.environ["HERMES_BACKTEST"] = "1"

_REPO = Path(__file__).resolve().parents[1]
_env = _REPO / ".env.local"
if _env.is_file():
    for _line in _env.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _, _v = _line.partition("=")
            if _k.strip() == "HYPERLIQUID_PRIVATE_KEY":
                continue
            os.environ.setdefault(_k.strip(), _v.strip())
_config_path = os.environ.get("HERMES_AGENT_CONFIG_FILE", "")
if not _config_path:
    for _candidate in (
        Path("/data/.agent-config.json"),
        Path("/home/ldy/hermes-deploy/.agent-config.json"),
        _REPO / ".agent-config.json",
    ):
        if _candidate.is_file():
            os.environ["HERMES_AGENT_CONFIG_FILE"] = str(_candidate)
            break
sys.path.insert(0, str(_REPO))

from hermes_trader.agents.config_store import cfg_get, read_agent_config
from hermes_trader.agents.dsl_exit import _build_policy_from_config
from hermes_trader.backtest import driver as kdriver
from hermes_trader.backtest import signals as ksig
from hermes_trader.backtest import stats as kstats
from hermes_trader.backtest.cost import CostModel
from hermes_trader.backtest.types import Trade
from hermes_trader.client.hl_client import fetch_hl_candles
from hermes_trader.client.universe import get_universe

# Simple cost model: fee only
ROUND_TRIP_FEE_BPS = 5.0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=int, default=7, help="回测天数（默认7天）")
    ap.add_argument("--coins", default="", help="币种列表（逗号分隔，默认空=top10）")
    ap.add_argument("--coins-count", type=int, default=10, help="币种数量（默认10）")
    ap.add_argument("--equity", type=float, default=100.0, help="初始资金（默认$100）")
    ap.add_argument("--interval", default="1h", choices=["15m", "1h", "4h"], help="周期（默认1h）")
    args = ap.parse_args()

    cfg = read_agent_config()
    equity_fraction = float(cfg_get("equity_fraction_per_trade", 0.10, config=cfg))
    leverage = int(cfg_get("leverage", 5, config=cfg))
    scan_long_only = bool(cfg_get("scan_long_only", True, config=cfg))
    max_loss = float(cfg_get("dsl_exit.max_loss_pct", 1.0, config=cfg))
    protect = float(cfg_get("dsl_exit.protect_pct", 1.5, config=cfg))
    retrace = float(cfg_get("dsl_exit.retrace_threshold", 0.15, config=cfg))

    if args.coins:
        coins = [c.strip().upper() for c in args.coins.split(",") if c.strip()]
    else:
        universe = get_universe()
        perps = [m for m in universe if m["type"] == "perp" and not m["coin"].startswith("@")]
        coins = sorted(perps, key=lambda m: m.get("dayNtlVlm", 0), reverse=True)[:args.coins_count]
        coins = [c["coin"] for c in coins]

    print("=== Quick Backtest ===")
    print(f"period: {args.days} days | interval: {args.interval} | coins: {len(coins)}")
    print(
        f"equity: ${args.equity:.0f} | leverage: {leverage}x | "
        f"long_only: {scan_long_only} | max_loss: {max_loss}% | protect: {protect}%\n"
    )

    bars_per_day = {"15m": 96, "1h": 24, "4h": 6}[args.interval]
    total_bars = args.days * bars_per_day + 100
    bar_ms = {"15m": 900_000, "1h": 3_600_000, "4h": 14_400_000}[args.interval]

    base_policy = replace(
        _build_policy_from_config(),
        max_loss_pct=max_loss,
        protect_pct=protect,
        retrace_threshold=retrace,
    )
    cost = CostModel(round_trip_fee_bps=ROUND_TRIP_FEE_BPS)

    all_trades: List[Trade] = []
    loaded_coins = 0
    total_signals = 0
    data_errors = 0

    for coin in coins:
        try:
            print(f"{coin}: fetching {total_bars} bars...", flush=True)
            candles = fetch_hl_candles(coin, args.interval, total_bars)
            if len(candles) < 110:
                print(f"  {coin:8} skip ({len(candles)} bars)")
                continue
            loaded_coins += 1

            signals = ksig.heuristic_signals(candles, None, bar_ms=bar_ms)
            if scan_long_only:
                signals = [signal for signal in signals if signal.side == "long"]
            total_signals += len(signals)

            trades = kdriver.run(
                candles,
                signals,
                base_policy,
                coin=coin,
                leverage=leverage,
                notional_usd=args.equity * equity_fraction,
                cost=cost,
                bar_ms=bar_ms,
            )
            all_trades.extend(trades)
            print(f"  {coin:8} bars={len(candles)} signals={len(signals)} trades={len(trades)}")

        except Exception as e:
            data_errors += 1
            print(f"  {coin:8} error: {type(e).__name__}: {e}")

    print("\n=== SUMMARY ===")
    print(f"coins loaded  : {loaded_coins}/{len(coins)} | data errors: {data_errors}")
    print(f"signals       : {total_signals}")
    if loaded_coins == 0:
        print("backtest failed: no real market data loaded")
        return 2
    if not all_trades:
        print("no trades fired (data flow passed; performance sample is zero)")
        return 0

    s = kstats.trade_stats(all_trades, equity=args.equity)
    print(f"trades        : {s.n}")
    print(f"win rate      : {s.wins}/{s.n} = {s.win_rate_pct:.1f}%")
    print(f"expectancy    : ${s.expectancy_usd:+.3f} per trade")
    print(f"total pnl     : ${s.pnl_net_usd:+.2f} ({s.pnl_pct_equity:+.2f}%)")
    print(f"max drawdown  : ${s.max_dd_usd:.2f}")
    print(f"\nexits: {s.by_reason}")

    print(f"\n{'time':18} {'coin':7} {'side':5} {'entry':>10} {'exit':>10} {'pnl':>8} {'reason'}")
    print("-" * 80)
    for t in all_trades[:15]:
        ts = datetime.fromtimestamp(t.entry_time_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
        print(
            f"{ts:18} {t.coin:7} {t.side:5} {t.entry_fill_px:>10.4f} "
            f"{t.exit_fill_px:>10.4f} {t.pnl_net_usd:>+8.3f} {t.reason.value}"
        )
    if len(all_trades) > 15:
        print(f"... and {len(all_trades) - 15} more")
    return 0


if __name__ == "__main__":
    sys.exit(main())
