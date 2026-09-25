#!/usr/bin/env python3
"""Backtest the hermes-trader strategy on historical Hyperliquid candles.

Thin CLI over the unified P4 backtest kernel (``hermes_trader.backtest``):
fetches candles per coin, scans the same trigger + TA-confirm logic the live
scanner uses (``backtest.signals.heuristic_signals`` — the AI verdict is
substituted with a deterministic rule, since replaying an LLM per historical
bar would be too expensive), enforces the live late-entry gate, then runs the
PRODUCTION two-phase trailing-stop engine (``DSLTracker`` via the
``DslBarExit`` bar adapter) with the H-7 cost model (``CostModel``).

This script owns only the research workflow concerns: CLI parsing, candle
fetching, universe selection, per-coin memory calibration of fees/slippage,
the late-entry veto, and reporting. Signal/PIT/exit/statistics semantics live
in the kernel and are shared with backtest_logged.py and the tests.

Usage:
    python3 scripts/backtest.py                    # defaults: 14 days, 20 coins
    python3 scripts/backtest.py --days 30 --coins 30
    python3 scripts/backtest.py --equity 200 --interval 1h
"""
from __future__ import annotations

import argparse
import bisect
import math
import os
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any, Dict, List, Optional

# P3-17: mark this process as a backtest BEFORE importing any hermes_trader
# client modules, so exchange._make_exchange() refuses to load a live mainnet
# private key into the simulation process.
os.environ["HERMES_BACKTEST"] = "1"

# load .env.local (HL is public; we just want the same module imports working)
_REPO = Path(__file__).resolve().parents[1]
_env = _REPO / ".env.local"
if _env.is_file():
    for _line in _env.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _, _v = _line.partition("=")
            # Never let .env.local inject a live signing key into a backtest.
            if _k.strip() == "HYPERLIQUID_PRIVATE_KEY":
                continue
            os.environ.setdefault(_k.strip(), _v.strip())
sys.path.insert(0, str(_REPO))

from hermes_trader.agents.config import get_config
from hermes_trader.agents.config_store import cfg_get, read_agent_config
from hermes_trader.agents.dsl_exit import _build_policy_from_config
from hermes_trader.agents.ta_filter import late_entry_check
from hermes_trader.backtest import cost as kcost
from hermes_trader.backtest import driver as kdriver
from hermes_trader.backtest import guard as kguard
from hermes_trader.backtest import signals as ksig
from hermes_trader.backtest import stats as kstats
from hermes_trader.backtest.types import Trade
from hermes_trader.client.hl_client import fetch_hl_candles
from hermes_trader.client.universe import get_universe
from hermes_trader.models.types import Candle

# Interval → candle duration in ms (mirrors hl_client._MS_PER_CANDLE).
_MS_PER: Dict[str, int] = {
    "5m": 5 * 60_000, "15m": 15 * 60_000, "1h": 60 * 60_000,
    "4h": 4 * 3_600_000, "1d": 24 * 3_600_000,
}


def _closed_slice(series: Optional[List[Candle]], ts_ms: List[int],
                  decision_ms: int, tf_ms: int) -> Optional[List[Candle]]:
    """Return the prefix of a higher-timeframe series that is FULLY CLOSED at
    the decision instant (bar open time + duration <= decision time).

    Entry decisions in this engine are made on bar i's close and filled at
    bar i+1's open; ``decision_ms`` = bar i open + one sim-bar duration. A
    higher-TF bar is usable only if it has closed by then — anything still in
    progress would leak future price action (look-ahead). Matches the live
    gate, which at order time sees only completed candles.
    """
    if not series:
        return None
    cutoff = decision_ms - tf_ms  # latest higher-TF bar OPEN time fully closed
    j = bisect.bisect_right(ts_ms, cutoff)
    return series[:j] if j > 0 else None

# Hyperliquid perp taker fee model used by the live executor: 2.5 bps per side.
ROUND_TRIP_FEE_BPS = 5.0

