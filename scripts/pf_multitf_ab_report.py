#!/usr/bin/env python3
"""Multi-timeframe direction-agreement candidate A/B backtest (S3).

Offline, read-only backtest for the strategy-direction.md S3 entry-rework
candidates. The production price-based directional signals (the same S1
SIGNAL_SPECS) are replayed on base bars, then each firing signal is scored
under three variants:

  * baseline      - current production behaviour: every price signal is
                    admitted, no higher-timeframe (HTF) filter.
  * A_agree       - admit only when the signal side agrees with the HTF
                    trend (ema8 > ema21 for longs, ema8 < ema21 for shorts).
  * B_agree_str   - A plus HTF trend strength (adx14 >= 20) and a non-extreme
                    HTF RSI (long only when rsi < 70, short only when
                    rsi > 30) so we do not chase tops / sell bottoms.

HTF bars are bucket-resampled from the base series (1h -> 4h, x4;
4h -> 1d, x6), aligned to the epoch so buckets coincide with exchange
boundaries. A bucket is only visible to a signal once it is fully closed
(`bucket_start + htf_interval <= base_bar_close`), giving strict no
look-ahead. Indicators use the production hermes_trader.indicators.math
primitives, matching research._compute_indicators (ema8/ema21, rsi14, adx14).

Entry/exit/cost/PF/half-window machinery is reused verbatim from
pf_dual_period_report (S1) so A/B shares the baseline's exact accounting;
this script only adds the HTF filter and an A-vs-baseline comparison
(net PF must improve and max drawdown must not worsen).

Acceptance (docs/strategy-direction.md S5): both windows net PF >= 1.05
(1.2 target), >= --min-samples per window, net PF reported with gross PF,
both non-overlapping half windows net PF >= 1.0, and the candidate must
beat baseline on net PF without worsening max drawdown.

Never places orders; sets HERMES_BACKTEST=1 and skips the private key.
"""
from __future__ import annotations

import argparse
import math
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

os.environ.setdefault("HERMES_BACKTEST", "1")
_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO))
sys.path.insert(0, str(Path(__file__).resolve().parent))  # scripts/ (backtest_logged, _memory_io)

import backtest_logged as btlog
import pf_dual_period_report as pf

from hermes_trader.agents.config_store import cfg_get
from hermes_trader.indicators import math as ind
from hermes_trader.models.types import Candle

# Base interval -> HTF aggregation factor (1h->4h is x4; 4h->1d is x6).
_HTF_FACTOR = {"1h": 4, "4h": 6}
# Minimum closed HTF bars before ema8/ema21 direction is trustworthy
# (research._compute_indicators needs >= 21 bars).
_HTF_MIN_BARS = 21
_ADX_MIN = 20.0          # B: HTF trend must be "strong enough"
_RSI_LONG_MAX = 70.0     # B: no longs into HTF overbought
_RSI_SHORT_MIN = 30.0    # B: no shorts into HTF oversold


# ────────────────────────── core (pure, unit-tested) ─────────────────────────

@dataclass
class MTTrade:
    """One signal instance under one variant, scored forward hold_bars."""
    source: str
    side: str            # "long" | "short"
    coin: str
    variant: str         # "baseline" | "A_agree" | "B_agree_str"
    entry_ts: int
    gross_pct: float
    net_pct: float
    exit_ts: int = 0


