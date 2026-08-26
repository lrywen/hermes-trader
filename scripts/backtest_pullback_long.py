#!/usr/bin/env python3
"""A/B backtest: runner_entry_gate baseline vs "pullback long" bypass (Suggestion A).

Suggestion A was proposed after analysing XRP trades that were blocked because
they lacked a fresh impulse (volume+breakout/burst).  The bypass admits longs
that are in a confirmed uptrend, have at least one structural (slow-burn)
trigger, are NOT over-extended, and have pulled back to a lower-risk entry
zone (RSI < 70, price within 2 ATR of EMA21).

The script replays 1h bars for the top-N perps, runs the same deterministic
heuristic entry model as scripts/backtest.py (AI verdict substituted by a
score/trend/burst rule), and applies the runner_entry_gate in two modes:

  * BASELINE  -- fresh_impulse AND structure (current live logic)
  * PULLBACK  -- baseline OR pullback-long bypass

Both modes exit through the identical DSL two-phase trailing stop, so the
only difference is which entries are admitted.  Win rate and payoff ratio
are the primary comparison metrics.

Usage:
    python3 scripts/backtest_pullback_long.py
    python3 scripts/backtest_pullback_long.py --days 30 --coins 30
"""
from __future__ import annotations

import argparse
import math
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

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
sys.path.insert(0, str(_REPO))

from hermes_trader.agents.config import get_config
from hermes_trader.agents.config_store import read_agent_config, cfg_get
from hermes_trader.client.hl_client import fetch_hl_candles
from hermes_trader.client.universe import get_universe
from hermes_trader.indicators import math as ind
from hermes_trader.indicators import triggers as trig
from hermes_trader.models.types import Candle

ROUND_TRIP_FEE_BPS = 5.0


# ---------------------------------------------------------------------------
# Trade / DSL (mirrors scripts/backtest.py)
# ---------------------------------------------------------------------------

@dataclass
class Trade:
    coin: str
    side: str
    entry_bar: int
    entry_px: float
    notional: float
    via_pullback: bool = False
    exit_bar: int = 0
    exit_px: float = 0.0
    pnl_usd: float = 0.0
    exit_reason: str = ""


@dataclass
class DSL:
    side: str
    entry_px: float
    entry_bar: int
    peak_px: float
    max_loss_pct: float = 2.5
    protect_pct: float = 1.5
    retrace_threshold: float = 0.30
    hard_timeout_bars: int = 180

    def check_bar(self, bar_idx: int, bar: Candle) -> Tuple[bool, float, str]:
        is_long = self.side == "long"
        if bar_idx - self.entry_bar >= self.hard_timeout_bars:
            return True, bar.c, "hard_timeout"
        max_loss_px = (self.entry_px * (1 - self.max_loss_pct / 100) if is_long
                       else self.entry_px * (1 + self.max_loss_pct / 100))
        if is_long and bar.l <= max_loss_px:
            return True, min(max_loss_px, bar.o), f"max_loss {self.max_loss_pct}%"
        if not is_long and bar.h >= max_loss_px:
            return True, max(max_loss_px, bar.o), f"max_loss {self.max_loss_pct}%"
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
        if is_long and bar.h > self.peak_px:
            self.peak_px = bar.h
        if not is_long and bar.l < self.peak_px:
            self.peak_px = bar.l
        return False, 0.0, ""


# ---------------------------------------------------------------------------
# Indicator helpers
# ---------------------------------------------------------------------------

def _evaluate(window: List[Candle], cfg: Dict[str, Any]) -> Tuple[float, list]:
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


