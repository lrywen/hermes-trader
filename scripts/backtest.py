#!/usr/bin/env python3
"""Backtest the hermes-trader strategy on historical Hyperliquid candles.

Walks 1h-bar history per coin, evaluates the same triggers + TA-filter
logic as the live scanner, simulates entries with the current sizing
formula (equity_fraction x per-coin-max leverage), and exits via the DSL
two-phase trailing-stop engine. PnL is net of the round-trip taker fee AND
adverse entry/exit slippage plus a stop-out delay penalty (H-7 cost model;
--no-slippage restores the fee-only baseline).

The AI research step is *substituted* with a deterministic heuristic that
mirrors the system prompt's entry rules — calling OpenRouter per signal
over historical bars would be too expensive. The mechanical edge is
tested; real AI judgment is not.

Caveats reported in the summary so they aren't lost.

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
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

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
from hermes_trader.agents.config_store import read_agent_config, cfg_get
from hermes_trader.agents.ta_filter import late_entry_check
from hermes_trader.client.hl_client import fetch_hl_candles
from hermes_trader.client.universe import get_universe
from hermes_trader.indicators import math as ind
from hermes_trader.indicators import triggers as trig
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


@dataclass
class Trade:
    coin: str
    side: str           # "long" or "short"
    entry_bar: int
    entry_px: float     # filled entry price AFTER adverse entry slippage (H-7)
    notional: float
    margin: float
    leverage: int
    exit_bar: int = 0
    exit_px: float = 0.0  # filled exit price AFTER adverse exit slippage (H-7)
    pnl_usd: float = 0.0
    exit_reason: str = ""
    # O-7 (supplemental audit 2026-08-30): in-sample vs out-of-sample tag for
    # walk-forward validation. A trade entered on/after the split bar is OOS.
    in_sample: bool = True


@dataclass
class DSL:
    """Local re-implementation of dsl_exit's two-phase trailing stop."""
    side: str
    entry_px: float
    entry_bar: int
    peak_px: float
    max_loss_pct: float = 2.5
    protect_pct: float = 1.5
    retrace_threshold: float = 0.30
    hard_timeout_bars: int = 180

    def check_bar(self, bar_idx: int, bar: Candle) -> Tuple[bool, float, str]:
        """Did this bar trigger an exit? Stops fire intra-bar at the stop price."""
        is_long = self.side == "long"
        # NOTE: the peak is deliberately NOT updated before the stop checks below.
        # Deriving this bar's trailing floor from this bar's own high, then testing
        # it against this bar's own low, is intra-bar lookahead: as retrace -> 0 the
        # floor -> bar.h and `bar.l <= floor` becomes unconditionally true, so the
        # sim sells at every bar's exact high. Stops must only ever be evaluated
        # against a floor that was already known when the bar opened; the peak is
        # advanced at the end of the bar instead.

        if bar_idx - self.entry_bar >= self.hard_timeout_bars:
            return True, bar.c, "hard_timeout"

        # Max-loss stop. On a gap through the stop the fill is the open, not the
        # stop price -- a resting stop cannot fill better than the market.
        max_loss_px = (self.entry_px * (1 - self.max_loss_pct / 100) if is_long
                       else self.entry_px * (1 + self.max_loss_pct / 100))
        if is_long and bar.l <= max_loss_px:
            return True, min(max_loss_px, bar.o), f"max_loss {self.max_loss_pct}%"
        if not is_long and bar.h >= max_loss_px:
            return True, max(max_loss_px, bar.o), f"max_loss {self.max_loss_pct}%"

        # Phase-2 trailing floor (only active once protect_pct profit reached).
        # Uses the peak as of the *previous* bar's close, so the floor is knowable
        # before this bar trades.
        if is_long:
            peak_profit_pct = (self.peak_px - self.entry_px) / self.entry_px * 100
            if peak_profit_pct >= self.protect_pct:
                profit_range = self.peak_px - self.entry_px
                floor = self.entry_px + profit_range * (1 - self.retrace_threshold)
                if bar.l <= floor:
                    return True, min(floor, bar.o), "trailing_stop"
        else:
            peak_profit_pct = (self.entry_px - self.peak_px) / self.entry_px * 100
            if peak_profit_pct >= self.protect_pct:
                profit_range = self.entry_px - self.peak_px
                ceiling = self.entry_px - profit_range * (1 - self.retrace_threshold)
                if bar.h >= ceiling:
                    return True, max(ceiling, bar.o), "trailing_stop"

        # Bar survived: now advance the peak so the *next* bar sees this extreme.
        if is_long and bar.h > self.peak_px:
            self.peak_px = bar.h
        if not is_long and bar.l < self.peak_px:
            self.peak_px = bar.l

        return False, 0.0, ""


