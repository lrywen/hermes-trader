#!/usr/bin/env python3
"""90/180-day historical backtest for the "re-enable shorts" decision.

The live system runs runner_entry_gate.allow_shorts=false and the only
historical short fill was a manual test — so there is NO live sample base.
This script replays 1h bars and asks: had shorts been enabled under several
candidate gate sets, what would the mechanical edge have been, net of the
H-7 cost model (5bps round-trip fee + 5bps entry slip + 15bps exit slip +
10bps stop-out delay)?

Variants (ALL enforce the live ta_late_entry short-mirror hard gate, 100%
parity via the shared late_entry_check() pure function on closed 4h bars):

  A  baseline     : bearish signal (composite>=25 | ATR>=0.4% | burst)
  B  + own trend  : the coin's own 4h is in a downtrend (EMA8 < EMA21)
  C  + macro down : BTC macro regime == "down" (production trend_from_closes
                    math with the live regime_classifier params)
  D  + both       : B AND C
  E  + composite  : D with composite >= MIN_SCORE, swept over SCAN_SCORES to
                    calibrate runner_entry_gate.min_short_composite.

Implementation note (Audit 2026-09-22): this script no longer re-implements a
local DSL/Trade loop. It scans base short candidates once with the shared
``hermes_trader.backtest.signals`` helpers, then runs each variant through the
UNIFIED P4 kernel (``backtest.driver.run``) — the SAME production DSLTracker +
H-7 CostModel ``scripts/backtest.py`` uses — so exit/cost semantics match the
main backtest exactly.

Anti-look-ahead: entries decide on 1h bar i close and fill at bar i+1 open;
the 4h series is sliced to bars fully closed by the decision instant; the
macro regime at bar i uses only BTC closes up to that bar (EMA is causal).

Caveats: the AI verdict is substituted by a deterministic heuristic, so
min_short_confidence (an LLM output) CANNOT be replayed — the composite score
stands in for min_short_composite only; the $50M volume floor uses TODAY's
dayNtlVlm snapshot (survivorship bias); one open position per coin; equity
held constant.

Usage:
    python3 scripts/backtest_short_regime.py --days 180 --coins 20
"""
from __future__ import annotations

import argparse
import bisect
import math
import os
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

os.environ["HERMES_BACKTEST"] = "1"

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
from hermes_trader.agents.ta_filter import late_entry_check
from hermes_trader.backtest import cost as kcost
from hermes_trader.backtest import driver as kdriver
from hermes_trader.backtest import signals as ksig
from hermes_trader.backtest import stats as kstats
from hermes_trader.backtest.types import Signal, Trade
from hermes_trader.client.hl_client import fetch_hl_candles
from hermes_trader.client.universe import get_universe
from hermes_trader.indicators import math as ind
from hermes_trader.models.types import Candle

_MS_PER: Dict[str, int] = {
    "5m": 5 * 60_000, "15m": 15 * 60_000, "1h": 60 * 60_000,
    "4h": 4 * 60_000, "1d": 24 * 60_000,
}

W4_WINDOW = 60     # trailing 4h suffix fed to late_entry_check
SCAN_SCORES = (20.0, 25.0, 30.0, 40.0)  # variant-E min_short_composite sweep


def _closed_slice(series: Optional[List[Candle]], ts_ms: List[int],
                  decision_ms: int, tf_ms: int) -> Optional[List[Candle]]:
    """Prefix of a higher-TF series FULLY CLOSED at the decision instant.

    Mirrors scripts/backtest.py::_closed_slice (no look-ahead).
    """
    if not series:
        return None
    cutoff = decision_ms - tf_ms
    j = bisect.bisect_right(ts_ms, cutoff)
    return series[:j] if j > 0 else None