def _indicators(window: List[Candle]) -> Dict[str, Any]:
    """Compute EMA trend, ATR%, ADX, RSI, extension from EMA21."""
    closes = [c.c for c in window]
    out: Dict[str, Any] = {"bullish": None, "atr_pct": None, "adx14": None,
                           "rsi14": None, "extension_atr": None}
    if len(closes) < 30:
        return out
    e8 = ind.ema(closes, 8)[-1]
    e21 = ind.ema(closes, 21)[-1]
    if not (math.isfinite(e8) and math.isfinite(e21)):
        return out
    a = ind.atr(window, 14)[-1]
    if not math.isfinite(a) or closes[-1] == 0:
        return out
    out["bullish"] = e8 > e21
    out["atr_pct"] = a / closes[-1] * 100
    out["ema21"] = e21
    out["ema8"] = e8
    adx14 = ind.adx(window, 14)[-1]
    out["adx14"] = adx14 if math.isfinite(adx14) else None
    # RSI(14)
    if len(window) >= 16:
        rsi_arr = ind.rsi(window, 14)
        rsi_val = rsi_arr[-1] if len(rsi_arr) else float("nan")
        out["rsi14"] = rsi_val if math.isfinite(rsi_val) else None
    # Extension in ATR units from EMA21
    if a > 0 and e21 > 0:
        out["extension_atr"] = (closes[-1] - e21) / a
    return out


def _heuristic_verdict(score: float, hits, bullish: Optional[bool],
                       atr_pct: Optional[float]) -> Optional[str]:
    if bullish is None:
        return None
    burst = any(h["name"] == "momentumBurst" and h["fired"] for h in hits)
    score_ok = score >= 25
    trend_ok = atr_pct is not None and atr_pct >= 0.4
    if not (score_ok or trend_ok or burst):
        return None
    return "LONG" if bullish else "SHORT"


def _ta_confirmed(bullish, atr_pct, adx14, composite: float) -> bool:
    if bullish is None or atr_pct is None:
        return False
    s = 20
    if 30 < (atr_pct * 10) < 700:
        s += 15
    if atr_pct >= 0.5:
        s += 15
    if adx14 is not None and adx14 >= 25:
        s += 15
    s += min(15, composite / 100 * 15)
    return s >= 45


# ---------------------------------------------------------------------------
# runner_entry_gate model (mirrors executor._runner_entry_block_reason)
# ---------------------------------------------------------------------------

def _runner_gate_allows(
    side: str,
    score: float,
    hits: list,
    inds: Dict[str, Any],
    gate_cfg: Dict[str, Any],
    allow_pullback: bool,
) -> Tuple[bool, bool]:
    """Return (allowed, via_pullback).

    Models the live gate's fresh_impulse + structure logic, plus optionally
    the pullback-long bypass (Suggestion A).
    """
    volume = any(h["name"] == "volumeSpike" and h["fired"] for h in hits)
    breakout = any(h["name"] == "breakout" and h["fired"] for h in hits)
    burst = any(h["name"] == "momentumBurst" and h["fired"] for h in hits)
    # Slow-burn proxy: structural (non-spike) triggers.
    slow_count = sum(
        1 for h in hits
        if h["fired"] and h["name"] in ("rangeCompression", "trendStrength")
    )
    min_score = float(gate_cfg.get("min_composite", 45.0))
    rsi_overbought = float(gate_cfg.get("rsi_overbought", 75.0))
    max_ext = float(gate_cfg.get("max_extension_atr", 2.5))

    bullish = inds.get("bullish")
    rsi14 = inds.get("rsi14")
    ext = inds.get("extension_atr")

    fresh_impulse = (volume and (breakout or burst)) or (burst and score >= min_score)
    structured_runner = fresh_impulse and (slow_count >= 1 or score >= min_score)

    # Common late-entry vetoes
    if side == "long":
        if rsi14 is not None and rsi14 > rsi_overbought:
            return False, False
        if ext is not None and ext > max_ext:
            return False, False

    if side == "short":
        if not bool(gate_cfg.get("allow_shorts", True)):
            return False, False
        downtrend = bullish is False
        structured_short = (
            downtrend
            or (score >= min_score and (slow_count >= 1 or fresh_impulse))
            or (fresh_impulse and score >= min_score)
        )
        return structured_short, False

    # side == "long"
    if structured_runner:
        return True, False

    # --- Pullback-long bypass (Suggestion A) ---
    if allow_pullback and side == "long":
        pb_min_score = float(gate_cfg.get("pullback_min_composite", 20.0))
        pb_max_rsi = float(gate_cfg.get("pullback_max_rsi", 70.0))
        pb_max_ext = float(gate_cfg.get("pullback_max_extension_atr", 2.0))
        pullback_long = (
            bullish is True
            and slow_count >= 1
            and score >= pb_min_score
            and not fresh_impulse          # only bypasses when no fresh impulse
            and (rsi14 is None or rsi14 < pb_max_rsi)
            and (ext is None or ext < pb_max_ext)
        )
        if pullback_long:
            return True, True

    return False, False


