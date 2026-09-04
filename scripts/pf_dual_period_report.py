#!/usr/bin/env python3
"""Dual-period Profit Factor baseline report (strategy-direction.md S1).

Offline, read-only backtest that replays the production *price-based
directional* entry signals on historical bars and scores each one as
"signal direction x forward N-bar return" per the S1 acceptance command:

    python3 scripts/pf_dual_period_report.py --windows 4h:365d,1h:180d --with-costs

Gate (docs/strategy-direction.md S3): a signal side is admitted only when
BOTH windows carry >= --min-samples samples AND the *net* PF (after taker
fees + measured/assumed slippage, round trip) clears --gate-pf (1.05
minimum-admission; 1.2 target). Gross PF is always reported alongside for
the "net only is valid, gross is the sanity reference" rule. PF is computed
long / short / all separately.

Only signals replayable from candles alone are evaluated. Whale/news/LLM
verdicts are not reproducible offline — their PF census is the S2 phase
(logged-verdict replay, e.g. strategy_grid_search.py).

Matching convention (no look-ahead): a signal is evaluated on bars [:i+1]
(bar i is the last CLOSED bar); entry fills at bar i+1 OPEN; exit at bar
i+hold_bars CLOSE. Costs are spot-symmetric (PF is leverage-invariant).

Never places orders; sets HERMES_BACKTEST=1 and skips the private key.
"""
from __future__ import annotations

import argparse
import os
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

os.environ.setdefault("HERMES_BACKTEST", "1")
_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO))
sys.path.insert(0, str(Path(__file__).resolve().parent))  # scripts/ (backtest_logged, _memory_io)

import backtest_logged as btlog

from hermes_trader.agents import market_regime
from hermes_trader.agents.config_store import cfg_get
from hermes_trader.indicators import triggers as tg
from hermes_trader.models.types import Candle

_INTERVAL_MS = {"5m": 300_000, "1h": 3_600_000, "4h": 14_400_000}
# Hold ~24h per default at every scale (documented S1 convention:
# "signal direction x forward N-bar return").
_DEFAULT_HOLD_BARS = {"4h": 6, "1h": 24, "5m": 288}
_WARMUP_BARS = 120
_MIN_SAMPLES_DEFAULT = 30
_GATE_PF_DEFAULT = 1.05
# Fallback adverse slippage per side (bps) when a coin has < 3 measured
# exit fills. Spec mandates measured slip when available; 2bps matches the
# grid_search/backtest stress assumption for the no-history case.
_DEFAULT_SLIPPAGE_BPS = 2.0


# ────────────────────────── core (pure, unit-tested) ──────────────────────────

@dataclass
class Trade:
    """One signal instance, scored forward over `hold_bars` bars."""
    source: str
    side: str           # "long" | "short"
    coin: str
    entry_ts: int       # ms, the closed signal bar open time
    gross_pct: float    # side-aware % move, entry open -> exit close
    net_pct: float      # gross minus round-trip cost (spot %)
    exit_ts: int = 0


def profit_factor(returns: List[float]) -> Optional[float]:
    """gross_win / |gross_loss|; wins are >0, losses are <= 0 (ties count as
    losses, matching shadow_book.get_stats). Returns None with no losses,
    0.0 with no wins and some losses."""
    wins = sum(r for r in returns if r > 0)
    losses = sum(r for r in returns if r <= 0)
    if losses == 0:
        return None
    return wins / abs(losses) if losses < 0 else 0.0


def round_trip_cost_pct(taker_bps: float, slippage_bps: float) -> float:
    """Round-trip cost in spot %, two fills (entry+exit)."""
    return (taker_bps + slippage_bps) * 2.0 / 100.0


def forward_trade(entry_px: float, exit_px: float, side: str,
                  cost_pct: float) -> Tuple[float, float]:
    """(gross_pct, net_pct) for a fill at `entry_px` exiting at `exit_px`."""
    sign = 1.0 if side == "long" else -1.0
    gross = sign * (exit_px - entry_px) / entry_px * 100.0
    return gross, gross - cost_pct


def parse_window(spec: str) -> Tuple[str, int]:
    """'4h:365d' -> ('4h', 365). Interval in {5m,1h,4h}; span in days."""
    try:
        interval, span = spec.split(":")
    except ValueError as e:
        raise ValueError(f"bad window spec {spec!r}; expected INTERVAL:SPANd") from e
    if interval not in _INTERVAL_MS:
        raise ValueError(f"unsupported interval {interval!r}; use 5m/1h/4h")
    span = span.strip()
    if not span.endswith("d"):
        raise ValueError(f"bad span {span!r}; expected days like 365d")
    days = int(span[:-1])
    if days <= 0:
        raise ValueError(f"bad span {span!r}; days must be positive")
    return interval, days