def resample_htf(candles: List[Candle], factor: int,
                 base_interval_ms: int) -> List[Candle]:
    """Aggregate base candles into HTF buckets of `factor` bars. Buckets are
    aligned to the epoch (t // bucket_ms) so they coincide with exchange
    HTF boundaries (1h->4h, 4h->1d) regardless of the fetch start. Each
    output candle spans [bucket_start, bucket_start + bucket_ms)."""
    if factor <= 1:
        return list(candles)
    bucket_ms = factor * base_interval_ms
    groups: Dict[int, List[Candle]] = {}
    order: List[int] = []
    for c in candles:
        b = int(c.t // bucket_ms)
        if b not in groups:
            groups[b] = []
            order.append(b)
        groups[b].append(c)
    out: List[Candle] = []
    for b in order:
        g = groups[b]
        out.append(Candle(
            t=b * bucket_ms,
            o=float(g[0].o),
            h=max(float(x.h) for x in g),
            l=min(float(x.l) for x in g),
            c=float(g[-1].c),
            v=sum(float(getattr(x, "v", 0.0) or 0.0) for x in g),
        ))
    return out


def closed_htf_window(htf_candles: List[Candle], factor: int,
                      base_interval_ms: int, base_bar_close_ms: int) -> List[Candle]:
    """HTF buckets fully closed at the base bar's close. A bucket starting at
    `t` closes at `t + factor*base_interval`; it is usable only once the base
    bar has reached that instant (strict no look-ahead)."""
    bucket_ms = factor * base_interval_ms
    return [c for c in htf_candles if c.t + bucket_ms <= base_bar_close_ms]


def htf_features(closed: List[Candle]) -> Optional[Dict[str, float]]:
    """HTF trend/strength state from closed buckets, using the production
    indicator primitives (same ema/rsi/adx as research._compute_indicators).
    Returns None when there are too few bars or the EMAs are not finite."""
    if len(closed) < _HTF_MIN_BARS:
        return None
    closes = [float(c.c) for c in closed]
    e8 = ind.ema(closes, 8)[-1]
    e21 = ind.ema(closes, 21)[-1]
    if not (math.isfinite(e8) and math.isfinite(e21)):
        return None
    adx_last: Optional[float] = None
    for v in reversed(ind.adx(closed, 14)):
        if math.isfinite(v):
            adx_last = v
            break
    rsi_last = ind.rsi_last(closes, 14)
    return {
        "bullish": 1.0 if e8 > e21 else 0.0,
        "adx": adx_last if adx_last is not None else float("nan"),
        "rsi": rsi_last if rsi_last is not None else float("nan"),
    }


def admit_agreement(side: str, feat: Optional[Dict[str, float]]) -> bool:
    """Variant A: signal side must agree with the HTF ema8/ema21 trend."""
    if feat is None:
        return False
    bullish = feat["bullish"] > 0.5
    return bullish if side == "long" else (not bullish)


def admit_strength_non_extreme(side: str, feat: Optional[Dict[str, float]]) -> bool:
    """Variant B: A plus HTF adx >= 20 and a non-extreme HTF RSI."""
    if not admit_agreement(side, feat):
        return False
    adx_v = feat["adx"] if feat is not None else float("nan")
    rsi_v = feat["rsi"] if feat is not None else float("nan")
    if not (math.isfinite(adx_v) and adx_v >= _ADX_MIN):
        return False
    if not math.isfinite(rsi_v):
        return False
    if side == "long" and rsi_v >= _RSI_LONG_MAX:
        return False
    if side == "short" and rsi_v <= _RSI_SHORT_MIN:
        return False
    return True


_VARIANTS: List[Tuple[str, Callable[[str, Optional[Dict[str, float]]], bool]]] = [
    ("baseline", lambda side, feat: True),
    ("A_agree", admit_agreement),
    ("B_agree_str", admit_strength_non_extreme),
]


def replay_multitf(candles: List[Candle], specs: List[pf.SignalSpec],
                   factor: int, base_interval_ms: int, hold_bars: int,
                   cost_pct: float, warmup: int,
                   only_since_ts: Optional[int] = None,
                   coin: str = "") -> List[MTTrade]:
    """Replay S1 specs on base bars; for every firing signal emit one trade
    per variant that admits it. HTF state is computed once per bar from the
    buckets closed at that bar (no look-ahead). Entry at bar i+1 open, exit
    at bar i+hold_bars close (identical fills across variants)."""
    htf_candles = resample_htf(candles, factor, base_interval_ms)
    trades: List[MTTrade] = []
    for i in range(warmup, len(candles) - hold_bars - 1):
        if only_since_ts is not None and candles[i].t < only_since_ts:
            continue
        entry_bar, exit_bar = i + 1, i + hold_bars
        if exit_bar >= len(candles):
            continue
        entry_px, exit_px = float(candles[entry_bar].o), float(candles[exit_bar].c)
        if entry_px <= 0:
            continue
        base_close = candles[i].t + base_interval_ms
        feat = htf_features(closed_htf_window(htf_candles, factor,
                                              base_interval_ms, base_close))
        window = candles[: i + 1]
        for spec in specs:
            try:
                hit = spec.fn(window)
            except Exception:
                hit = {"fired": False}
            if not isinstance(hit, dict) or not hit.get("fired"):
                continue
            side = spec.side(hit)
            if side not in ("long", "short"):
                continue
            gross, net = pf.forward_trade(entry_px, exit_px, side, cost_pct)
            for name, pred in _VARIANTS:
                if not pred(side, feat):
                    continue
                trades.append(MTTrade(
                    source=spec.source, side=side, coin=coin, variant=name,
                    entry_ts=candles[i].t, gross_pct=gross, net_pct=net,
                    exit_ts=candles[exit_bar].t))
    return trades


def equity_drawdown(trades: List["MTTrade"]) -> float:
    """Max drawdown (spot %) over trades ordered by entry time, using net
    returns summed at equal notional."""
    ordered = sorted(trades, key=lambda t: t.entry_ts)
    cum = peak = mdd = 0.0
    for t in ordered:
        cum += t.net_pct
        peak = max(peak, cum)
        mdd = max(mdd, peak - cum)
    return mdd


# ───────────────────────────────── I/O / CLI ─────────────────────────────────

def _pf_str(x: Optional[float]) -> str:
    if x is None:
        return "n/a"
    return f"{x:.3f}"


def _variant_verdict(window_stats: Dict[str, List[pf.PFStats]],
                     window_mdd: Dict[str, List[float]],
                     min_samples: int, gate_pf: float) -> Dict[str, str]:
    """Per-variant verdict. baseline uses the S1 gate; A/B must additionally
    beat baseline net PF on every window and not worsen drawdown, with both
    half windows net PF >= 1.0."""
    out: Dict[str, str] = {}
    base = window_stats.get("baseline", [])
    for name, _ in _VARIANTS:
        stats = window_stats.get(name, [])
        verdict = pf.gate_verdict(stats, min_samples, gate_pf)
        if name == "baseline" or verdict != "PASS":
            out[name] = verdict
            continue
        better = all(
            s.net_pf is not None and b.net_pf is not None and s.net_pf > b.net_pf
            for s, b in zip(stats, base))
        half_ok = all(
            (s.net_first_half is None or s.net_first_half >= 1.0) and
            (s.net_second_half is None or s.net_second_half >= 1.0)
            for s in stats)
        mdd_ok = all(m <= bm for m, bm in zip(window_mdd[name], window_mdd["baseline"]))
        out[name] = "PASS" if (better and half_ok and mdd_ok) else "FAIL(vs base)"
    return out


def run(args: argparse.Namespace) -> int:
    windows = [pf.parse_window(w) for w in args.windows.split(",")]
    hold_bars = {}
    if args.hold_bars:
        for hb in args.hold_bars.split(","):
            iv, n = hb.split(":")
            hold_bars[iv] = int(n)

    taker_bps = args.taker_fee_bps
    if taker_bps is None:
        taker_bps = float(cfg_get("execution.taker_fee_pct", default=0.025)) * 100.0
    coins = [c.strip().upper() for c in args.coins.split(",") if c.strip()]
    if args.use_measured_slippage:
        try:
            from _memory_io import load_memory
            mem = load_memory(_REPO / ".agent-memory.json")
        except Exception:
            mem = {}
    else:
        mem = {}

    btlog._API_SLEEP_S = max(0.0, float(args.api_sleep or 0.0))
    btlog._load_disk_cache(args.cache_file)

    print("# Multi-timeframe direction-agreement A/B (strategy-direction.md S3)")
    print(f"# windows: {', '.join(f'{iv}/{days}d' for iv, days in windows)}")
    print("# variants: baseline=no HTF filter | A_agree=HTF ema8/ema21 agrees "
          "| B_agree_str=A + adx>=20 + RSI non-extreme (long<70, short>30)")
    print(f"# coins: {', '.join(coins)} | costs: taker {taker_bps:g}bps + slip "
          f"{'measured' if args.use_measured_slippage else str(args.slippage_bps) + 'bps assumed'}"
          f" | --with-costs={bool(args.with_costs)} | gate PF {args.gate_pf:g}, "
          f"min samples {args.min_samples}/window")
    print()

    # wkey -> variant -> side -> trades
    bucket: Dict[str, Dict[str, Dict[str, List[MTTrade]]]] = {}
    now_ms = int(time.time() * 1000)
    for iv, days in windows:
        factor = _HTF_FACTOR[iv]
        base_ms = pf._INTERVAL_MS[iv]
        hold = hold_bars.get(iv, pf._DEFAULT_HOLD_BARS.get(iv, 6))
        warmup = max(pf._WARMUP_BARS, factor * _HTF_MIN_BARS + 1)
        need = pf.bars_needed(iv, days, hold, warmup=warmup)
        eval_since = now_ms - days * 86_400_000
        wkey = f"{iv}:{days}d"
        bucket[wkey] = {name: {"long": [], "short": [], "all": []}
                        for name, _ in _VARIANTS}
        for coin in coins:
            slip_bps = (pf.measured_slip_bps(mem, coin) if mem else 0.0) or float(args.slippage_bps)
            cost_pct = pf.round_trip_cost_pct(taker_bps, slip_bps) if args.with_costs else 0.0
            candles = btlog.fetch_candles_at(coin, iv, need, now_ms)
            if not candles:
                print(f"# {coin} {iv}: no candles (fetch failed / cache miss)")
                continue
            trades = replay_multitf(candles, pf.SIGNAL_SPECS, factor, base_ms,
                                    hold, cost_pct, warmup,
                                    only_since_ts=eval_since, coin=coin)
            for t in trades:
                for side_key in (t.side, "all"):
                    bucket[wkey][t.variant][side_key].append(t)
    btlog._save_disk_cache(args.cache_file)

    wkeys = list(bucket)
    # ── ensemble ("all" sides pooled) comparison, the A/B verdict input ──
    window_stats: Dict[str, List[pf.PFStats]] = {}
    window_mdd: Dict[str, List[float]] = {}
    for name, _ in _VARIANTS:
        window_stats[name] = [pf.pf_stats(bucket[w][name]["all"]) for w in wkeys]
        window_mdd[name] = [equity_drawdown(bucket[w][name]["all"]) for w in wkeys]
    verdicts = _variant_verdict(window_stats, window_mdd,
                                args.min_samples, args.gate_pf)

    header = (f"{'variant':<14} {'side':<5} | "
              + " | ".join(f"{w:>34}" for w in wkeys) + " | verdict")
    print(header)
    print("-" * len(header))
    for name, _ in _VARIANTS:
        for side in ("all", "long", "short"):
            cells = []
            for w in wkeys:
                st = pf.pf_stats(bucket[w][name][side])
                mdd = equity_drawdown(bucket[w][name][side])
                cells.append(
                    f"n={st.n:<5} net={_pf_str(st.net_pf):<6} gross={_pf_str(st.gross_pf):<6} "
                    f"h1/h2={_pf_str(st.net_first_half)}/{_pf_str(st.net_second_half):<6} "
                    f"mdd={mdd:6.2f}%")
            v = verdicts[name] if side == "all" else ""
            print(f"{name:<14} {side:<5} | " + " | ".join(f"{c:>34}" for c in cells) + f" | {v}")
        print("-" * len(header))

    print("\n# verdict legend: PASS=both windows net PF >= gate, samples OK, "
          "halves >= 1.0; FAIL(vs base)=gate met but PF not above baseline or "
          "drawdown worse; INSUFFICIENT=< min samples; FAIL=net PF below gate.")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--windows", default="4h:365d,1h:180d")
    p.add_argument("--coins", default="BTC,ETH,SOL")
    p.add_argument("--hold-bars", default="")
    p.add_argument("--with-costs", action="store_true")
    p.add_argument("--taker-fee-bps", type=float, default=None)
    p.add_argument("--slippage-bps", type=float, default=pf._DEFAULT_SLIPPAGE_BPS)
    p.add_argument("--use-measured-slippage", action="store_true")
    p.add_argument("--min-samples", type=int, default=pf._MIN_SAMPLES_DEFAULT)
    p.add_argument("--gate-pf", type=float, default=pf._GATE_PF_DEFAULT)
    p.add_argument("--cache-file", default=str(_REPO / ".backtest_cache.json"))
    p.add_argument("--api-sleep", type=float, default=0.0)
    return run(p.parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