# ---------------------------------------------------------------------------
# Simulation
# ---------------------------------------------------------------------------

def _simulate(coin: str, candles: List[Candle], max_lev: int, *,
              equity: float, equity_fraction: float, lev_ceiling: int,
              cfg: Dict[str, Any], gate_cfg: Dict[str, Any],
              allow_pullback: bool,
              max_loss_pct: float = 2.5, protect_pct: float = 1.5,
              retrace_threshold: float = 0.30,
              warmup: int = 100) -> List[Trade]:
    trades: List[Trade] = []
    open_t: Optional[Trade] = None
    open_dsl: Optional[DSL] = None
    fee_pct = ROUND_TRIP_FEE_BPS / 10000.0

    for i in range(warmup, len(candles) - 1):
        window = candles[: i + 1]
        bar = candles[i]
        next_bar = candles[i + 1]

        if open_t and open_dsl:
            done, exit_px, reason = open_dsl.check_bar(i, bar)
            if done:
                gross_pct = ((exit_px - open_t.entry_px) / open_t.entry_px
                             if open_t.side == "long"
                             else (open_t.entry_px - exit_px) / open_t.entry_px)
                open_t.exit_bar = i
                open_t.exit_px = exit_px
                open_t.pnl_usd = open_t.notional * (gross_pct - fee_pct)
                open_t.exit_reason = reason
                trades.append(open_t)
                open_t = open_dsl = None
            else:
                continue

        score, hits = _evaluate(window, cfg)
        inds = _indicators(window)
        verdict = _heuristic_verdict(score, hits, inds.get("bullish"),
                                     inds.get("atr_pct"))
        if verdict is None:
            continue
        burst = any(h["name"] == "momentumBurst" and h["fired"] for h in hits)
        if not _ta_confirmed(inds.get("bullish"), inds.get("atr_pct"),
                             inds.get("adx14"), score) and not burst:
            continue

        side = "long" if verdict == "LONG" else "short"
        allowed, via_pb = _runner_gate_allows(
            side, score, hits, inds, gate_cfg, allow_pullback)
        if not allowed:
            continue

        lev = min(lev_ceiling, max_lev)
        notional = equity * equity_fraction * lev
        open_t = Trade(coin=coin, side=side, entry_bar=i + 1,
                       entry_px=next_bar.o, notional=notional,
                       via_pullback=via_pb)
        open_dsl = DSL(side=side, entry_px=next_bar.o, entry_bar=i + 1,
                       peak_px=next_bar.o, max_loss_pct=max_loss_pct,
                       protect_pct=protect_pct,
                       retrace_threshold=retrace_threshold)
    return trades


# ---------------------------------------------------------------------------
# Stats + report
# ---------------------------------------------------------------------------

