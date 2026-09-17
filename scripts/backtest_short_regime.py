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

Anti-look-ahead: entries decide on 1h bar i close and fill at bar i+1 open;
the 4h series is sliced to bars fully closed by the decision instant; the
macro regime at bar i uses only BTC closes up to that bar (EMA is causal,
so the full-length EMA array indexed at i equals the prefix EMA — identical
math to trend_from_closes on the prefix).

Caveats: the AI verdict is substituted by the same deterministic heuristic
as scripts/backtest.py, so min_short_confidence (an LLM output) CANNOT be
replayed — the composite score stands in for min_short_composite only; the
$50M volume floor uses TODAY's dayNtlVlm snapshot (survivorship bias); one
open position per coin; equity held constant; DSL modelled with the base
3-param ladder (max_loss/protect/retrace — the live phase2_tiers /
regime_aware refinements are not replayed).

Usage:
    HERMES_AGENT_CONFIG_FILE=/tmp/hermes-agent-config.json \
        python3 scripts/backtest_short_regime.py --days 180 --coins 20
"""
from __future__ import annotations

import argparse
import bisect
import math
import os
import sys
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
sys.path.insert(0, str(_REPO / "scripts"))

import backtest as bt  # noqa: E402  (Trade/DSL/_evaluate/_closed_slice/...)
from hermes_trader.agents.config import get_config  # noqa: E402
from hermes_trader.agents.config_store import cfg_get, read_agent_config  # noqa: E402
from hermes_trader.agents.ta_filter import late_entry_check  # noqa: E402
from hermes_trader.client.hl_client import fetch_hl_candles  # noqa: E402
from hermes_trader.client.universe import get_universe  # noqa: E402
from hermes_trader.indicators import math as ind  # noqa: E402
from hermes_trader.models.types import Candle  # noqa: E402

WINDOW = 150       # trailing 1h indicator window (suffix; all triggers used
                   # here have <= ~60-bar memory or decaying EMA memory with
                   # <1% residual weight past 150 bars) — keeps the scan O(n)
W4_WINDOW = 60     # trailing 4h suffix fed to late_entry_check
SCAN_SCORES = (20.0, 25.0, 30.0, 40.0)  # variant-E min_short_composite sweep


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
                     cfg: Dict[str, Any], le_params: Dict[str, Any],
                     warmup: int, sim_ms: int) -> tuple:
    """Pass 1 (run ONCE per coin): every bar where the BASE short signal
    fires — bearish heuristic verdict + ta_confirmed proxy + live ta_late
    short-mirror hard gate. Records the per-variant filter inputs so the
    variants can replay without rescanning indicators."""
    t4 = [c.t for c in candles_4h] if candles_4h else []
    closes4 = [c.c for c in candles_4h] if candles_4h else []
    ema8_4 = ind.ema(closes4, 8) if len(closes4) >= 8 else []
    ema21_4 = ind.ema(closes4, 21) if len(closes4) >= 21 else []
    cands: List[Dict[str, Any]] = []
    stats = {"bearish": 0, "signal": 0, "ta_conf": 0, "late_veto": 0}
    for i in range(warmup, len(candles) - 1):
        bar = candles[i]
        window = candles[max(0, i + 1 - WINDOW): i + 1]
        score, hits = bt._evaluate(window, cfg)
        bullish, atr_pct, adx14 = bt._trend_and_atr_pct(window)
        if bullish is None or bullish:
            continue  # shorts only; None = insufficient data
        stats["bearish"] += 1
        verdict = bt._heuristic_verdict(score, hits, bullish, atr_pct)
        if verdict != "SHORT":
            continue
        stats["signal"] += 1
        burst = any(h["name"] == "momentumBurst" and h["fired"] for h in hits)
        if not bt._ta_confirmed(bullish, atr_pct, adx14, score) and not burst:
            continue
        stats["ta_conf"] += 1
        decision_ms = bar.t + sim_ms
        # Live ta_late_entry hard gate (short mirror), closed 4h bars only.
        if le_params and candles_4h:
            w4 = bt._closed_slice(candles_4h, t4, decision_ms, bt._MS_PER["4h"])
            if w4:
                w4 = w4[-W4_WINDOW:]
            le = late_entry_check(w4, None, "short", le_params)
            if le.get("block"):
                stats["late_veto"] += 1
                continue
        # Variant-B input: own 4h downtrend on closed bars (EMA8 < EMA21).
        j4 = bisect.bisect_right(t4, decision_ms - bt._MS_PER["4h"])
        own_down = False
        if j4 >= 22 and ema8_4 and ema21_4:
            e8, e21 = ema8_4[j4 - 1], ema21_4[j4 - 1]
            own_down = math.isfinite(e8) and math.isfinite(e21) and e8 < e21
        # Variant-C input: macro regime from the last CLOSED BTC 1h bar.
        jm = bisect.bisect_right(macro_ts, bar.t) - 1
        macro = macro_regime[jm] if 0 <= jm < len(macro_regime) else "neutral"
        cands.append({"bar": i, "score": score, "own_down": own_down,
                      "macro": macro, "atr_pct": atr_pct})
    return cands, stats


def _simulate_shorts(coin: str, candles: List[Candle],
                     cands: List[Dict[str, Any]], max_lev: int, *,
                     equity: float, equity_fraction: float, lev_ceiling: int,
                     max_loss_pct: float, protect_pct: float,
                     retrace_threshold: float,
                     admit: Callable[[Dict[str, Any]], bool],
                     entry_slip_bps: float, exit_slip_bps: float,
                     stop_delay_slip_bps: float) -> List[bt.Trade]:
    """Pass 2: DSL position management over the admitted candidates. Mirrors
    backtest._simulate's short branch exactly (entry at next bar open with
    adverse slip, H-7 exit costs, ROUND_TRIP_FEE_BPS)."""
    by_bar = {c["bar"]: c for c in cands}
    fee_pct = bt.ROUND_TRIP_FEE_BPS / 10000.0

    def _fill(px: float, is_buy: bool, bps: float) -> float:
        adj = px * bps / 10000.0
        return px + adj if is_buy else px - adj

    trades: List[bt.Trade] = []
    open_t: Optional[bt.Trade] = None
    open_dsl: Optional[bt.DSL] = None
    for i in range(len(candles) - 1):
        bar = candles[i]
        if open_t is not None and open_dsl is not None:
            done, exit_ref, reason = open_dsl.check_bar(i, bar)
            if done:
                is_stop = reason.startswith("max_loss")
                slip = exit_slip_bps + (stop_delay_slip_bps if is_stop else 0.0)
                exit_px = _fill(exit_ref, True, slip)  # closing a short = BUY
                gross = (open_t.entry_px - exit_px) / open_t.entry_px
                open_t.exit_bar = i
                open_t.exit_px = exit_px
                open_t.pnl_usd = open_t.notional * (gross - fee_pct)
                open_t.exit_reason = reason
                trades.append(open_t)
                open_t = open_dsl = None
                continue
        if open_t is None and i in by_bar and admit(by_bar[i]):
            next_bar = candles[i + 1]
            lev = min(lev_ceiling, max_lev)
            notional = equity * equity_fraction * lev
            entry_px = _fill(next_bar.o, False, entry_slip_bps)  # SELL below
            open_t = bt.Trade(coin=coin, side="short", entry_bar=i + 1,
                              entry_px=entry_px, notional=notional,
                              margin=equity * equity_fraction, leverage=lev)
            open_dsl = bt.DSL(side="short", entry_px=entry_px, entry_bar=i + 1,
                              peak_px=entry_px, max_loss_pct=max_loss_pct,
                              protect_pct=protect_pct,
                              retrace_threshold=retrace_threshold)
    return trades


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


def _window_metrics(trades: List[bt.Trade], candles: List[Candle],
                    days: int, equity: float) -> Dict[str, Any]:
    """Stats restricted to trades ENTERED within the trailing `days` window."""
    cutoff = candles[-1].t - days * 86_400_000
    sub = [t for t in trades if candles[t.entry_bar].t >= cutoff]
    m = bt._split_metrics(sub, equity)
    reasons: Dict[str, int] = {}
    for t in sub:
        reasons[t.exit_reason] = reasons.get(t.exit_reason, 0) + 1
    m["reasons"] = reasons
    return m


def _merge_metrics(per_coin: List[Dict[str, Any]], equity: float) -> Dict[str, Any]:
    """Merge per-coin _window_metrics dicts into portfolio-level stats."""
    n = sum(m.get("n", 0) for m in per_coin)
    out: Dict[str, Any] = {"n": n}
    if n == 0:
        return out
    wins = sum(m.get("wins", 0) for m in per_coin)
    pnl = sum(m.get("pnl", 0.0) for m in per_coin)
    out.update(n=n, wins=wins, win_rate=wins / n * 100, pnl=pnl,
               expectancy=pnl / n, pnl_pct=pnl / equity * 100,
               max_dd=sum(m.get("max_dd", 0.0) for m in per_coin))
    reasons: Dict[str, int] = {}
    for m in per_coin:
        for k, v in (m.get("reasons") or {}).items():
            reasons[k] = reasons.get(k, 0) + v
    out["reasons"] = reasons
    return out


def _print_variant(label: str, desc: str, merged: Dict[int, Dict[str, Any]],
                   windows: tuple) -> None:
    print(f"\n--- Variant {label}: {desc} ---")
    hdr = f"  {'window':<7s} {'n':>4s} {'win%':>6s} {'exp/trade':>10s} " \
          f"{'PnL':>9s} {'ret%':>7s} {'maxDD':>8s}  exits"
    print(hdr)
    for d in windows:
        m = merged.get(d) or {}
        if not m.get("n"):
            print(f"  {d}d{'':<4s} {'0':>4s} {'-':>6s} {'-':>10s} "
                  f"{'-':>9s} {'-':>7s} {'-':>8s}  -")
            continue
        print(f"  {d}d{'':<4s} {m['n']:>4d} {m['win_rate']:>5.1f}% "
              f"${m['expectancy']:>+8.3f} ${m['pnl']:>+8.2f} "
              f"{m['pnl_pct']:>+6.1f}% ${m['max_dd']:>7.2f}  {m['reasons']}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=int, default=180,
                    help="history to pull (results are also split at 90d)")
    ap.add_argument("--coins", type=int, default=20)
    ap.add_argument("--equity", type=float, default=100.0)
    ap.add_argument("--min-vol", type=float, default=50e6,
                    help="24h notional volume floor for the universe (USD)")
    ap.add_argument("--entry-slip-bps", type=float, default=bt.DEFAULT_ENTRY_SLIP_BPS)
    ap.add_argument("--exit-slip-bps", type=float, default=bt.DEFAULT_EXIT_SLIP_BPS)
    ap.add_argument("--stop-delay-slip-bps", type=float,
                    default=bt.DEFAULT_STOP_DELAY_SLIP_BPS)
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
    sim_ms = bt._MS_PER[interval]
    total_bars = args.days * 24 + 100  # + warmup
    need_4h = math.ceil(total_bars * sim_ms / bt._MS_PER["4h"]) + 40

    cfg = get_config()
    cfg["_interval"] = interval

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
    print(f"cost model (H-7): {bt.ROUND_TRIP_FEE_BPS:.1f}bps RT fee + "
          f"{args.entry_slip_bps:.1f}bps entry / {args.exit_slip_bps:.1f}bps exit slip + "
          f"{args.stop_delay_slip_bps:.1f}bps stop-out delay")
    print(f"live short gates for reference: min_short_confidence={live_min_short_conf} "
          f"(NOT replayable — LLM output), min_short_composite={live_min_short_comp}")

    # Macro regime series from BTC 1h closes (production classifier params).
    print("\nfetching BTC 1h for macro regime series ...")
    btc = fetch_hl_candles("BTC", interval, total_bars)
    btc_closes = [c.c for c in btc]
    macro_ts = [c.t for c in btc]
    macro_regime = _macro_regime_series(btc_closes, fast_p, slow_p, slope_up,
                                        lookback)
    n_down = sum(1 for r in macro_regime if r == "down")
    n_up = sum(1 for r in macro_regime if r == "up")
    print(f"BTC bars: {len(btc)}   regime mix: up {n_up / len(btc) * 100:.0f}% / "
          f"down {n_down / len(btc) * 100:.0f}% / "
          f"neutral {(len(btc) - n_up - n_down) / len(btc) * 100:.0f}%")

    universe = get_universe()
    perps = [m for m in universe
             if m["type"] == "perp" and not m["coin"].startswith("@")
             and float(m.get("dayNtlVlm", 0) or 0) >= args.min_vol]
    coins = sorted(perps, key=lambda m: float(m.get("dayNtlVlm", 0) or 0),
                   reverse=True)[: args.coins]
    print(f"universe: {len(perps)} perps pass the volume floor; "
          f"simulating top {len(coins)}: "
          f"{', '.join(m['coin'] for m in coins)}\n")

    variant_labels = ["A", "B", "C", "D"] + [f"E>={s:g}" for s in SCAN_SCORES]
    trades_by_variant: Dict[str, List[bt.Trade]] = {v: [] for v in variant_labels}
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
        cands, stats = _scan_candidates(coin, candles, candles_4h, macro_ts,
                                        macro_regime, cfg, le_params,
                                        warmup=100, sim_ms=sim_ms)
        for k in total_stats:
            total_stats[k] += stats[k]
        for label in variant_labels:
            if label.startswith("E"):
                ms = float(label.split(">=")[1])
                admit = _variant_admit("E", ms)
            else:
                admit = _variant_admit(label)
            trades_by_variant[label].extend(_simulate_shorts(
                coin, candles, cands, max_lev, equity=args.equity,
                equity_fraction=equity_fraction, lev_ceiling=lev_ceiling,
                max_loss_pct=max_loss, protect_pct=protect, retrace_threshold=retrace,
                admit=admit, entry_slip_bps=args.entry_slip_bps,
                exit_slip_bps=args.exit_slip_bps,
                stop_delay_slip_bps=args.stop_delay_slip_bps))
        print(f"  {coin:8s} bars={len(candles)}  base short candidates={len(cands)} "
              f"(bearish {stats['bearish']} → signal {stats['signal']} → "
              f"ta_conf {stats['ta_conf']}, late-veto {stats['late_veto']})")

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
        merged: Dict[int, Dict[str, Any]] = {}
        for d in windows:
            per_coin = []
            for coin, candles in candles_by_coin.items():
                ct = [t for t in trades_by_variant[label] if t.coin == coin]
                per_coin.append(_window_metrics(ct, candles, d, args.equity))
            merged[d] = _merge_metrics(per_coin, args.equity)
        _print_variant(label, descs[label], merged, windows)
        for d in windows:
            m = merged[d]
            verdict_rows.append((label, d, m.get("n", 0), m.get("win_rate", 0.0),
                                 m.get("expectancy", 0.0), m.get("pnl", 0.0),
                                 m.get("max_dd", 0.0)))

    print("\n=== DECISION GRID (expectancy is per-trade USD on $"
          f"{args.equity:.0f} equity) ===")
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
    print("  - DSL modelled with the base 3-param ladder; live phase2_tiers / "
          "regime_aware / breakeven refinements not replayed.")
    print("  - One open position per coin; equity constant; no cooldown; "
          "max_concurrent cap NOT enforced across coins.")
    print("  - 150-bar 1h / 60-bar 4h indicator windows (exponential-memory "
          "approximation, <1% residual).")
    print("  - Past performance does NOT imply future results.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