def _evaluate(window: List[Candle], cfg: Dict[str, Any]) -> Tuple[float, list]:
    """Run the 6 live triggers + composite score on the trailing window."""
    th, w = cfg["thresholds"], cfg["weights"]
    hits = [
        trig.pct_move_spike(window, th["sigmaThreshold"]),
        trig.volume_spike(window, th["sigmaThreshold"]),
        trig.breakout(
            window, th["breakoutLookback"],
            min_rvol=th.get("breakoutMinRvol", 1.5),
            rvol_window=th.get("breakoutRvolWindow", 20),
            atr_score_mult=th.get("breakoutAtrScoreMult", 3.0),
        ),
        trig.range_compression(window, th["bbLength"], th["bbStdDev"]),
        trig.trend_strength(window, th["adxPeriod"]),
        trig.momentum_burst(window, th["momentumLookback"], th["momentumPct"]),
    ]
    return trig.composite_score(hits, w), hits


def _trend_and_atr_pct(window: List[Candle]) -> Tuple[Optional[bool], Optional[float], Optional[float]]:
    """4h-style EMA trend, ATR% of price, ADX(14). None if insufficient data."""
    closes = [c.c for c in window]
    if len(closes) < 30:
        return None, None, None
    e8 = ind.ema(closes, 8)[-1]; e21 = ind.ema(closes, 21)[-1]
    if not (math.isfinite(e8) and math.isfinite(e21)):
        return None, None, None
    a = ind.atr(window, 14)[-1]
    if not math.isfinite(a) or closes[-1] == 0:
        return None, None, None
    atr_pct = a / closes[-1] * 100
    adx14 = ind.adx(window, 14)[-1]
    return e8 > e21, atr_pct, (adx14 if math.isfinite(adx14) else None)


def _heuristic_verdict(score: float, hits, bullish: Optional[bool],
                       atr_pct: Optional[float]) -> Optional[str]:
    """Stand-in for AI: 'score >= 25 OR directional trend with ATR >= 0.4%'."""
    if bullish is None:
        return None
    burst = any(h["name"] == "momentumBurst" and h["fired"] for h in hits)
    score_ok = score >= 25
    trend_ok = atr_pct is not None and atr_pct >= 0.4
    if not (score_ok or trend_ok or burst):
        return None
    return "LONG" if bullish else "SHORT"


def _ta_confirmed(bullish, atr_pct, adx14, composite: float) -> bool:
    """Local proxy for ta_filter.analyze_perception's CONFIRMED gate (score >= 45)."""
    if bullish is None or atr_pct is None:
        return False
    s = 20  # trend present
    if 30 < (atr_pct * 10) < 700:  # very loose proxy for RSI window
        s += 15
    if atr_pct >= 0.5:
        s += 15
    if adx14 is not None and adx14 >= 25:
        s += 15
    s += min(15, composite / 100 * 15)
    return s >= 45