def _macro_regime_series(closes: List[float], fast_p: int, slow_p: int,
                         slope_up: float, lookback: int) -> List[str]:
    """trend_from_closes replayed at every bar index (no look-ahead)."""
    n = len(closes)
    out = ["neutral"] * n
    if n < slow_p:
        return out
    fast = ind.ema(closes, fast_p)
    slow = ind.ema(closes, slow_p)
    for i in range(n):
        if i < slow_p - 1 or i < lookback:
            continue
        f_now, s_now, f_prev = fast[i], slow[i], fast[i - lookback]
        if not all(math.isfinite(v) for v in (f_now, s_now, f_prev)):
            continue
        if f_prev == 0:
            continue
        slope = (f_now - f_prev) / abs(f_prev)
        if f_now > s_now and slope > slope_up:
            out[i] = "up"
        elif f_now < s_now and slope < -slope_up:
            out[i] = "down"
    return out


def _scan_candidates(coin: str, candles: List[Candle],
                     candles_4h: Optional[List[Candle]],
                     macro_ts: List[int], macro_regime: List[str],
                     th: Dict[str, Any], weights: Dict[str, float],
                     le_params: Dict[str, Any],
                     warmup: int, sim_ms: int) -> tuple:
    """Pass 1 (run ONCE per coin): every bar where the BASE short signal fires
    — bearish heuristic verdict + ta_confirmed proxy + live ta_late short
    hard gate. Records the per-variant filter inputs so variants can replay
    through the kernel without rescanning indicators."""
    t4 = [c.t for c in candles_4h] if candles_4h else []
    closes4 = [c.c for c in candles_4h] if candles_4h else []
    ema8_4 = ind.ema(closes4, 8) if len(closes4) >= 8 else []
    ema21_4 = ind.ema(closes4, 21) if len(closes4) >= 21 else []
    cands: List[Dict[str, Any]] = []
    stats = {"bearish": 0, "signal": 0, "ta_conf": 0, "late_veto": 0}
    for i in range(warmup, len(candles) - 1):
        window = candles[: i + 1]
        score, hits = ksig.evaluate_window(window, th, weights)
        bullish, atr_pct, adx14 = ksig.trend_and_atr_pct(window)
        if bullish is None or bullish:
            continue  # shorts only; None = insufficient data
        stats["bearish"] += 1
        verdict = ksig.heuristic_verdict(score, hits, bullish, atr_pct)
        if verdict != "short":
            continue
        stats["signal"] += 1
        burst = any(h["name"] == "momentumBurst" and h["fired"] for h in hits)
        if not ksig.ta_confirmed(bullish, atr_pct, adx14, score) and not burst:
            continue
        stats["ta_conf"] += 1
        decision_ms = candles[i].t + sim_ms
        # Live ta_late_entry hard gate (short mirror), closed 4h bars only.
        if le_params and candles_4h:
            w4 = _closed_slice(candles_4h, t4, decision_ms, _MS_PER["4h"])
            if w4:
                w4 = w4[-W4_WINDOW:]
            le = late_entry_check(w4, None, "short", le_params)
            if le.get("block"):
                stats["late_veto"] += 1
                continue
        # Variant-B input: own 4h downtrend on closed bars (EMA8 < EMA21).
        j4 = bisect.bisect_right(t4, decision_ms - _MS_PER["4h"])
        own_down = False
        if j4 >= 22 and ema8_4 and ema21_4:
            e8, e21 = ema8_4[j4 - 1], ema21_4[j4 - 1]
            own_down = math.isfinite(e8) and math.isfinite(e21) and e8 < e21
        # Variant-C input: macro regime from the last CLOSED BTC 1h bar.
        jm = bisect.bisect_right(macro_ts, candles[i].t) - 1
        macro = macro_regime[jm] if 0 <= jm < len(macro_regime) else "neutral"
        cands.append({"bar": i, "score": score, "own_down": own_down,
                      "macro": macro, "atr_pct": atr_pct})
    return cands, stats


def _variant_admit(name: str, min_score: float = 0.0
                   ) -> Callable[[Dict[str, Any]], bool]:
    if name == "A":
        return lambda c: True
    if name == "B":
        return lambda c: bool(c["own_down"])
    if name == "C":
        return lambda c: c["macro"] == "down"
    if name == "D":
        return lambda c: bool(c["own_down"]) and c["macro"] == "down"
    if name == "E":
        return lambda c: (bool(c["own_down"]) and c["macro"] == "down"
                          and c["score"] >= min_score)
    raise ValueError(name)


