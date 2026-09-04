#!/usr/bin/env python3
"""Parameter robustness report (advanced-optimization-roadmap.md §4).

Offline, read-only backtest that proves a tuned signal threshold is NOT a
point-luck overfit before it ships. For every candidate threshold it sweeps
a +/-20% one-dimensional neighbourhood grid and replays the production signal
on historical bars, then applies two checks:

  1. split-window: the evaluation window (default 4h:365d) is cut at its
     calendar midpoint into two non-overlapping half-year segments; the
     BASELINE (production default) grid point must carry net PF >= 1.0 in
     BOTH segments (None PF = no losses counts as clearing) and each segment
     must clear the minimum-sample floor.
  2. smoothness ("no spike"): the baseline point must not be an isolated
     peak (both immediate grid neighbours strictly below
     (1 - rel_tol) * baseline PF) and the max adjacent net-PF jump across
     the grid must stay within --max-pf-jump.

Complementary to the dual-period gate (strategy-direction.md §3): dual-period
guards single-period luck; this guards parameter-point luck.

Only candle-replayable directional triggers with a sensitivity *pct_threshold*
are swept (momentum_burst / uptrend_momentum / downtrend_momentum). An integer
*lookback* of 2 collapses a +/-20% grid onto a single integer (degenerate), so
lookback knobs are not swept. Matching convention is identical to
pf_dual_period_report (signal at closed bar i -> entry i+1 open, exit
i+hold_bars close, no look-ahead; spot-symmetric costs).

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
import pf_dual_period_report as pf

from hermes_trader.agents.config_store import cfg_get
from hermes_trader.indicators import triggers as tg

# Sweep knobs.
_DEFAULT_WINDOW = "4h:365d"
_GRID_STEPS_EACH_SIDE = 4
_GRID_SPREAD = 0.20          # +/-20% neighbourhood around the production default
_SPLIT_GATE_PF = 1.0         # §4: both half-year segments net PF >= 1.0
_SPIKE_REL_TOL = 0.25        # isolated-peak tolerance: neighbour < 75% of peak => spike
_MAX_PF_JUMP = 0.30          # max |delta net PF| between adjacent grid points
_DEFAULT_HALF_MIN_SAMPLES = 15


# ────────────────────────── core (pure, unit-tested) ──────────────────────────

def build_grid(baseline: float, spread: float, steps: int,
               is_int: bool = False) -> Tuple[List[float], int]:
    """Inclusive 1-D grid: `baseline` with `steps` equally spaced points on
    each side spanning +/-`spread` (fraction). Returns ascending unique
    values and the index of `baseline`. Integer knobs are rounded to the
    nearest int then de-duplicated (a spread that collapses to one point is
    reported as a single-point grid, baseline index 0)."""
    steps = max(1, int(steps))
    lo = baseline * (1.0 - spread)
    hi = baseline * (1.0 + spread)
    raw = [lo + (hi - lo) * k / (2 * steps) for k in range(2 * steps + 1)]
    if is_int:
        raw = [float(int(round(v))) for v in raw]
    vals: List[float] = []
    for v in raw:
        if not vals or v != vals[-1]:
            vals.append(v)
    if len(vals) == 1:
        return vals, 0
    base_idx = min(range(len(vals)), key=lambda i: abs(vals[i] - baseline))
    return vals, base_idx


def split_mid_ts(eval_since_ms: int, now_ms: int) -> int:
    """Calendar midpoint of the evaluation window — trades on/after it form
    the second non-overlapping half-year segment."""
    return eval_since_ms + (now_ms - eval_since_ms) // 2


def segment_pfs(trades: List[pf.Trade], mid_ts: int,
                use_net: bool) -> Tuple[Optional[float], Optional[float], int, int]:
    """(first-half PF, second-half PF, n first, n second) split at mid_ts."""
    attr = "net_pct" if use_net else "gross_pct"
    first = [t for t in trades if t.entry_ts < mid_ts]
    second = [t for t in trades if t.entry_ts >= mid_ts]
    p1 = pf.profit_factor([getattr(t, attr) for t in first])
    p2 = pf.profit_factor([getattr(t, attr) for t in second])
    return p1, p2, len(first), len(second)


def _clears(pf_val: Optional[float], gate: float) -> bool:
    """None PF (no losses) clears; a finite PF must reach the gate."""
    return pf_val is None or pf_val >= gate


def half_gate(p1: Optional[float], p2: Optional[float], n1: int, n2: int,
              min_samples: int, gate_pf: float) -> str:
    """PASS only if both non-overlapping segments have >= min_samples trades
    AND net PF clears gate. INSUFFICIENT if either segment is short samples;
    otherwise FAIL."""
    if n1 < min_samples or n2 < min_samples:
        return "INSUFFICIENT"
    if _clears(p1, gate_pf) and _clears(p2, gate_pf):
        return "PASS"
    return "FAIL"


def is_isolated_spike(values: List[Optional[float]], idx: int,
                      rel_tol: float) -> bool:
    """True if grid point `idx` is a strict isolated peak: BOTH immediate
    neighbours exist, are finite and lie below (1 - rel_tol) * peak. A None
    PF (no losses, effectively capped at +inf) can never be a spike; a None
    neighbour (no losses) can never undercut the peak, so it prevents the
    spike call there. Endpoints have no two neighbours -> never a spike."""
    if idx <= 0 or idx >= len(values) - 1:
        return False
    peak = values[idx]
    left, right = values[idx - 1], values[idx + 1]
    if peak is None or left is None or right is None:
        return False
    threshold = peak * (1.0 - rel_tol)
    return left < threshold and right < threshold


def max_adjacent_jump(values: List[Optional[float]]) -> float:
    """Largest |delta PF| between adjacent FINITE grid points. None points
    (no losses) break the adjacency comparison rather than injecting a jump.
    Returns 0.0 when there are fewer than two finite adjacent pairs."""
    worst = 0.0
    for a, b in zip(values, values[1:]):
        if a is None or b is None:
            continue
        worst = max(worst, abs(a - b))
    return worst


@dataclass
class GridPoint:
    value: float
    is_baseline: bool
    full_pf: Optional[float]
    n: int
    h1_pf: Optional[float]
    h2_pf: Optional[float]
    h1_n: int
    h2_n: int


def target_verdict(grid: List[GridPoint], baseline_idx: int,
                   min_samples: int, gate_pf: float,
                   rel_tol: float, max_jump: float) -> Tuple[str, List[str]]:
    """Aggregate §4 verdict for one scan target. Returns (verdict, reasons)
    where verdict is PASS / INSUFFICIENT / FAIL:
      - baseline segment gate (both half-years PF >= gate, samples floor),
      - no isolated spike at the baseline,
      - max adjacent net-PF jump within max_jump."""
    reasons: List[str] = []
    base = grid[baseline_idx]
    gate = half_gate(base.h1_pf, base.h2_pf, base.h1_n, base.h2_n,
                     min_samples, gate_pf)
    if gate == "INSUFFICIENT":
        reasons.append(f"baseline halves under sample floor "
                       f"(n={base.h1_n}/{base.h2_n} < {min_samples})")
        return "INSUFFICIENT", reasons
    if gate == "FAIL":
        reasons.append(f"baseline half-year net PF below {gate_pf:g} "
                       f"(H1 {pf._pf_str(base.h1_pf)} / H2 {pf._pf_str(base.h2_pf)})")

    pf_curve = [g.full_pf for g in grid]
    if is_isolated_spike(pf_curve, baseline_idx, rel_tol):
        reasons.append("baseline is an isolated PF spike "
                       f"(neighbours {pf._pf_str(pf_curve[baseline_idx - 1])} / "
                       f"{pf._pf_str(pf_curve[baseline_idx + 1])} << "
                       f"{pf._pf_str(pf_curve[baseline_idx])})")
    jump = max_adjacent_jump(pf_curve)
    if jump > max_jump:
        reasons.append(f"neighbourhood not smooth: max adjacent net-PF jump "
                       f"{jump:.2f} > {max_jump:g}")
    return ("PASS" if not reasons else "FAIL"), reasons


# ───────────────────────────── scan targets ──────────────────────────────────

@dataclass
class ScanTarget:
    key: str
    source: str
    baseline: float
    builder: Callable[[float], Callable[[List[Any]], Dict[str, Any]]]
    side_fn: Callable[[Dict[str, Any]], Optional[str]]


def _mb_side(hit: Dict[str, Any]) -> Optional[str]:
    return pf._momentum_burst_side(hit) if hit.get("fired") else None


SCAN_TARGETS: List[ScanTarget] = [
    ScanTarget(
        "momentum_burst.pct_threshold", "momentum_burst", 4.0,
        lambda v: (lambda candles: tg.momentum_burst(candles, pct_threshold=v)),
        _mb_side,
    ),
    ScanTarget(
        "uptrend_momentum.pct_threshold", "uptrend_momentum", 5.0,
        lambda v: (lambda candles: tg.uptrend_momentum(candles, pct_threshold=v)),
        lambda h: "long" if h.get("fired") else None,
    ),
    ScanTarget(
        "downtrend_momentum.pct_threshold", "downtrend_momentum", 5.0,
        lambda v: (lambda candles: tg.downtrend_momentum(candles, pct_threshold=v)),
        lambda h: "short" if h.get("fired") else None,
    ),
]


# ───────────────────────────── I/O / report ──────────────────────────────────

def _sweep_target(target: ScanTarget, grid_values: List[float],
                  baseline_idx: int,
                  candles_by_coin: Dict[str, List[Any]],
                  cost_by_coin: Dict[str, float], hold_bars: int,
                  mid_ts: int, use_net: bool,
                  warmup: int = pf._WARMUP_BARS) -> List[GridPoint]:
    points: List[GridPoint] = []
    for gi, val in enumerate(grid_values):
        spec = pf.SignalSpec(target.source, target.builder(val), target.side_fn)
        trades: List[pf.Trade] = []
        for coin, candles in candles_by_coin.items():
            trades.extend(pf.replay_signals(candles, [spec], hold_bars,
                                            cost_by_coin.get(coin, 0.0),
                                            warmup=warmup, coin=coin))
        trades.sort(key=lambda t: t.entry_ts)
        h1, h2, n1, n2 = segment_pfs(trades, mid_ts, use_net)
        full = pf.profit_factor([(t.net_pct if use_net else t.gross_pct)
                                 for t in trades])
        points.append(GridPoint(value=val, is_baseline=(gi == baseline_idx),
                                full_pf=full, n=len(trades),
                                h1_pf=h1, h2_pf=h2, h1_n=n1, h2_n=n2))
    return points


def run(args: argparse.Namespace) -> int:
    interval, days = pf.parse_window(args.window)
    hold_bars = args.hold_bars if args.hold_bars else pf._DEFAULT_HOLD_BARS.get(interval, 6)
    taker_bps = args.taker_fee_bps
    if taker_bps is None:
        taker_bps = float(cfg_get("execution.taker_fee_pct", default=0.025)) * 100.0
    coins = [c.strip().upper() for c in args.coins.split(",") if c.strip()]

    mem: Dict[str, Any] = {}
    try:
        from _memory_io import load_memory
        mem = load_memory(_REPO / args.memory_file)
    except Exception:
        mem = {}

    btlog._API_SLEEP_S = max(0.0, float(args.api_sleep or 0.0))
    btlog._load_disk_cache(args.cache_file)

    use_net = bool(args.with_costs)
    now_ms = int(time.time() * 1000)
    eval_since = now_ms - days * 86_400_000
    mid_ts = split_mid_ts(eval_since, now_ms)
    need = pf.bars_needed(interval, days, hold_bars)

    print("# Parameter robustness report (roadmap §4)")
    print(f"# window: {interval}/{days}d (hold {hold_bars} bars) | coins: {', '.join(coins)}")
    print(f"# grid: +/-{args.spread * 100:g}% x {args.steps} steps/side | "
          f"split gate net PF >= {args.split_gate_pf:g} on two non-overlapping "
          f"half-year segments | smooth: spike rel-tol {args.spike_rel_tol:g}, "
          f"max adjacent jump {args.max_pf_jump:g}")
    print(f"# costs: taker {taker_bps:g}bps/side + slippage "
          f"{'measured (fallback ' + str(args.slippage_bps) + 'bps)' if mem else str(args.slippage_bps) + 'bps assumed'}"
          f" | --with-costs={use_net} | half min-samples {args.half_min_samples}")
    print()

    candles_by_coin: Dict[str, List[Any]] = {}
    cost_by_coin: Dict[str, float] = {}
    for coin in coins:
        slip_bps = (pf.measured_slip_bps(mem, coin) if mem else 0.0) or float(args.slippage_bps)
        cost_by_coin[coin] = pf.round_trip_cost_pct(taker_bps, slip_bps) if use_net else 0.0
        candles = btlog.fetch_candles_at(coin, interval, need, now_ms)
        if not candles:
            print(f"# {coin} {interval}: no candles (fetch failed / cache miss)")
            continue
        candles_by_coin[coin] = candles
    btlog._save_disk_cache(args.cache_file)

    fragile: List[str] = []
    for target in SCAN_TARGETS:
        grid_values, base_idx = build_grid(target.baseline, args.spread, args.steps)
        if len(grid_values) <= 1:
            print(f"## {target.key}: degenerate grid (spread collapses to one point) — skipped")
            print()
            continue
        points = _sweep_target(target, grid_values, base_idx, candles_by_coin,
                               cost_by_coin, hold_bars, mid_ts, use_net)
        verdict, reasons = target_verdict(
            points, base_idx, args.half_min_samples, args.split_gate_pf,
            args.spike_rel_tol, args.max_pf_jump)

        hdr = (f"{'param':>7} | {'n':>5} | {'fullPF':>9} | "
               f"{'H1 n/PF':>13} | {'H2 n/PF':>13} |")
        print(f"## {target.key}  (baseline {target.baseline:g})  -> {verdict}")
        print(hdr)
        print("-" * len(hdr))
        for g in points:
            mark = "*" if g.is_baseline else " "
            print(f"{g.value:>6.2f}{mark}| {g.n:>5} | {pf._pf_str(g.full_pf):>9} | "
                  f"{g.h1_n:>5} {pf._pf_str(g.h1_pf):>7} | "
                  f"{g.h2_n:>5} {pf._pf_str(g.h2_pf):>7} |")
        if reasons:
            for r in reasons:
                print(f"   ! {r}")
        else:
            print("   ok: baseline clears both half-year segments and the "
                  "neighbourhood is smooth (no isolated spike / jump).")
        if verdict != "PASS":
            fragile.append(f"{target.key}: {verdict}")
        print()

    print("# summary")
    if fragile:
        print(f"#   FRAGILE / NOT-ROBUST ({len(fragile)}):")
        for f in fragile:
            print(f"#     - {f}")
    else:
        print("#   all swept targets PASSED §4 robustness "
              "(split-window net PF >= gate + smooth neighbourhood).")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--window", default=_DEFAULT_WINDOW,
                    help="INTERVAL:SPANd to evaluate (default 4h:365d; split into two halves)")
    ap.add_argument("--coins", default="BTC,ETH",
                    help="comma list of coins to replay (default BTC,ETH)")
    ap.add_argument("--hold-bars", type=int, default=0,
                    help="forward hold in bars (default ~24h for the interval)")
    ap.add_argument("--with-costs", action="store_true",
                    help="deduct taker fee + slippage round trip (net PF); §4 is a net-PF gate")
    ap.add_argument("--taker-fee-bps", type=float, default=None,
                    help="per-side taker fee bps (default: execution.taker_fee_pct config, 2.5)")
    ap.add_argument("--slippage-bps", type=float, default=pf._DEFAULT_SLIPPAGE_BPS,
                    help="assumed adverse slippage bps/side when no measured fills exist")
    ap.add_argument("--memory-file", default=".agent-memory.json",
                    help="memory JSON for measured exit slippage (best effort)")
    ap.add_argument("--spread", type=float, default=_GRID_SPREAD,
                    help="neighbourhood spread as a fraction of baseline (default 0.20 = +/-20%%)")
    ap.add_argument("--steps", type=int, default=_GRID_STEPS_EACH_SIDE,
                    help="grid points on each side of baseline (default 4)")
    ap.add_argument("--split-gate-pf", type=float, default=_SPLIT_GATE_PF,
                    help="net PF each half-year segment must clear (default 1.0)")
    ap.add_argument("--spike-rel-tol", type=float, default=_SPIKE_REL_TOL,
                    help="isolated-peak relative tolerance (default 0.25)")
    ap.add_argument("--max-pf-jump", type=float, default=_MAX_PF_JUMP,
                    help="max allowed |delta net PF| between adjacent grid points (default 0.30)")
    ap.add_argument("--half-min-samples", type=int, default=_DEFAULT_HALF_MIN_SAMPLES,
                    help="minimum trades per half-year segment at baseline (default 15)")
    ap.add_argument("--cache-file",
                    default=os.path.join(tempfile.gettempdir(), "hermes_pf_dual_period_candles.json"),
                    help="disk candle cache (defaults to the S1 dual-period cache for reuse)")
    ap.add_argument("--api-sleep", type=float, default=0.0,
                    help="seconds to sleep before uncached Hyperliquid candle requests")
    args = ap.parse_args()
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