def _simulate(coin: str, candles: List[Candle], max_lev: int, *,
              equity: float, equity_fraction: float, lev_ceiling: int,
              cfg: Dict[str, Any], warmup: int = 100,
              max_loss_pct: float = 2.5, protect_pct: float = 1.5,
              retrace_threshold: float = 0.30,
              atr_mult: float = 0.0, atr_floor: float = 1.0,
              atr_ceiling: float = 4.0,
              stop_widths: Optional[list] = None,
              candles_4h: Optional[List[Candle]] = None,
              candles_15m: Optional[List[Candle]] = None,
              late_entry_params: Optional[Dict[str, Any]] = None,
              late_vetoes: Optional[List[dict]] = None,
              entry_slip_bps: float = DEFAULT_ENTRY_SLIP_BPS,
              exit_slip_bps: float = DEFAULT_EXIT_SLIP_BPS,
              stop_delay_slip_bps: float = DEFAULT_STOP_DELAY_SLIP_BPS,
              fee_bps: float = ROUND_TRIP_FEE_BPS,
              oos_split_bar: Optional[int] = None) -> List[Trade]:
    trades: List[Trade] = []
    open_t: Optional[Trade] = None
    open_dsl: Optional[DSL] = None
    # O-8 (supplemental audit 2026-08-30): fee_bps can be overridden per coin
    # from memory.avg_round_trip_fee_bps (measured exchange fees); defaults to
    # the conservative ROUND_TRIP_FEE_BPS constant when history is thin.
    fee_pct = fee_bps / 10000.0

    # H-7: adverse-fill helpers. A marketable IOC BUY fills ABOVE the reference
    # price and a SELL fills BELOW; ``bps`` is the adverse fraction in bps.
    def _fill(px: float, is_buy: bool, bps: float) -> float:
        adj = px * bps / 10000.0
        return px + adj if is_buy else px - adj

    # ta_late_entry parity (deep audit 高危项, 2026-08-30): the live
    # ta_late_entry_gate re-runs late_entry_check() on FRESH 4h (+15m) candles
    # immediately before order placement. The backtest calls the SAME pure
    # function on only the higher-TF bars that have CLOSED by the decision
    # instant (bar i close → fill at i+1 open), so the veto is 100% identical
    # in rules and free of look-ahead. Backtests evaluate the FINAL rule set,
    # so the check is enforced regardless of the live gate's gray-release
    # mode (mode is a deployment control, not a strategy difference).
    le_params = dict(late_entry_params or {})
    le_enabled = bool(le_params) and candles_4h is not None
    if le_enabled:
        le_params.pop("mode", None)
        le_params.pop("shadow_log_path", None)
    sim_ms = _MS_PER.get(cfg.get("_interval", "1h"), _MS_PER["1h"])
    t4 = [c.t for c in candles_4h] if candles_4h else []
    t15 = [c.t for c in candles_15m] if candles_15m else []

    for i in range(warmup, len(candles) - 1):
        window = candles[: i + 1]
        bar = candles[i]
        next_bar = candles[i + 1]

        # Manage open position
        if open_t and open_dsl:
            done, exit_ref, reason = open_dsl.check_bar(i, bar)
            if done:
                # H-7: the DSL stop price is a TRIGGER, not a fill. The live
                # exit waits for mid-hold + oracle confirm then crosses the
                # book with a marketable IOC — always adverse, and a stop-out
                # in a fast move overshoots (extra delay penalty). Closing a
                # long = SELL (fills below ref); closing a short = BUY (fills
                # above). Trail/timeout exits pay the regular exit slip.
                is_stop_out = reason.startswith("max_loss")
                slip = exit_slip_bps + (stop_delay_slip_bps if is_stop_out else 0.0)
                close_is_buy = open_t.side == "short"
                exit_px = _fill(exit_ref, close_is_buy, slip)
                gross_pct = ((exit_px - open_t.entry_px) / open_t.entry_px
                             if open_t.side == "long"
                             else (open_t.entry_px - exit_px) / open_t.entry_px)
                open_t.exit_bar = i
                open_t.exit_px = exit_px
                open_t.pnl_usd = open_t.notional * (gross_pct - fee_pct)
                open_t.exit_reason = reason
                # O-7: classify by ENTRY bar — a position opened before the split
                # that exits after it still belongs to the in-sample generation
                # (the decision used only in-sample information).
                if oos_split_bar is not None:
                    open_t.in_sample = open_t.entry_bar < oos_split_bar
                trades.append(open_t)
                open_t = open_dsl = None
            else:
                continue   # one open trade per coin at a time

        # Look for entry
        score, hits = _evaluate(window, cfg)
        bullish, atr_pct, adx14 = _trend_and_atr_pct(window)
        verdict = _heuristic_verdict(score, hits, bullish, atr_pct)
        if verdict is None:
            continue
        burst = any(h["name"] == "momentumBurst" and h["fired"] for h in hits)
        if not _ta_confirmed(bullish, atr_pct, adx14, score) and not burst:
            continue

        side = "long" if verdict == "LONG" else "short"

        # Live late-entry hard gate, same pure function + same closed-bar
        # information set as the order-time recompute.
        if le_enabled:
            decision_ms = bar.t + sim_ms
            w4 = _closed_slice(candles_4h, t4, decision_ms, _MS_PER["4h"])
            w15 = _closed_slice(candles_15m, t15, decision_ms, _MS_PER["15m"])
            le = late_entry_check(w4, w15, side, le_params)
            if le.get("block"):
                if late_vetoes is not None:
                    late_vetoes.append({
                        "coin": coin, "side": side, "bar": i,
                        "reason": le.get("reason", ""),
                        "rsi4h": le.get("rsi4h"), "adx4h": le.get("adx4h"),
                        "extension": le.get("extension"),
                    })
                continue

        lev = min(lev_ceiling, max_lev)
        notional = equity * equity_fraction * lev
        margin = equity * equity_fraction
        # H-7: entry IOC fills adverse to next bar's open (long BUY fills above,
        # short SELL fills below). The stop/trail ladder anchors on the FILLED
        # price, matching live dsl_exit which tracks the actual entry price.
        entry_px = _fill(next_bar.o, side == "long", entry_slip_bps)
        open_t = Trade(coin=coin, side=side, entry_bar=i + 1, entry_px=entry_px,
                       notional=notional, margin=margin, leverage=lev)
        # ATR-stop mode: stop width = atr_mult × ATR% at entry, clamped — mirrors
        # the live dsl_exit.atr_stop feature. atr_mult=0 keeps the fixed stop.
        eff_max_loss = max_loss_pct
        if atr_mult > 0 and atr_pct is not None and atr_pct > 0:
            eff_max_loss = min(max(atr_pct * atr_mult, atr_floor), atr_ceiling)
            if stop_widths is not None:
                stop_widths.append(eff_max_loss)
        open_dsl = DSL(side=side, entry_px=entry_px, entry_bar=i + 1,
                       peak_px=entry_px, max_loss_pct=eff_max_loss,
                       protect_pct=protect_pct, retrace_threshold=retrace_threshold)
    return trades