# H-7 (supplemental audit 2026-08-30): the old model charged ONLY the 5 bps
# round-trip fee and filled stops intra-bar at the exact stop price, while the
# live system (a) pays adverse slippage on every IOC entry/exit (live caps:
# 1.5% open / 5.0% close, exchange.py max_slippage_*), and (b) never fills at
# the stop price — the exit signal fires on a confirmed bar and the marketable
# IOC lands several seconds later through the book. Costs below default to
# live-observed conservative values and can be overridden via CLI; when
# --use-memory-slip is set, per-coin realized adverse exit slip from
# memory.avg_exit_slip_bps overrides the exit default (same data the live
# stop-widener uses).
DEFAULT_ENTRY_SLIP_BPS = 5.0    # ~typical taker adverse fill on entry
DEFAULT_EXIT_SLIP_BPS = 15.0    # exits are market/stop-driven → wider
# Exit confirmation-delay penalty: live exits wait ~4s mid + oracle confirm +
# IOC transit. In a 1h bar that drift is negligible, but a stop firing in a
# fast move overshoots the stop price; modeled as extra adverse bps on
# stop-out exits only (trailing/timeout exits use the regular exit slip).
DEFAULT_STOP_DELAY_SLIP_BPS = 10.0


def _print_walk_forward(is_trades: List[Trade], oos_trades: List[Trade],
                        equity: float) -> None:
    """O-7: print the in-sample vs out-of-sample comparison block.

    Trades are split PER COIN (each coin's bar count/warmup gives its own
    split bar) and aggregated by segment before scoring — identical to the
    old per-trade in_sample tagging, but stats now come from the kernel.
    """
    print("\n=== WALK-FORWARD / OUT-OF-SAMPLE (O-7) ===")
    if not oos_trades:
        print("no out-of-sample trades (raise --oos-frac or widen the window)")
        return
    ms = kstats.trade_stats(is_trades, equity=equity)
    mo = kstats.trade_stats(oos_trades, equity=equity)

    print(f"  {'Segment':<22s} {'IN-SAMPLE':>14s} {'OUT-OF-SAMPLE':>14s}")
    print(f"  {'-'*22} {'-'*14} {'-'*14}")

    def _line(label: str, vs: str, vo: str) -> None:
        print(f"  {label:<22s} {vs:>14s} {vo:>14s}")

    def _pct(v: float) -> str:
        return f"{v:.1f}%"

    def _usd(v: float) -> str:
        return f"${v:+.2f}"

    _line("trades", str(ms.n), str(mo.n))
    _line("win rate", _pct(ms.win_rate_pct) if ms.n else "-",
          _pct(mo.win_rate_pct) if mo.n else "-")
    _line("expectancy/trade", f"${ms.expectancy_usd:+.3f}" if ms.n else "-",
          f"${mo.expectancy_usd:+.3f}" if mo.n else "-")
    _line("total PnL", _usd(ms.pnl_net_usd) if ms.n else "-",
          _usd(mo.pnl_net_usd) if mo.n else "-")
    _line("return on equity",
          _pct(ms.pnl_pct_equity or 0.0) if ms.n else "-",
          _pct(mo.pnl_pct_equity or 0.0) if mo.n else "-")
    _line("Sharpe (per-trade)", f"{ms.sharpe:.2f}" if ms.n else "-",
          f"{mo.sharpe:.2f}" if mo.n else "-")
    _line("max drawdown", f"${ms.max_dd_usd:.2f}" if ms.n else "-",
          f"${mo.max_dd_usd:.2f}" if mo.n else "-")
    oos_ok = mo.n > 0 and mo.expectancy_usd > 0
    print("\n  The strategy is validated out-of-sample only when the OOS "
          "expectancy/PnL stays positive and its Sharpe is in the same "
          "ballpark as in-sample — a large IS-OOS gap means overfitting.")
    print(f"  OOS verdict: {'EDGE HELD out-of-sample' if oos_ok else 'OOS edge not present — treat IS results with caution'}")