def _run_variant(candles: List[Candle], cands: List[Dict[str, Any]],
                 policy, admit: Callable[[Dict[str, Any]], bool], *,
                 coin: str, leverage: int, notional: float,
                 cost: kcost.CostModel, sim_ms: int) -> List[Trade]:
    """Turn admitted base candidates into short Signals and run them through
    the unified kernel (production DSL + H-7 cost)."""
    signals = [Signal(c["bar"], "short") for c in cands if admit(c)]
    return kdriver.run(
        candles, signals, policy, coin=coin, leverage=leverage,
        notional_usd=notional, cost=cost, bar_ms=sim_ms,
    )


def _scale_trade(t: Trade, notional_usd: float) -> Trade:
    """重放一笔交易到新的名义金额：收益率口径不变，按名义比例缩放金额项。"""
    base = t.notional_usd or notional_usd
    k = (notional_usd / base) if base else 0.0
    return replace(
        t, notional_usd=notional_usd,
        fee_usd=round(t.fee_usd * k, 6),
        pnl_gross_usd=round(t.pnl_gross_usd * k, 6),
        pnl_net_usd=round(t.pnl_net_usd * k, 6),
    )


def _apply_portfolio_constraint(trades: List[Trade], init_equity: float,
                                fraction: float, max_concurrent: int
                                ) -> List[Trade]:
    """组合级资金/仓位约束（修正 ret% 口径）。

    此前每个币独立按固定 $100 本金、每个信号都新开仓，导致同一权益被反复
    使用、累计 PnL 远超本金（ret 出现 -4614%）。这里按入场时间做事件驱动
    回放，模拟真实账户：
      * 初始权益 init_equity，每笔名义 = 当前权益 × fraction × 该笔隐含杠杆
        （杠杆由 原notional / 原占用保证金 推断，用 notional/equity 比例还原）；
      * 同一时刻最多 max_concurrent 个持仓，且同一币种不重复开仓；
      * 信号到达时仓位已满或该币已持仓 → 跳过（不是无脑全做）；
      * 持仓平仓后盈亏并入权益、资金回笼，后续按新权益下单（复利）。

    注：每笔交易的进/出场价与退出原因不变（kernel 已确定），仅按当时权益
    缩放名义与金额，因此胜率/退出原因分布不变，PnL/ret 变为真实本金口径。
    """
    # 原回放里每笔名义=固定权益×fraction×leverage；反推每笔 leverage 倍数。
    base_margin = init_equity * fraction
    ordered = sorted(trades, key=lambda t: (t.entry_time_ms, t.exit_time_ms))
    equity = init_equity
    open_positions: List[Trade] = []  # 当前活跃（已缩放）持仓
    accepted: List[Trade] = []
    for t in ordered:
        # 释放本笔入场之前已平仓的持仓，盈亏并入权益。
        still: List[Trade] = []
        for p in open_positions:
            if p.exit_time_ms <= t.entry_time_ms:
                equity += p.pnl_net_usd
            else:
                still.append(p)
        open_positions = still
        # 仓位上限 / 同币不重复。
        if len(open_positions) >= max_concurrent:
            continue
        if any(p.coin == t.coin for p in open_positions):
            continue
        if equity <= 0:
            break
        leverage = (t.notional_usd / base_margin) if base_margin else 0.0
        notional = max(0.0, equity * fraction * leverage)
        if notional <= 0:
            continue
        st = _scale_trade(t, round(notional, 6))
        open_positions.append(st)
        accepted.append(st)
    # 末尾仍活跃的持仓其实已在 kernel 平仓（有 exit），其盈亏也计入最终权益，
    # 但 accepted 已含这些 Trade；统计层直接用全部 accepted 即可。
    return sorted(accepted, key=lambda t: t.entry_time_ms)


def _window_trades(trades: List[Trade], candles: List[Candle],
                   days: int) -> List[Trade]:
    """Trades ENTERED within the trailing `days` window."""
    cutoff = candles[-1].t - days * 86_400_000
    return [t for t in trades if candles[t.entry_bar].t >= cutoff]