# O-7 (supplemental audit 2026-08-30): walk-forward / out-of-sample split.
def oos_split_index(n_bars: int, warmup: int, oos_frac: float) -> int:
    """Bar index at which the out-of-sample window starts.

    Only bars in [warmup, n_bars) can generate entries (the loop decision
    window), so the split is placed ``oos_frac`` of the way through THAT
    window. Trades with entry_bar >= the returned index are out-of-sample.
    The fixed ``warmup`` indicator prefix is always in-sample (no leakage —
    indicators on OOS bars only read past bars).
    """
    if n_bars <= warmup:
        return n_bars  # nothing tradeable → all "in-sample"
    return int(warmup + (n_bars - warmup) * (1.0 - oos_frac))


def _split_metrics(trades: List[Trade], equity: float) -> Dict[str, Any]:
    """Stats for one walk-forward segment: count, win rate, expectancy, total
    PnL, per-trade Sharpe (365/active-days annualization), and the peak-to-trough
    max drawdown of the cumulative-PnL path (USD)."""
    n = len(trades)
    out: Dict[str, Any] = {"n": n}
    if n == 0:
        return out
    pnls = [t.pnl_usd for t in trades]
    wins = sum(1 for p in pnls if p > 0)
    total = sum(pnls)
    out.update(
        n=n, wins=wins, win_rate=wins / n * 100,
        expectancy=total / n, pnl=total, pnl_pct=total / equity * 100,
    )
    # Per-trade Sharpe, annualized over the span the trades actually cover.
    # One trade per coin at a time but many coins run concurrently, so the
    # trading span is taken from first entry to last exit bar.
    mean = total / n
    var = sum((p - mean) ** 2 for p in pnls) / (n - 1) if n > 1 else 0.0
    sd = math.sqrt(var)
    span_days = 0
    try:
        # exit_bar - entry_bar differences are per-coin; the segment's calendar
        # span is (last exit - first entry) of the whole merged trade list, but
        # bars here are per-coin indices so we only use the mean holding span as
        # a conservative per-trade horizon. Sharpe is reported for relative
        # IS-vs-OOS comparison, not as an absolute fund Sharpe.
        span_days = max(
            1.0,
            sum((t.exit_bar - t.entry_bar) for t in trades) / n / 24.0,
        )
    except Exception:
        span_days = 1.0
    if sd > 0:
        out["sharpe"] = mean / sd * math.sqrt(365.0 / span_days)
    else:
        out["sharpe"] = 0.0
    # Max drawdown over the chronological cumulative-PnL path. Trades from
    # different coins interleave on bars; ordering by exit bar gives a close
    # approximation of the realized equity curve (constant-equity assumption).
    ordered = sorted(trades, key=lambda t: (t.exit_bar, t.coin))
    peak = 0.0
    cum = 0.0
    mdd = 0.0
    for t in ordered:
        cum += t.pnl_usd
        if cum > peak:
            peak = cum
        dd = peak - cum
        if dd > mdd:
            mdd = dd
    out["max_dd"] = mdd
    return out