def bars_needed(interval: str, days: int, hold_bars: int,
                warmup: int = _WARMUP_BARS) -> int:
    """Bars to fetch: `days` of evaluation history plus warmup and the
    forward hold tail for the last signals."""
    per_day = 86_400_000 // _INTERVAL_MS[interval]
    return per_day * days + warmup + hold_bars + 1


@dataclass
class PFStats:
    n: int
    gross_pf: Optional[float]
    net_pf: Optional[float]
    gross_first_half: Optional[float]
    gross_second_half: Optional[float]
    net_first_half: Optional[float]
    net_second_half: Optional[float]


def _half_pfs(trades: List[Trade], attr: str) -> Tuple[Optional[float], Optional[float]]:
    """Split the (time-ordered) sample in half and return each half's PF."""
    mid = len(trades) // 2
    if mid == 0 or len(trades) - mid == 0:
        return None, None
    first = profit_factor([getattr(t, attr) for t in trades[:mid]])
    second = profit_factor([getattr(t, attr) for t in trades[mid:]])
    return first, second


def pf_stats(trades: List[Trade]) -> PFStats:
    """Aggregate one signal/side/window bucket. Splits the ordered sample in
    half for the roadmap-S4 robustness check (both halves net-PF >= 1.0)."""
    trades = sorted(trades, key=lambda t: t.entry_ts)
    n = len(trades)
    g1, g2 = _half_pfs(trades, "gross_pct")
    n1, n2 = _half_pfs(trades, "net_pct")
    return PFStats(
        n=n,
        gross_pf=profit_factor([t.gross_pct for t in trades]),
        net_pf=profit_factor([t.net_pct for t in trades]),
        gross_first_half=g1, gross_second_half=g2,
        net_first_half=n1, net_second_half=n2,
    )


def gate_verdict(stats_by_window: List[PFStats], min_samples: int,
                 gate_pf: float) -> str:
    """'PASS' only if every window has enough samples AND net PF clears the
    gate (None PF with no losses counts as clearing). 'INSUFFICIENT' if any
    window is short of samples; otherwise 'FAIL'."""
    if len(stats_by_window) != 2 or any(s.n < min_samples for s in stats_by_window):
        return "INSUFFICIENT"
    for s in stats_by_window:
        if s.net_pf is not None and s.net_pf < gate_pf:
            return "FAIL"
    return "PASS"


# ─────────────────── offline signal replay (candles only) ────────────────────

def _breakout_side(hit: Dict[str, Any]) -> Optional[str]:
    reason = str(hit.get("reason", ""))
    if "breakout above" in reason:
        return "long"
    if "breakout below" in reason:
        return "short"
    return None


@dataclass
class SignalSpec:
    source: str
    fn: Callable[[List[Candle]], Dict[str, Any]]
    side: Callable[[Dict[str, Any]], Optional[str]]  # hit -> long/short/None


def _regime_hit(window: List[Candle]) -> Dict[str, Any]:
    closes = [float(c.c) for c in window]
    trend = market_regime.trend_from_closes(closes)
    return {"fired": trend in ("up", "down"), "trend": trend}


SIGNAL_SPECS: List[SignalSpec] = [
    SignalSpec("regime_trend", _regime_hit,
               lambda h: "long" if h.get("trend") == "up"
               else ("short" if h.get("trend") == "down" else None)),
    SignalSpec("uptrend_momentum", tg.uptrend_momentum, lambda h: "long" if h.get("fired") else None),
    SignalSpec("downtrend_momentum", tg.downtrend_momentum, lambda h: "short" if h.get("fired") else None),
    SignalSpec("bullish_reversal", tg.bullish_reversal_candle, lambda h: "long" if h.get("fired") else None),
    SignalSpec("bearish_reversal", tg.bearish_reversal_candle, lambda h: "short" if h.get("fired") else None),
    SignalSpec("breakout", tg.breakout, lambda h: _breakout_side(h) if h.get("fired") else None),
    SignalSpec("trend_flip_1h", tg.trend_flip_1h, lambda h: "long" if h.get("fired") else None),
    SignalSpec("higher_lows_1h", tg.higher_lows_1h, lambda h: "long" if h.get("fired") else None),
    SignalSpec("momentum_continuation_1h", tg.momentum_continuation_1h,
               lambda h: "long" if h.get("fired") else None),
]