def _print_variant(label: str, desc: str,
                   trades_by_window: Dict[int, List[Trade]],
                   windows: tuple, equity: float) -> List[tuple]:
    print(f"\n--- Variant {label}: {desc} ---")
    print(f"  {'window':<7s} {'n':>4s} {'win%':>6s} {'exp/trade':>10s} "
          f"{'PnL':>9s} {'ret%':>7s} {'maxDD':>8s}  exits")
    rows = []
    for d in windows:
        sub = trades_by_window.get(d) or []
        s = kstats.trade_stats(sub, equity=equity)
        rows.append((label, d, s.n, s.win_rate_pct, s.expectancy_usd,
                     s.pnl_net_usd, s.max_dd_usd))
        if not s.n:
            print(f"  {d}d{'':<4s} {'0':>4s} {'-':>6s} {'-':>10s} "
                  f"{'-':>9s} {'-':>7s} {'-':>8s}  -")
            continue
        print(f"  {d}d{'':<4s} {s.n:>4d} {s.win_rate_pct:>5.1f}% "
              f"${s.expectancy_usd:>+8.3f} ${s.pnl_net_usd:>+8.2f} "
              f"{s.pnl_pct_equity:>+6.1f}% ${s.max_dd_usd:>7.2f}  "
              f"{s.by_reason}")
    return rows


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=int, default=180,
                    help="history to pull (results are also split at 90d)")
    ap.add_argument("--coins", type=int, default=20)
    ap.add_argument("--equity", type=float, default=100.0)
    ap.add_argument("--min-vol", type=float, default=50e6,
                    help="24h notional volume floor for the universe (USD)")
    ap.add_argument("--entry-slip-bps", type=float,
                    default=kcost.DEFAULT_ENTRY_SLIP_BPS)
    ap.add_argument("--exit-slip-bps", type=float,
                    default=kcost.DEFAULT_EXIT_SLIP_BPS)
    ap.add_argument("--stop-delay-slip-bps", type=float,
                    default=kcost.DEFAULT_STOP_DELAY_SLIP_BPS)
    ap.add_argument("--max-concurrent", type=int, default=5,
                    help="组合级最大同时持仓数（同币不重复，平仓资金回笼）")
    args = ap.parse_args()

    live = read_agent_config()
    live_dsl = live.get("dsl_exit", {}) or {}
    equity_fraction = float(live.get("equity_fraction_per_trade", 0.10))
    lev_ceiling = int(cfg_get("leverage", config=live) or 5)
    max_loss = float(cfg_get("dsl_exit.max_loss_pct", config=live_dsl) or 2.5)
    protect = float(cfg_get("dsl_exit.protect_pct", config=live_dsl) or 1.5)
    retrace = float(cfg_get("dsl_exit.retrace_threshold", config=live_dsl) or 0.30)

    le_params = dict(live.get("ta_late_entry") or {})
    le_params.pop("mode", None)
    le_params.pop("shadow_log_path", None)

    rc = live.get("regime_classifier") or {}
    fast_p = int(rc.get("fast_ema", 20))
    slow_p = int(rc.get("slow_ema", 30))
    slope_up = float(rc.get("slope_threshold", 0.002))
    lookback = int(rc.get("slope_lookback", 8))
    gate = live.get("runner_entry_gate") or {}
    live_min_short_comp = float(gate.get("min_short_composite", 40))
    live_min_short_conf = float(gate.get("min_short_confidence", 0.68))

    interval = "1h"
    sim_ms = _MS_PER[interval]
    total_bars = args.days * 24 + 100  # + warmup
    need_4h = math.ceil(total_bars * sim_ms / _MS_PER["4h"]) + 40

    # Trigger thresholds/weights the live scanner uses.
    cfg = get_config()
    th = dict(cfg["thresholds"])
    weights = dict(cfg["weights"])

    # Production exit policy: base 3-param ladder (see module docstring caveat).
    from hermes_trader.agents.dsl_exit import _build_policy_from_config
    policy = replace(
        _build_policy_from_config(),
        max_loss_pct=max_loss, protect_pct=protect,
        retrace_threshold=retrace,
    )

    print("=== hermes-trader SHORT re-enable backtest ===")
    print(f"period: {args.days}d (also split at 90d)   interval: {interval}   "
          f"universe: top-{args.coins} perps with 24h vol >= ${args.min_vol / 1e6:.0f}M")
    print(f"equity: ${args.equity:.0f}   fraction: {equity_fraction:.0%}   "
          f"leverage ceiling: {lev_ceiling}x")
    print(f"DSL: max_loss={max_loss}%  protect={protect}%  retrace={retrace}")
    print(f"ta_late_entry: ENFORCED (rsi {le_params.get('rsi_ob')}/{le_params.get('rsi_os')}, "
          f"ext ±{le_params.get('ext_ob')}, trend-relax ADX>={le_params.get('adx_trend_threshold')}, "
          f"mtf={le_params.get('mtf_enabled')})")
    print(f"macro regime: BTC EMA{fast_p}/{slow_p} slope±{slope_up} over {lookback} bars "
          f"(live regime_classifier params)")
    print(f"cost model (H-7): {kcost.DEFAULT_ROUND_TRIP_FEE_BPS:.1f}bps RT fee + "
          f"{args.entry_slip_bps:.1f}bps entry / {args.exit_slip_bps:.1f}bps exit slip + "
          f"{args.stop_delay_slip_bps:.1f}bps stop-out delay")
    print(f"live short gates for reference: min_short_confidence={live_min_short_conf} "
          f"(NOT replayable — LLM output), min_short_composite={live_min_short_comp}")

    # Macro regime series from BTC 1h closes (production classifier params).
    print("\nfetching BTC 1h for macro regime series ...")
    btc = fetch_hl_candles("BTC", interval, total_bars)
    macro_ts = [c.t for c in btc]
    macro_regime = _macro_regime_series(
        [c.c for c in btc], fast_p, slow_p, slope_up, lookback)
    n_down = sum(1 for r in macro_regime if r == "down")
    n_up = sum(1 for r in macro_regime if r == "up")
    print(f"BTC bars: {len(btc)}   regime mix: up {n_up / len(btc) * 100:.0f}% / "
          f"down {n_down / len(btc) * 100:.0f}% / "
          f"{(len(btc) - n_up - n_down) / len(btc) * 100:.0f}%")

    universe = get_universe()
    perps = [m for m in universe
             if m["type"] == "perp" and not m["coin"].startswith("@")
             and float(m.get("dayNtlVlm", 0) or 0) >= args.min_vol]
    coins = sorted(perps, key=lambda m: float(m.get("dayNtlVlm", 0) or 0),
                   reverse=True)[: args.coins]
    print(f"universe: {len(perps)} perps pass the volume floor; simulating top "
          f"{len(coins)}: {', '.join(m['coin'] for m in coins)}\n")

    variant_labels = ["A", "B", "C", "D"] + [f"E>={s:g}" for s in SCAN_SCORES]
    trades_by_variant: Dict[str, List[Trade]] = {v: [] for v in variant_labels}
    candles_by_coin: Dict[str, List[Candle]] = {}
    total_stats = {"bearish": 0, "signal": 0, "ta_conf": 0, "late_veto": 0}

    for m in coins:
        coin = m["coin"]
        max_lev = int(m.get("maxLeverage", 5))
        try:
            candles = fetch_hl_candles(coin, interval, total_bars)
            if len(candles) < 200:
                print(f"  {coin}: only {len(candles)} bars — skipped")
                continue
            candles_4h = fetch_hl_candles(coin, "4h", need_4h)
        except Exception as e:
            print(f"  {coin}: fetch failed: {e} — skipped")
            continue
        candles_by_coin[coin] = candles
        cands, cstats = _scan_candidates(
            coin, candles, candles_4h, macro_ts, macro_regime, th, weights,
            le_params, warmup=100, sim_ms=sim_ms)
        for k in total_stats:
            total_stats[k] += cstats[k]

        lev = min(lev_ceiling, max_lev)
        notional = args.equity * equity_fraction * lev
        cost = kcost.CostModel(
            round_trip_fee_bps=kcost.DEFAULT_ROUND_TRIP_FEE_BPS,
            entry_slip_bps=args.entry_slip_bps,
            exit_slip_bps=args.exit_slip_bps,
            stop_delay_slip_bps=args.stop_delay_slip_bps,
        )
        for label in variant_labels:
            if label.startswith("E"):
                ms = float(label.split(">=")[1])
                admit = _variant_admit("E", ms)
            else:
                admit = _variant_admit(label)
            trades_by_variant[label].extend(_run_variant(
                candles, cands, policy, admit, coin=coin, leverage=lev,
                notional=notional, cost=cost, sim_ms=sim_ms))
        print(f"  {coin:8s} bars={len(candles)}  base short candidates={len(cands)} "
              f"(bearish {cstats['bearish']} → signal {cstats['signal']} → "
              f"ta_conf {cstats['ta_conf']}, late-veto {cstats['late_veto']})")

    print(f"\nbase-candidate funnel (all coins): bearish {total_stats['bearish']} → "
          f"signal {total_stats['signal']} → ta_conf {total_stats['ta_conf']} → "
          f"late-vetoed {total_stats['late_veto']} → admitted to variant filters")

    windows = (90, args.days) if args.days != 90 else (90,)
    descs = {
        "A": "baseline: bearish signal + ta_late only",
        "B": "+ own 4h downtrend (EMA8<EMA21)",
        "C": "+ macro down (BTC regime)",
        "D": "+ BOTH own-4h downtrend AND macro down",
    }
    for s in SCAN_SCORES:
        descs[f"E>={s:g}"] = f"D + composite>={s:g} (min_short_composite candidate)"

    print("\n=== VARIANT RESULTS (net of H-7 costs) ===")
    verdict_rows = []
    for label in variant_labels:
        # 组合级资金/仓位约束：在真实本金与并发上限下回放，PnL/ret 才可信。
        constrained = _apply_portfolio_constraint(
            trades_by_variant[label], args.equity, equity_fraction,
            args.max_concurrent,
        )
        trades_by_window: Dict[int, List[Trade]] = {}
        for d in windows:
            sub = []
            for coin, candles in candles_by_coin.items():
                ct = [t for t in constrained if t.coin == coin]
                sub.extend(_window_trades(ct, candles, d))
            trades_by_window[d] = sub
        verdict_rows.extend(_print_variant(
            label, descs[label], trades_by_window, windows, args.equity))

    print("\n=== DECISION GRID (per-trade USD on "
          f"${args.equity:.0f} equity) ===")
    print(f"  {'variant':<8s} {'win':>5s} {'n':>4s} {'win%':>6s} "
          f"{'exp/trade':>10s} {'PnL':>9s} {'maxDD':>8s}")
    for label, d, n, wr, exp, pnl, mdd in verdict_rows:
        flag = " <-- n>=30 & exp>0" if n >= 30 and exp > 0 else ""
        print(f"  {label:<8s} {d}d{'':<2s} {n:>4d} {wr:>5.1f}% "
              f"${exp:>+8.3f} ${pnl:>+8.2f} ${mdd:>7.2f}{flag}")

    print("\nCaveats:")
    print("  - AI verdict substituted with a heuristic; min_short_confidence "
          "(LLM) NOT replayed — only min_short_composite is calibrated here.")
    print("  - Volume floor uses TODAY's dayNtlVlm snapshot (survivorship bias).")
    print(f"  - Portfolio accounting: max {args.max_concurrent} concurrent positions, "
          "one per coin, equity rebalances after each close (compounding).")
    print("  - Cooldown between entries is NOT applied; entries still follow the "
          "kernel's next-bar-open fill.")
    print("  - Exits/costs come from the unified kernel (production DSLTracker + "
          "H-7), matching scripts/backtest.py.")
    print("  - Past performance does NOT imply future results.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