def _print_summary(all_trades: List[Trade], equity: float, days: int,
                   *, cost_note: str = "",
                   walk_forward: Optional[tuple[List[Trade], List[Trade]]] = None) -> None:
    print("\n=== SUMMARY ===")
    n = len(all_trades)
    if n == 0:
        print("no trades fired")
        if cost_note:
            print(f"\nCaveats:\n  - {cost_note}")
        return
    s = kstats.trade_stats(all_trades, equity=equity)
    pnl_total = s.pnl_net_usd

    print(f"trades        : {s.n}")
    print(f"win rate      : {s.wins}/{s.n} = {s.win_rate_pct:.1f}%")
    print(f"avg win       : ${s.avg_win_usd:+.2f}")
    print(f"avg loss      : ${s.avg_loss_usd:+.2f}")
    print(f"expectancy    : ${s.expectancy_usd:+.3f} per trade")
    print(f"total PnL     : ${pnl_total:+.2f}  ({pnl_total / equity * 100:+.1f}% on ${equity:.0f}, over {days} days)")
    print(f"exit reasons  : {s.by_reason}")

    # RFT-01：regime 选档分布（仅当回测带了 regime/选档标签时）。
    labeled = [t for t in all_trades if t.exit_label]
    if labeled:
        print("regime 选档   :")
        buckets: dict[str, List[Trade]] = {}
        for t in labeled:
            buckets.setdefault(t.exit_label, []).append(t)
        for label, ts_ in sorted(buckets.items(), key=lambda kv: -len(kv[1])):
            tot = sum(t.pnl_net_usd for t in ts_)
            print(f"  {label:28} {len(ts_):4d} 笔  PnL ${tot:+.2f}  "
                  f"(${tot/len(ts_):+.3f}/笔)")

    # Sample worst and best
    sorted_t = sorted(all_trades, key=lambda t: t.pnl_net_usd)
    print("\nworst 3       :")
    for t in sorted_t[:3]:
        print(f"  {t.coin:6} {t.side:5} bars {t.entry_bar}->{t.exit_bar}  "
              f"${t.pnl_net_usd:+.2f}  {t.reason.value}")
    print("best 3        :")
    for t in sorted_t[-3:][::-1]:
        print(f"  {t.coin:6} {t.side:5} bars {t.entry_bar}->{t.exit_bar}  "
              f"${t.pnl_net_usd:+.2f}  {t.reason.value}")

    print("\nCaveats:")
    print("  - AI verdict substituted with a heuristic (score / trend / burst). Real LLM not replayed.")
    if cost_note:
        print(f"  - {cost_note}")
    print("  - One open position per coin at a time; max_concurrent cap NOT enforced across coins.")
    print("  - Equity held constant (no compounding); cooldown_min not applied.")
    print("  - Unified P4 kernel drives the PRODUCTION DSLTracker, so semantics differ from the "
          "old local DSL:")
    print("    * a position still open on the last bar is CLOSED at that bar's close "
          "(end_of_data); the old script silently dropped it;")
    print("    * hard timeout is the production policy in real wall-clock MINUTES "
          "(default 1800 min; 1h config), not the old local 180-bar limit — stale-flat, "
          "time-scratch, phase-2 tiers and breakeven lock likewise apply when configured;")
    print("    * the production leverage-aware ROE safety net (max_loss_roe_pct) caps losses "
          "in addition to the spot stop — the old local DSL had no ROE cap;")
    print("    * ATR stop width uses the SIM-interval ATR(14)% at the decision bar (this "
          "script's convention), not the production 4h ATR;")
    print("    * exit reasons are normalized to max_loss / floor_breach / hard_timeout / "
          "stale_flat_timeout / time_scratch / end_of_data.")
    print("  - ta_late_entry hard gate is 100% aligned with live: same late_entry_check() "
          "pure function, ENFORCED (backtests evaluate the final rule set), and only "
          "4h/15m bars CLOSED by the decision instant are used — no look-ahead.")
    print("  - Past performance does NOT imply future results.")
    if walk_forward is not None:
        _print_walk_forward(walk_forward[0], walk_forward[1], equity)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--coins", type=int, default=20)
    ap.add_argument("--interval", default="1h", choices=["5m", "15m", "1h", "4h", "1d"])
    ap.add_argument("--equity", type=float, default=100.0)
    ap.add_argument("--equity-fraction", type=float, default=0.0,
                    help="margin fraction per trade (default: .agent-config.json)")
    ap.add_argument("--leverage-ceiling", type=int, default=0,
                    help="max leverage to simulate (default: .agent-config.json)")
    ap.add_argument("--max-loss", type=float, default=None,
                    help="DSL max_loss_pct spot stop (default: .agent-config.json)")
    ap.add_argument("--protect", type=float, default=None,
                    help="DSL protect_pct spot profit threshold (default: .agent-config.json)")
    ap.add_argument("--retrace", type=float, default=None,
                    help="DSL phase-2 retrace threshold 0-1 (default: .agent-config.json)")
    ap.add_argument("--atr-mult", type=float, default=None,
                    help="ATR stop mult (default: live atr_stop setting; 0 = fixed --max-loss)")
    ap.add_argument("--atr-floor", type=float, default=None,
                    help="ATR stop floor spot pct (default: .agent-config.json)")
    ap.add_argument("--atr-ceiling", type=float, default=None,
                    help="ATR stop ceiling spot pct (default: .agent-config.json)")
    ap.add_argument("--no-late-entry", action="store_true",
                    help="disable the live ta_late_entry hard gate (default: enforced, "
                         "100%% parity with the live order-time gate)")
    ap.add_argument("--regime-replay", action="store_true",
                     help="RFT-01：每仓在 PIT 窗口现判 regime，并按生产同源 "
                          "select_exit_params/regime clocks 选出场档（默认关闭=平铺 policy）")
    ap.add_argument("--tick-confirm", action="store_true",
                     help="T-26（已为生产默认）：破位确认按实盘秒级 tick 子采样回放")
    ap.add_argument("--bar-confirm", action="store_true",
                     help="T-26：改用旧 bar 粗粒度口径（consecutive_breaches=连续 N 根 K 线）")
    ap.add_argument("--tick-confirm-interval-s", type=float, default=5.0,
                     help="T-26 子采样 tick 间隔秒（默认 5=exit_checkpoint_min_interval_s）")
    ap.add_argument("--entry-slip-bps", type=float, default=DEFAULT_ENTRY_SLIP_BPS,
                    help=f"adverse entry slippage in bps (default {DEFAULT_ENTRY_SLIP_BPS})")
    ap.add_argument("--exit-slip-bps", type=float, default=DEFAULT_EXIT_SLIP_BPS,
                    help=f"adverse exit slippage in bps (default {DEFAULT_EXIT_SLIP_BPS})")
    ap.add_argument("--stop-delay-slip-bps", type=float, default=DEFAULT_STOP_DELAY_SLIP_BPS,
                    help="extra adverse bps on max_loss stop-outs (confirm-delay/overshoot; "
                         f"default {DEFAULT_STOP_DELAY_SLIP_BPS})")
    ap.add_argument("--use-memory-slip", action="store_true",
                    help="override --exit-slip-bps per coin with memory.avg_exit_slip_bps "
                         "(realized adverse exit slip from live closes; falls back to the "
                         "default when a coin has < the configured min samples)")
    ap.add_argument("--use-memory-fee", action="store_true",
                    help="O-8: calibrate the round-trip fee per coin with "
                         "memory.avg_round_trip_fee_bps (actual exchange fee_usd from live "
                         "closes; falls back to the 5-bps default when a coin has < the "
                         "configured min samples)")
    ap.add_argument("--no-slippage", action="store_true",
                    help="zero all slippage/penalty (fee-only, pre-H-7 behavior)")
    ap.add_argument("--oos-frac", type=float, default=0.0,
                    help="O-7: fraction of the tradeable window held out as "
                         "out-of-sample for walk-forward validation (e.g. 0.3 = "
                         "last 30%%; 0 disables the IS/OOS report)")
    args = ap.parse_args()
    if not 0.0 <= args.oos_frac < 1.0:
        ap.error("--oos-frac must be in [0, 1) (0 disables the OOS report)")

    live = read_agent_config()
    live_dsl = live.get("dsl_exit", {}) or {}
    live_atr = live_dsl.get("atr_stop", {}) or {}
    equity_fraction = float(args.equity_fraction or live.get("equity_fraction_per_trade", 0.10))
    leverage_ceiling = int(args.leverage_ceiling or cfg_get("leverage", config=live))
    max_loss = float(args.max_loss if args.max_loss is not None else cfg_get("dsl_exit.max_loss_pct", config=live_dsl))
    protect = float(args.protect if args.protect is not None else cfg_get("dsl_exit.protect_pct", config=live_dsl))
    retrace = float(args.retrace if args.retrace is not None else cfg_get("dsl_exit.retrace_threshold", config=live_dsl))
    if args.atr_mult is not None:
        atr_mult = float(args.atr_mult)
    else:
        atr_mult = float(live_atr.get("atr_mult", 0.0)) if bool(live_atr.get("enabled", False)) else 0.0
    atr_floor = float(args.atr_floor if args.atr_floor is not None else live_atr.get("floor_pct", 1.0))
    atr_ceiling = float(args.atr_ceiling if args.atr_ceiling is not None else live_atr.get("ceiling_pct", 4.0))
    # Live late-entry gate parameters (same ta_late_entry config block the
    # order-time gate reads). The backtest ENFORCES the veto regardless of the
    # live mode (shadow/enforce is a deployment control, not a rule difference).
    late_entry_params: Dict[str, Any] = {} if args.no_late_entry else dict(live.get("ta_late_entry") or {})
    # mode/shadow_log_path are deployment controls, not veto rules — strip them
    # once up front so every coin calls the same pure rule parameter set.
    le_cfg = dict(late_entry_params)
    le_cfg.pop("mode", None)
    le_cfg.pop("shadow_log_path", None)

    # H-7 cost model (see constants above). --no-slippage restores the
    # fee-only baseline; otherwise defaults are live-conservative and the
    # per-coin exit slip can be overridden from realized live closes.
    if args.no_slippage:
        entry_slip_bps = exit_slip_bps = stop_delay_slip_bps = 0.0
    else:
        entry_slip_bps = float(args.entry_slip_bps)
        exit_slip_bps = float(args.exit_slip_bps)
        stop_delay_slip_bps = float(args.stop_delay_slip_bps)
    _mem = None
    if args.use_memory_slip and not args.no_slippage:
        try:
            from hermes_trader.agents.memory import memory as _mem_obj
            _mem = _mem_obj
        except Exception as e:  # read-only best effort; defaults stay in place
            print(f"  (memory slip unavailable: {e}; using --exit-slip-bps default)")
    # O-8: measured round-trip fee source. Fee is charged regardless of the
    # slippage toggle, so this is independent of --no-slippage.
    _mem_fee = None
    if args.use_memory_fee:
        try:
            from hermes_trader.agents.memory import memory as _mem_fee
        except Exception as e:  # read-only best effort; default fee stays
            print(f"  (memory fee unavailable: {e}; using {ROUND_TRIP_FEE_BPS:.1f}-bps default)")
            _mem_fee = None

    _fee_tag = " per-coin calibrated from live fills" if _mem_fee is not None else ""
    if args.no_slippage:
        cost_note = (f"Fee-only model (--no-slippage): {ROUND_TRIP_FEE_BPS:.1f}-bps "
                     f"round-trip fee{_fee_tag}, no slippage.")
    else:
        cost_note = (f"Cost model (H-7): {ROUND_TRIP_FEE_BPS:.1f}-bps round-trip fee{_fee_tag} + "
                     f"{entry_slip_bps:.1f}-bps entry slip / {exit_slip_bps:.1f}-bps exit slip"
                     f"{' (per-coin from live memory where available)' if _mem is not None else ''}"
                     f" + {stop_delay_slip_bps:.1f}-bps stop-out delay penalty; no funding cost.")

    bars_per_day = {"5m": 288, "15m": 96, "1h": 24, "4h": 6, "1d": 1}[args.interval]
    total_bars = args.days * bars_per_day + 100  # +warmup

    # Trigger config is only used for the header line; the kernel's
    # default_heuristic_config() reads the same source.
    cfg = get_config()
    universe = get_universe()
    perps = [m for m in universe if m["type"] == "perp" and not m["coin"].startswith("@")]
    coins = sorted(perps, key=lambda m: m.get("dayNtlVlm", 0), reverse=True)[: args.coins]

    # One production exit policy for the whole run, built from the live config
    # then overlaid with the CLI knobs. The kernel adapter copies it again per
    # position (zeroing sub-bar confirm gates), so coins never share state.
    base_policy = replace(
        _build_policy_from_config(),
        max_loss_pct=max_loss,
        protect_pct=protect,
        retrace_threshold=retrace,
        atr_stop_enabled=atr_mult > 0,
        atr_stop_mult=atr_mult,
        atr_stop_floor_pct=atr_floor,
        atr_stop_ceiling_pct=atr_ceiling,
    )

    print("=== hermes-trader backtest ===")
    print(f"period: {args.days} days   interval: {args.interval}   universe: top-{args.coins} by 24h volume")
    print(f"equity: ${args.equity:.0f}   fraction: {equity_fraction:.0%}   leverage ceiling: {leverage_ceiling}x")
    print(f"DSL: max_loss={max_loss}%  protect={protect}%  retrace={retrace}  atr_mult={atr_mult}")
    print(f"triggers config: sigma={cfg['thresholds']['sigmaThreshold']}  "
          f"momentumPct={cfg['thresholds']['momentumPct']}\n")

    all_trades: List[Trade] = []
    is_trades: List[Trade] = []
    oos_trades: List[Trade] = []
    stop_widths: List[float] = []
    late_vetoes: List[dict] = []
    sim_ms = _MS_PER[args.interval]
    # Bars to pull for the higher-TF gate series: enough to cover the whole sim
    # window plus indicator warmup (min_bars_4h=30 / min_bars_15m=20).
    need_4h = math.ceil(total_bars * sim_ms / _MS_PER["4h"]) + 40
    need_15m = math.ceil(total_bars * sim_ms / _MS_PER["15m"]) + 30
    if late_entry_params:
        print(f"ta_late_entry: ENFORCED in backtest (live mode={late_entry_params.get('mode', 'shadow')}; "
              f"rsi_ob={late_entry_params.get('rsi_ob')}, adx_trend={late_entry_params.get('adx_trend_threshold')}, "
              f"mtf={late_entry_params.get('mtf_enabled')}; --no-late-entry to disable)\n")
    for m in coins:
        coin = m["coin"]; max_lev = int(m.get("maxLeverage", 5))
        try:
            candles = fetch_hl_candles(coin, args.interval, total_bars)
            if len(candles) < 110:
                print(f"  {coin:8} skip ({len(candles)} bars — insufficient)")
                continue
            candles_4h: Optional[List[Candle]] = None
            candles_15m: Optional[List[Candle]] = None
            if late_entry_params:
                try:
                    # Reuse the base series when it IS the higher TF; fetch failures
                    # degrade this coin to no-gate, mirroring the live fail-open.
                    candles_4h = candles if args.interval == "4h" else fetch_hl_candles(coin, "4h", need_4h)
                    candles_15m = candles if args.interval == "15m" else fetch_hl_candles(coin, "15m", need_15m)
                except Exception as e:
                    print(f"  {coin:8} late-entry gate unavailable ({e}) — running without it")
                    candles_4h = candles_15m = None
            # H-7: per-coin realized adverse exit slip (when enabled and the
            # coin has enough live closes), else the CLI/default value.
            coin_exit_slip = exit_slip_bps
            if _mem is not None:
                try:
                    _ms = float(_mem.avg_exit_slip_bps(coin))
                    if _ms > 0.0:
                        coin_exit_slip = _ms
                except Exception:
                    pass
            # O-8: per-coin measured round-trip fee (when enabled and enough
            # live closes), else the static default.
            coin_fee_bps = ROUND_TRIP_FEE_BPS
            if _mem_fee is not None:
                try:
                    _mf = float(_mem_fee.avg_round_trip_fee_bps(coin))
                    if _mf > 0.0:
                        coin_fee_bps = _mf
                except Exception:
                    pass

            # Decision-time context: attach the SIM-interval ATR(14)% computed
            # from exactly the bars closed at the decision instant (this
            # script's convention — production registration uses 4h ATR).
            open_times = [c.t for c in candles]

            def _ctx(close_ms: int) -> tuple[float, str]:
                # close_ms == next bar's open t, so bisect_left - 1 recovers
                # the decision bar (bisect_right would overshoot by one).
                j = bisect.bisect_left(open_times, close_ms) - 1
                if j < 0:
                    return 0.0, ""
                _bull, atr_pct, _adx = ksig.trend_and_atr_pct(candles[: j + 1])
                return (atr_pct or 0.0), ""

            signals = ksig.heuristic_signals(
                candles, ksig.default_heuristic_config(warmup=100),
                context_fn=_ctx, bar_ms=sim_ms,
            )

            # ta_late_entry parity (deep audit 高危项, 2026-08-30): the live
            # ta_late_entry_gate re-runs late_entry_check() on FRESH 4h (+15m)
            # candles immediately before order placement. The backtest calls
            # the SAME pure function on only the higher-TF bars that have
            # CLOSED by the decision instant (bar i close → fill at i+1 open),
            # so the veto is 100% identical in rules and free of look-ahead.
            kept: List[ksig.Signal] = []
            le_enabled = bool(le_cfg) and candles_4h is not None
            t4 = [c.t for c in candles_4h] if candles_4h else []
            t15 = [c.t for c in candles_15m] if candles_15m else []
            for sig in signals:
                if le_enabled:
                    decision_ms = candles[sig.bar_index].t + sim_ms
                    w4 = _closed_slice(candles_4h, t4, decision_ms, _MS_PER["4h"])
                    w15 = _closed_slice(candles_15m, t15, decision_ms, _MS_PER["15m"])
                    le = late_entry_check(w4, w15, sig.side, le_cfg)
                    if le.get("block"):
                        late_vetoes.append({
                            "coin": coin, "side": sig.side, "bar": sig.bar_index,
                            "reason": le.get("reason", ""),
                            "rsi4h": le.get("rsi4h"), "adx4h": le.get("adx4h"),
                            "extension": le.get("extension"),
                        })
                        continue
                kept.append(sig)

            lev = min(leverage_ceiling, max_lev)
            notional = args.equity * equity_fraction * lev
            cost = kcost.CostModel(
                round_trip_fee_bps=coin_fee_bps,
                entry_slip_bps=entry_slip_bps,
                exit_slip_bps=coin_exit_slip,
                stop_delay_slip_bps=stop_delay_slip_bps,
            )
            trades = kdriver.run(
                candles, kept, base_policy, coin=coin, leverage=lev,
                notional_usd=notional, cost=cost, bar_ms=sim_ms,
                regime_replay=args.regime_replay,
                dsl_config=live.get("dsl_exit", {}),
                confirm_mode="bar" if args.bar_confirm else "tick",
                tick_confirm_s=args.tick_confirm_interval_s,
            )
            # Structural no-look-ahead invariant check on every coin.
            kguard.assert_run_pit(candles, kept, trades)

            # ATR-stop width distribution: only for signals that actually
            # filled (the single-position kernel discards signals arriving
            # while already in a position), matching the old script's
            # collected-on-entry behavior.
            entry_bars = {t.entry_bar for t in trades}
            if atr_mult > 0:
                for sig in kept:
                    if sig.bar_index + 1 in entry_bars and sig.entry_atr_pct > 0:
                        stop_widths.append(
                            min(max(sig.entry_atr_pct * atr_mult, atr_floor),
                                atr_ceiling)
                        )

            # O-7: split per coin on its own tradeable window, classify by
            # ENTRY bar (a trade decided pre-split stays in-sample even if its
            # exit lands after the split).
            if args.oos_frac > 0:
                split_bar = kstats.oos_split_index(len(candles), 100, args.oos_frac)
                _is, _oos = kstats.split_trades(trades, split_bar)
                is_trades.extend(_is)
                oos_trades.extend(_oos)

            pnl = sum(t.pnl_net_usd for t in trades)
            w = sum(1 for t in trades if t.pnl_net_usd > 0)
            print(f"  {coin:8} {len(trades):3} trades  win {w:3}  PnL ${pnl:+7.2f}  (max_lev {max_lev}x)")
            all_trades.extend(trades)
        except Exception as e:
            print(f"  {coin:8} error: {e}")

    if late_entry_params and late_vetoes:
        print(f"\nta_late_entry vetoes: {len(late_vetoes)} entries blocked")
        for v in late_vetoes[:8]:
            print(f"  {v['coin']:8} {v['side']:5} bar {v['bar']:5}  {v['reason']}")
        if len(late_vetoes) > 8:
            print(f"  ... and {len(late_vetoes) - 8} more")

    _print_summary(
        all_trades, args.equity, args.days, cost_note=cost_note,
        walk_forward=((is_trades, oos_trades) if args.oos_frac > 0 else None),
    )
    if stop_widths:
        sw = sorted(stop_widths)
        n = len(sw)
        print(f"\nATR stop widths (spot %): n={n}  "
              f"min={sw[0]:.2f}  p25={sw[n//4]:.2f}  median={sw[n//2]:.2f}  "
              f"p75={sw[3*n//4]:.2f}  max={sw[-1]:.2f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