def replay_signals(candles: List[Candle], specs: List[SignalSpec],
                   hold_bars: int, cost_pct: float,
                   warmup: int = _WARMUP_BARS,
                   only_since_ts: Optional[int] = None,
                   coin: str = "") -> List[Trade]:
    """Replay every spec over `candles` and score each firing instance as a
    forward `hold_bars` trade. Signal at closed bar i enters at bar i+1
    open and exits at bar i+hold_bars close (no look-ahead)."""
    trades: List[Trade] = []
    for i in range(warmup, len(candles) - hold_bars - 1):
        if only_since_ts is not None and candles[i].t < only_since_ts:
            continue
        entry_bar, exit_bar = i + 1, i + hold_bars
        if exit_bar >= len(candles):
            continue
        entry_px, exit_px = float(candles[entry_bar].o), float(candles[exit_bar].c)
        if entry_px <= 0:
            continue
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
            gross, net = forward_trade(entry_px, exit_px, side, cost_pct)
            trades.append(Trade(source=spec.source, side=side, coin=coin,
                                entry_ts=candles[i].t, gross_pct=gross,
                                net_pct=net, exit_ts=candles[exit_bar].t))
    return trades


# ─────────────────────── measured slippage (best effort) ─────────────────────

def measured_slip_bps(mem: Dict[str, Any], coin: str, days: float = 30.0,
                      min_samples: int = 3) -> float:
    """Mean adverse exit slippage in bps over `days` for `coin`, computed
    straight from the memory closes ledger (the same rows memory.Memory
    derives avg_exit_slip_bps from). Returns 0.0 when fewer than
    `min_samples` qualifying fills exist (caller then applies the assumed
    fallback, not zero — measured slip is a refinement, never an
    underestimate)."""
    cutoff = time.time() - days * 86400.0
    vals: List[float] = []
    for c in mem.get("closes", []) or []:
        if not isinstance(c, dict) or c.get("coin") != coin:
            continue
        slip = c.get("exit_slip_bps")
        ts = c.get("closed_at") or 0
        try:
            slip_f = float(slip)
        except (TypeError, ValueError):
            continue
        if slip_f > 0 and float(ts) >= cutoff:
            vals.append(slip_f)
    if len(vals) < min_samples:
        return 0.0
    return sum(vals) / len(vals)


# ───────────────────────────────── I/O / CLI ─────────────────────────────────

def _pf_str(pf: Optional[float]) -> str:
    if pf is None:
        return "n/a(no losses)"
    return f"{pf:.3f}"


