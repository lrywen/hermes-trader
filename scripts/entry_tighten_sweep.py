"""Entry-side tightening sweep on the long-window backtest.

One fetch per (coin, timeframe) — the SAME candles are reused across every
candidate config, so we pay the 429-throttled network cost once. For each
config we run the production-parity kernel (multi-period ignition, full cost),
aggregate trades into a per-day bps series, and report the block-bootstrap 95%
CI alongside win rate / expectancy. Configs that clear a strictly-positive CI
are exported as validate_outcome-compatible trade JSONL so the live gate can
be minted without a second run.

Research tool (not shipped in the runtime image). Pure read/report: never
writes config, flips mode or places orders.
"""
from __future__ import annotations

import argparse
import collections
import json
import statistics
from dataclasses import replace
from typing import Optional

from hermes_trader.agents.dsl_exit import _build_policy_from_config
from hermes_trader.backtest import cost as kcost
from hermes_trader.backtest import driver as kdrv
from hermes_trader.backtest import signals as ksig
from hermes_trader.client.hl_client import fetch_hl_candles
from hermes_trader.validation.significance import DAY_MS, block_bootstrap_ci

# Each candidate: signal-side kwargs (HeuristicConfig), policy overrides
# (ExitPolicy), leverage, and long_only. The drift diagnostic showed longs have
# a real monotonically-rising edge while shorts have none, and that the edge is
# small (~0.2%/8h) so tight stops + high leverage destroy it. Hence the later
# configs go long-only, drop leverage and widen stops / extend holding.
CONFIGS: dict[str, dict] = {
    "baseline": {"sig": {}, "pol": {}, "lev": 10, "long_only": False},
    "longonly": {"sig": {}, "pol": {}, "lev": 10, "long_only": True},
    "lo3x_wide": {
        "sig": {},
        "pol": {"max_loss_pct": 2.5, "hard_timeout_minutes": 720.0,
                "stale_flat_timeout_minutes": 0.0},
        "lev": 3, "long_only": True,
    },
    "lo3x_strong": {
        "sig": {"min_adx": 20.0},
        "pol": {"max_loss_pct": 2.5, "hard_timeout_minutes": 720.0,
                "stale_flat_timeout_minutes": 0.0},
        "lev": 3, "long_only": True,
    },
    "lo5x_bal": {
        "sig": {"min_adx": 18.0},
        "pol": {"max_loss_pct": 1.8, "hard_timeout_minutes": 480.0,
                "stale_flat_timeout_minutes": 0.0},
        "lev": 5, "long_only": True,
    },
}


def day_bps_from_trades(trades) -> list[float]:
    byday: dict[int, list[float]] = collections.defaultdict(list)
    for t in trades:
        if not t.notional_usd:
            continue
        byday[t.entry_time_ms // DAY_MS].append(t.pnl_net_usd / t.notional_usd * 1e4)
    return [statistics.mean(byday[k]) for k in sorted(byday)]


def export_trades(path: str, trades, arm: str) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        for t in trades:
            fh.write(json.dumps({
                "type": "trade", "arm": arm, "coin": t.coin, "side": t.side,
                "entry_t": int(t.entry_time_ms), "exit_t": int(t.exit_time_ms),
                "notional": float(t.notional_usd), "pnl_net": float(t.pnl_net_usd),
                "reason": t.reason.value,
            }) + "\n")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--coins", type=int, default=25)
    ap.add_argument("--days", type=int, default=200)
    ap.add_argument("--boot", type=int, default=5000)
    ap.add_argument("--seed", type=int, default=20260928)
    ap.add_argument("--out-dir", default="/tmp")
    args = ap.parse_args()

    main_bars = args.days * 24
    policy = replace(_build_policy_from_config())
    cm = kcost.CostModel()

    # trades per config, accumulated across coins
    agg: dict[str, list] = {k: [] for k in CONFIGS}

    # Coin universe mirrors scripts/backtest.py ordering (highest open interest).
    universe = _default_universe(args.coins)
    for ci, coin in enumerate(universe, 1):
        try:
            candles = fetch_hl_candles(coin, "1h", main_bars)
        except Exception as e:
            print(f"[{ci}/{len(universe)}] {coin}: 1h unavailable ({e}); skip")
            continue
        if len(candles) < 110:
            print(f"[{ci}/{len(universe)}] {coin}: {len(candles)} bars; skip")
            continue
        # auxiliary timeframes once per coin, shared across configs. Skip 4h:
        # none of these configs uses the late-entry gate and ignition only needs
        # 5m/15m/1h — fetching 4h just triggers throttled quality-gate retries.
        aux: dict[str, object] = {"1h": candles}
        for period in ("15m", "5m"):
            try:
                aux[period] = fetch_hl_candles(coin, period, 5000)
            except Exception:
                aux[period] = None
        ign = {p: s for p, s in aux.items() if s is not None and p in policy.ignite_periods}

        base_cfg = ksig.default_heuristic_config(warmup=100)
        for label, spec in CONFIGS.items():
            cfg = replace(base_cfg, **spec["sig"])
            sigs = ksig.heuristic_signals(candles, cfg, bar_ms=3_600_000)
            if spec["long_only"]:
                sigs = [s for s in sigs if s.side == "long"]
            coin_policy = replace(policy, **spec["pol"])
            trades = kdrv.run(
                candles, sigs, coin_policy, coin=coin, leverage=spec["lev"],
                notional_usd=200.0, cost=cm, bar_ms=3_600_000,
                confirm_mode="bar", ignition_series=ign,
            )
            agg[label].extend(trades)
        print(f"[{ci}/{len(universe)}] {coin}: {len(candles)} bars")

    # ── report ──
    print("\n=== entry-tightening sweep ===")
    header = f"{'config':12} {'trades':>7} {'days':>5} {'win%':>6} {'meanbps':>9} {'CI lo':>8} {'CI hi':>8}  pass"
    print(header)
    for label in CONFIGS:
        trades = agg[label]
        if not trades:
            print(f"{label:12} {'0':>7}  no trades")
            continue
        wins = sum(1 for t in trades if t.pnl_net_usd > 0)
        win_pct = wins / len(trades) * 100
        day_bps = day_bps_from_trades(trades)
        if len(day_bps) >= 2:
            lo, hi = block_bootstrap_ci(day_bps, boot=args.boot, seed=args.seed)
        else:
            lo = hi = float("nan")
        mean_bps = statistics.mean(day_bps) if day_bps else float("nan")
        passed = lo > 0
        print(f"{label:12} {len(trades):>7} {len(day_bps):>5} {win_pct:>6.1f} "
              f"{mean_bps:>9.2f} {lo:>8.2f} {hi:>8.2f}  {'YES' if passed else ''}")
        if passed:
            path = f"{args.out_dir}/entry_tighten_{label}.jsonl"
            export_trades(path, trades, "filt")
            print(f"    -> exported positive-CI trades to {path}")
    return 0


def _default_universe(n: int):
    from hermes_trader.client.universe import get_universe
    universe = get_universe()
    perps = [m for m in universe if m["type"] == "perp" and not m["coin"].startswith("@")]
    ranked = sorted(perps, key=lambda m: m.get("dayNtlVlm", 0), reverse=True)
    coins = [m["coin"] for m in ranked[:n]]
    if not coins:
        coins = ["BTC", "ETH", "SOL", "XRP", "DOGE", "SUI", "BNB", "TRX",
                 "LINK", "AVAX", "TON", "SEI", "LTC", "NEAR", "ADA"]
    return coins


if __name__ == "__main__":
    raise SystemExit(main())