def _stats(trades: List[Trade], equity: float, days: int,
           label: str) -> Dict[str, Any]:
    n = len(trades)
    if n == 0:
        return {"label": label, "n": 0}
    wins = [t for t in trades if t.pnl_usd > 0]
    losses = [t for t in trades if t.pnl_usd < 0]
    pnl = sum(t.pnl_usd for t in trades)
    avg_win = sum(t.pnl_usd for t in wins) / len(wins) if wins else 0.0
    avg_loss = sum(t.pnl_usd for t in losses) / len(losses) if losses else 0.0
    payoff = abs(avg_win / avg_loss) if avg_loss != 0 else float("inf")
    wr = len(wins) / n * 100
    pb_trades = [t for t in trades if t.via_pullback]
    pb_wins = [t for t in pb_trades if t.pnl_usd > 0]
    reasons: Dict[str, int] = {}
    for t in trades:
        reasons[t.exit_reason] = reasons.get(t.exit_reason, 0) + 1
    return {
        "label": label, "n": n, "wins": len(wins), "losses": len(losses),
        "win_rate": wr, "avg_win": avg_win, "avg_loss": avg_loss,
        "payoff": payoff, "expectancy": pnl / n, "pnl": pnl,
        "pnl_pct": pnl / equity * 100,
        "pb_n": len(pb_trades),
        "pb_wins": len(pb_wins),
        "pb_win_rate": (len(pb_wins) / len(pb_trades) * 100) if pb_trades else 0.0,
        "pb_pnl": sum(t.pnl_usd for t in pb_trades),
        "exit_reasons": reasons,
    }