def _print_walk_forward(all_trades: List[Trade], equity: float) -> None:
    """O-7: print the in-sample vs out-of-sample comparison block."""
    is_tr = [t for t in all_trades if t.in_sample]
    oos_tr = [t for t in all_trades if not t.in_sample]
    print("\n=== WALK-FORWARD / OUT-OF-SAMPLE (O-7) ===")
    if not oos_tr:
        print("no out-of-sample trades (raise --oos-frac or widen the window)")
        return
    ms = _split_metrics(is_tr, equity)
    mo = _split_metrics(oos_tr, equity)

    print(f"  {'Segment':<22s} {'IN-SAMPLE':>14s} {'OUT-OF-SAMPLE':>14s}")
    print(f"  {'-'*22} {'-'*14} {'-'*14}")

    def _line(label: str, vs: str, vo: str) -> None:
        print(f"  {label:<22s} {vs:>14s} {vo:>14s}")

    _line("trades", str(ms.get("n", 0)), str(mo.get("n", 0)))
    _line("win rate",
          f"{ms['win_rate']:.1f}%" if ms.get("n") else "-",
          f"{mo['win_rate']:.1f}%" if mo.get("n") else "-")
    _line("expectancy/trade",
          f"${ms['expectancy']:+.3f}" if ms.get("n") else "-",
          f"${mo['expectancy']:+.3f}" if mo.get("n") else "-")
    _line("total PnL",
          f"${ms['pnl']:+.2f}" if ms.get("n") else "-",
          f"${mo['pnl']:+.2f}" if mo.get("n") else "-")
    _line("return on equity",
          f"{ms['pnl_pct']:+.1f}%" if ms.get("n") else "-",
          f"{mo['pnl_pct']:+.1f}%" if mo.get("n") else "-")
    _line("Sharpe (per-trade)",
          f"{ms['sharpe']:.2f}" if ms.get("n") else "-",
          f"{mo['sharpe']:.2f}" if mo.get("n") else "-")
    _line("max drawdown",
          f"${ms['max_dd']:.2f}" if ms.get("n") else "-",
          f"${mo['max_dd']:.2f}" if mo.get("n") else "-")
    oos_ok = mo.get("n", 0) > 0 and mo.get("expectancy", 0.0) > 0
    print("\n  The strategy is validated out-of-sample only when the OOS "
          "expectancy/PnL stays positive and its Sharpe is in the same "
          "ballpark as in-sample — a large IS-OOS gap means overfitting.")
    print(f"  OOS verdict: {'EDGE HELD out-of-sample' if oos_ok else 'OOS edge not present — treat IS results with caution'}")