def run(args: argparse.Namespace) -> int:
    windows: List[Tuple[str, int]] = [parse_window(w) for w in args.windows.split(",")]
    hold_bars = {}
    if args.hold_bars:
        for hb in args.hold_bars.split(","):
            iv, n = hb.split(":")
            hold_bars[iv] = int(n)
    for iv, _ in windows:
        hold_bars.setdefault(iv, _DEFAULT_HOLD_BARS.get(iv, 6))

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

    print("# Dual-period Profit Factor baseline (strategy-direction.md S1)")
    print(f"# windows: {', '.join(f'{iv}/{days}d' for iv, days in windows)} | "
          f"hold bars: {', '.join(f'{iv}={hold_bars[iv]}' for iv, _ in windows)}")
    print(f"# coins: {', '.join(coins)} | universe proxy: offline replay of "
          f"price-based signals only (whale/news/LLM => S2)")
    print(f"# costs: taker {taker_bps:g}bps/side + slippage "
          f"{'measured (fallback ' + str(args.slippage_bps) + 'bps)' if args.use_measured_slippage else str(args.slippage_bps) + 'bps assumed'}/side"
          f" | --with-costs={bool(args.with_costs)} | gate PF {args.gate_pf:g}, "
          f"min samples {args.min_samples}/window")
    print()

    # window -> source -> side -> trades
    bucket: Dict[str, Dict[str, Dict[str, List[Trade]]]] = {
        f"{iv}:{days}d": {spec.source: {"long": [], "short": [], "all": []}
                          for spec in SIGNAL_SPECS}
        for iv, days in windows
    }

    now_ms = int(time.time() * 1000)
    for iv, days in windows:
        wkey = f"{iv}:{days}d"
        need = bars_needed(iv, days, hold_bars[iv])
        eval_since = now_ms - days * 86_400_000
        for coin in coins:
            slip_bps = (measured_slip_bps(mem, coin) if mem else 0.0) or float(args.slippage_bps)
            cost_pct = round_trip_cost_pct(taker_bps, slip_bps) if args.with_costs else 0.0
            candles = btlog.fetch_candles_at(coin, iv, need, now_ms)
            if not candles:
                print(f"# {coin} {iv}: no candles (fetch failed / cache miss)")
                continue
            trades = replay_signals(candles, SIGNAL_SPECS, hold_bars[iv],
                                    cost_pct, only_since_ts=eval_since, coin=coin)
            for t in trades:
                for side_key in (t.side, "all"):
                    bucket[wkey][t.source][side_key].append(t)
    btlog._save_disk_cache(args.cache_file)

    # ── report ──
    header = f"{'signal':<26} {'side':<5} | " + " | ".join(
        f"{wkey:>22}" for wkey in bucket) + " | verdict"
    print(header)
    print("-" * len(header))
    wkeys = list(bucket)
    for spec in SIGNAL_SPECS:
        for side in ("long", "short", "all"):
            cells: List[str] = []
            stats_list: List[PFStats] = []
            for wkey in wkeys:
                st = pf_stats(bucket[wkey][spec.source][side])
                stats_list.append(st)
                pf_net = st.net_pf if args.with_costs else st.gross_pf
                cells.append(
                    f"n={st.n:<5} PF={'gross ' + _pf_str(st.gross_pf) + ' / net ' + _pf_str(st.net_pf):<22}"
                    if args.with_costs else f"n={st.n:<5} PF {_pf_str(st.gross_pf):<22}")
            verdict = gate_verdict(stats_list, args.min_samples, args.gate_pf)
            print(f"{spec.source:<26} {side:<5} | " + " | ".join(f"{c:>22}" for c in cells)
                  + f" | {verdict}")
        print()

    # Roadmap §4 robustness: PASS rows must also show net PF >= 1.0 in both
    # non-overlapping half-window splits (computed on the longest window).
    longest = max(windows, key=lambda w: w[1])
    wkey = f"{longest[0]}:{longest[1]}d"
    print("# robustness (non-overlapping half-split net PF on "
          f"{wkey}, gate {args.gate_pf:g}, half-floor 1.0):")
    any_robust = False
    for spec in SIGNAL_SPECS:
        for side in ("long", "short", "all"):
            st = pf_stats(bucket[wkey][spec.source][side])
            if st.n < args.min_samples or st.net_first_half is None:
                continue
            halves_ok = (st.net_first_half >= 1.0 and st.net_second_half is not None
                         and st.net_second_half >= 1.0)
            mark = "OK " if halves_ok else "WEAK"
            any_robust = True
            print(f"#   [{mark}] {spec.source:<24} {side:<5} "
                  f"n={st.n:<5} first-half net PF {_pf_str(st.net_first_half)} / "
                  f"second-half {_pf_str(st.net_second_half)}")
    if not any_robust:
        print("#   (no signal/side reached the sample floor on this window)")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--windows", default="4h:365d,1h:180d",
                    help="comma list of INTERVAL:SPANd (interval 5m/1h/4h; span in days)")
    ap.add_argument("--coins", default="BTC,ETH",
                    help="comma list of coins to replay (default BTC,ETH)")
    ap.add_argument("--hold-bars", default="",
                    help="per-interval forward hold, e.g. '4h:6,1h:24' (default ~24h each)")
    ap.add_argument("--with-costs", action="store_true",
                    help="deduct taker fee + slippage round trip (net PF); always also reports gross")
    ap.add_argument("--taker-fee-bps", type=float, default=None,
                    help="per-side taker fee bps (default: execution.taker_fee_pct config, 2.5)")
    ap.add_argument("--slippage-bps", type=float, default=_DEFAULT_SLIPPAGE_BPS,
                    help="assumed adverse slippage bps/side when no measured fills exist")
    ap.add_argument("--use-measured-slippage", action="store_true",
                    help="use mean measured exit_slip_bps from .agent-memory.json "
                         "(30d, >=3 fills) per coin, falling back to --slippage-bps")
    ap.add_argument("--min-samples", type=int, default=_MIN_SAMPLES_DEFAULT,
                    help="minimum instances per window to evaluate a signal (default 30)")
    ap.add_argument("--gate-pf", type=float, default=_GATE_PF_DEFAULT,
                    help="minimum-admission net PF for BOTH windows (default 1.05)")
    ap.add_argument("--cache-file",
                    default=os.path.join(tempfile.gettempdir(), "hermes_pf_dual_period_candles.json"),
                    help="disk cache for historical candles; empty string disables")
    ap.add_argument("--api-sleep", type=float, default=0.0,
                    help="seconds to sleep before uncached Hyperliquid candle requests")
    args = ap.parse_args()
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