def _print_report(base: Dict[str, Any], pb: Dict[str, Any],
                  days: int) -> None:
    print("\n" + "=" * 90)
    print(f"  PULLBACK-LONG BYPASS A/B BACKTEST  —  {days} days, 1h bars")
    print("=" * 90)
    cols = [base, pb]
    labels = [c["label"] for c in cols]
    w = 22

    def _row(metric: str, key: str, fmt: str = "{:.1f}", pct: bool = False,
             dollar: bool = False) -> None:
        vals = []
        for c in cols:
            if c["n"] == 0 or key not in c:
                vals.append("-")
                continue
            v = c[key]
            if pct:
                vals.append(f"{v:.1f}%")
            elif dollar:
                vals.append(f"${v:+.2f}")
            else:
                vals.append(fmt.format(v))
        print(f"  {metric:<28s} " + " ".join(f"{v:>{w}s}" for v in vals))

    print(f"\n  {'Metric':<28s} " + " ".join(f"{l:>{w}s}" for l in labels))
    print(f"  {'-'*28} " + " ".join(f"{'-'*w}" for _ in labels))
    _row("Total trades", "n", "{:d}")
    _row("Win rate", "win_rate", pct=True)
    wl_vals = []
    for c in cols:
        wl_vals.append(f"{c.get('wins',0)}/{c.get('losses',0)}" if c["n"] else "-")
    print(f"  {'Wins / Losses':<28s} " + " ".join(f"{v:>{w}s}" for v in wl_vals))
    _row("Avg win", "avg_win", dollar=True)
    _row("Avg loss", "avg_loss", dollar=True)
    _row("Payoff ratio", "payoff", "{:.2f}")
    _row("Expectancy/trade", "expectancy", dollar=True)
    _row("Total PnL", "pnl", dollar=True)
    _row("Return on equity", "pnl_pct", pct=True)

    # Pullback-only breakdown
    print(f"\n  --- Pullback-long bypass trades (only in PULLBACK column) ---")
    if pb.get("pb_n", 0) > 0:
        print(f"  Pullback-admitted trades   : {pb['pb_n']}")
        print(f"  Pullback win rate          : {pb['pb_win_rate']:.1f}%")
        print(f"  Pullback PnL contribution  : ${pb['pb_pnl']:+.2f}")
    else:
        print("  (no pullback trades fired)")

    print(f"\n  Exit reasons:")
    for c in cols:
        if c["n"] > 0:
            print(f"    {c['label']}: {c['exit_reasons']}")

    print("\n  Caveats:")
    print("  - AI verdict substituted with deterministic heuristic; real LLM not replayed.")
    print(f"  - Round-trip fee {ROUND_TRIP_FEE_BPS:.1f} bps; no funding, no slippage.")
    print("  - Pullback bypass requires: uptrend + slow-burn + score>=20 + RSI<70 + ext<2.0 ATR.")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--coins", type=int, default=20)
    ap.add_argument("--interval", default="1h",
                    choices=["5m", "15m", "1h", "4h", "1d"])
    ap.add_argument("--equity", type=float, default=100.0)
    ap.add_argument("--pullback-min-score", type=float, default=20.0)
    ap.add_argument("--pullback-max-rsi", type=float, default=70.0)
    ap.add_argument("--pullback-max-ext", type=float, default=2.0)
    args = ap.parse_args()

    live = read_agent_config()
    equity_fraction = float(live.get("equity_fraction_per_trade", 0.10))
    leverage_ceiling = int(cfg_get("leverage", config=live))
    live_dsl = live.get("dsl_exit", {}) or {}
    max_loss = float(cfg_get("dsl_exit.max_loss_pct", config=live_dsl))
    protect = float(cfg_get("dsl_exit.protect_pct", config=live_dsl))
    retrace = float(cfg_get("dsl_exit.retrace_threshold", config=live_dsl))

    bars_per_day = {"5m": 288, "15m": 96, "1h": 24, "4h": 6, "1d": 1}[args.interval]
    total_bars = args.days * bars_per_day + 100

    cfg = get_config()
    universe = get_universe()
    perps = [m for m in universe if m["type"] == "perp" and not m["coin"].startswith("@")]
    coins = sorted(perps, key=lambda m: m.get("dayNtlVlm", 0), reverse=True)[:args.coins]

    # Gate config mirrors live .agent-config.json runner_entry_gate
    gate_cfg = {
        "min_composite": 45.0,
        "allow_shorts": True,
        "rsi_overbought": 75.0,
        "max_extension_atr": 2.5,
        "pullback_min_composite": args.pullback_min_score,
        "pullback_max_rsi": args.pullback_max_rsi,
        "pullback_max_extension_atr": args.pullback_max_ext,
    }

    print("=== pullback-long bypass A/B backtest ===")
    print(f"period: {args.days}d  interval: {args.interval}  universe: top-{args.coins}")
    print(f"equity: ${args.equity:.0f}  fraction: {equity_fraction:.0%}  "
          f"lev ceiling: {leverage_ceiling}x")
    print(f"pullback bypass: score>={args.pullback_min_score}  "
          f"RSI<{args.pullback_max_rsi}  ext<{args.pullback_max_ext} ATR\n")

    baseline_all: List[Trade] = []
    pullback_all: List[Trade] = []

    for m in coins:
        coin = m["coin"]
        max_lev = int(m.get("maxLeverage", 5))
        try:
            candles = fetch_hl_candles(coin, args.interval, total_bars)
            if len(candles) < 110:
                print(f"  {coin:8} skip ({len(candles)} bars)")
                continue
            base = _simulate(
                coin, candles, max_lev, equity=args.equity,
                equity_fraction=equity_fraction, lev_ceiling=leverage_ceiling,
                cfg=cfg, gate_cfg=gate_cfg, allow_pullback=False,
                max_loss_pct=max_loss, protect_pct=protect,
                retrace_threshold=retrace)
            pull = _simulate(
                coin, candles, max_lev, equity=args.equity,
                equity_fraction=equity_fraction, lev_ceiling=leverage_ceiling,
                cfg=cfg, gate_cfg=gate_cfg, allow_pullback=True,
                max_loss_pct=max_loss, protect_pct=protect,
                retrace_threshold=retrace)
            bp = sum(t.pnl_usd for t in base)
            pp = sum(t.pnl_usd for t in pull)
            extra = len(pull) - len(base)
            print(f"  {coin:8} base {len(base):3}tr/${bp:+7.2f}  "
                  f"pullback {len(pull):3}tr/${pp:+7.2f}  (+{extra} trades)")
            baseline_all.extend(base)
            pullback_all.extend(pull)
        except Exception as e:
            print(f"  {coin:8} error: {e}")

    s_base = _stats(baseline_all, args.equity, args.days, "BASELINE")
    s_pb = _stats(pullback_all, args.equity, args.days, "PULLBACK")
    _print_report(s_base, s_pb, args.days)
    return 0


if __name__ == "__main__":
    sys.exit(main())