def _print_summary(all_trades: List[Trade], equity: float, days: int,
                   cost_note: str = "", oos: bool = False) -> None:
    print("\n=== SUMMARY ===")
    n = len(all_trades)
    if n == 0:
        print("no trades fired")
        if cost_note:
            print(f"\nCaveats:\n  - {cost_note}")
        return
    wins = [t for t in all_trades if t.pnl_usd > 0]
    losses = [t for t in all_trades if t.pnl_usd < 0]
    pnl_total = sum(t.pnl_usd for t in all_trades)
    avg_win = (sum(t.pnl_usd for t in wins) / len(wins)) if wins else 0.0
    avg_loss = (sum(t.pnl_usd for t in losses) / len(losses)) if losses else 0.0
    expectancy = pnl_total / n
    by_reason: Dict[str, int] = {}
    for t in all_trades:
        by_reason[t.exit_reason] = by_reason.get(t.exit_reason, 0) + 1

    print(f"trades        : {n}")
    print(f"win rate      : {len(wins)}/{n} = {len(wins) / n * 100:.1f}%")
    print(f"avg win       : ${avg_win:+.2f}")
    print(f"avg loss      : ${avg_loss:+.2f}")
    print(f"expectancy    : ${expectancy:+.3f} per trade")
    print(f"total PnL     : ${pnl_total:+.2f}  ({pnl_total / equity * 100:+.1f}% on ${equity:.0f}, over {days} days)")
    print(f"exit reasons  : {by_reason}")

    # Sample worst and best
    sorted_t = sorted(all_trades, key=lambda t: t.pnl_usd)
    print("\nworst 3       :")
    for t in sorted_t[:3]:
        print(f"  {t.coin:6} {t.side:5} bars {t.entry_bar}->{t.exit_bar}  "
              f"${t.pnl_usd:+.2f}  {t.exit_reason}")
    print("best 3        :")
    for t in sorted_t[-3:][::-1]:
        print(f"  {t.coin:6} {t.side:5} bars {t.entry_bar}->{t.exit_bar}  "
              f"${t.pnl_usd:+.2f}  {t.exit_reason}")

    print("\nCaveats:")
    print("  - AI verdict substituted with a heuristic (score / trend / burst). Real LLM not replayed.")
    if cost_note:
        print(f"  - {cost_note}")
    print("  - One open position per coin at a time; max_concurrent cap NOT enforced across coins.")
    print("  - Equity held constant (no compounding); cooldown_min not applied.")
    print("  - ta_late_entry hard gate is 100% aligned with live: same late_entry_check() "
          "pure function, ENFORCED (backtests evaluate the final rule set), and only "
          "4h/15m bars CLOSED by the decision instant are used — no look-ahead.")
    print("  - Past performance does NOT imply future results.")
    if oos:
        _print_walk_forward(all_trades, equity)


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
                         "100% parity with the live order-time gate)")
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

    cfg = get_config()
    cfg["_interval"] = args.interval
    universe = get_universe()
    perps = [m for m in universe if m["type"] == "perp" and not m["coin"].startswith("@")]
    coins = sorted(perps, key=lambda m: m.get("dayNtlVlm", 0), reverse=True)[: args.coins]

    print("=== hermes-trader backtest ===")
    print(f"period: {args.days} days   interval: {args.interval}   universe: top-{args.coins} by 24h volume")
    print(f"equity: ${args.equity:.0f}   fraction: {equity_fraction:.0%}   leverage ceiling: {leverage_ceiling}x")
    print(f"DSL: max_loss={max_loss}%  protect={protect}%  retrace={retrace}  atr_mult={atr_mult}")
    print(f"triggers config: sigma={cfg['thresholds']['sigmaThreshold']}  "
          f"momentumPct={cfg['thresholds']['momentumPct']}\n")

    all_trades: List[Trade] = []
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
            # O-7: walk-forward split. The decision window runs from `warmup`
            # (100) to the last candle; --oos-frac holds its tail out. The 100-
            # bar warmup prefix is always in-sample (indicators only read the
            # past, so it leaks nothing into OOS).
            coin_oos_bar = (oos_split_index(len(candles), 100, args.oos_frac)
                            if args.oos_frac > 0 else None)
            trades = _simulate(
                coin, candles, max_lev,
                equity=args.equity, equity_fraction=equity_fraction,
                lev_ceiling=leverage_ceiling, cfg=cfg,
                max_loss_pct=max_loss, protect_pct=protect,
                retrace_threshold=retrace,
                atr_mult=atr_mult, atr_floor=atr_floor,
                atr_ceiling=atr_ceiling, stop_widths=stop_widths,
                candles_4h=candles_4h, candles_15m=candles_15m,
                late_entry_params=late_entry_params, late_vetoes=late_vetoes,
                entry_slip_bps=entry_slip_bps, exit_slip_bps=coin_exit_slip,
                stop_delay_slip_bps=stop_delay_slip_bps,
                fee_bps=coin_fee_bps, oos_split_bar=coin_oos_bar,
            )
            pnl = sum(t.pnl_usd for t in trades)
            w = sum(1 for t in trades if t.pnl_usd > 0)
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

    _print_summary(all_trades, args.equity, args.days, cost_note=cost_note,
                   oos=args.oos_frac > 0)
    if stop_widths:
        sw = sorted(stop_widths)
        n = len(sw)
        print(f"\nATR stop widths (spot %): n={n}  "
              f"min={sw[0]:.2f}  p25={sw[n//4]:.2f}  median={sw[n//2]:.2f}  "
              f"p75={sw[3*n//4]:.2f}  max={sw[-1]:.2f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
