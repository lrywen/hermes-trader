#!/usr/bin/env python3
"""S2 signal-source Profit Factor census (strategy-direction.md §4 roadmap).

Produces the "signal quality leaderboard" and the actionable list of signal
sources whose net PF sits under the admission gate (1.05). Two populations are
scored:

1. *Offline replay* — every production price-based directional entry signal
   that is reproducible from candles alone (the pf_dual_period_report.SIGNAL_SPECS
   set, incl. momentum_burst). Same no-look-ahead fill convention and the same
   dual-window / net-PF gate as the S1 report.

2. *Logged-verdict replay* — the only producer of LONG/SHORT in production is
   the LLM analysis (whale/news/composite are *context*, they never emit their
   own verdict). Each persisted LONG/SHORT analysis is replayed on forward 5m
   candles (next-bar-open entry, fixed-hold close, same cost model) and the
   resulting trades are bucketed by the context that surrounded the call:
   debate vs single-fallback vs ai_down, whale accumulation present, news_risk
   positive/negative, composite-score band, confidence band, and each fired
   trigger (momentum burst / breakout / volume spike / uptrend / downtrend /
   daily mover / slow-burn). This answers "which *kind* of LLM call is worth
   sizing". With no persisted directional history the buckets honestly report
   INSUFFICIENT and start scoring automatically once analyses accumulate.

dailyMover is not replayed offline: it is a long-bias *context* trigger gated
on market-provided 24h notional (not a candles-only function), so it is
measured through the `trig_daily_mover` logged bucket rather than approximated
from candles.

Read-only; sets HERMES_BACKTEST=1. Never places orders.
"""
from __future__ import annotations

import argparse
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

os.environ.setdefault("HERMES_BACKTEST", "1")
_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO))
sys.path.insert(0, str(Path(__file__).resolve().parent))  # scripts/

import backtest_logged as btlog
import pf_dual_period_report as pf

from hermes_trader.agents.config_store import cfg_get
from hermes_trader.models.types import Candle

# Logged-verdict horizon: production LLM calls are timed against forward 5m
# candles (cf. strategy_grid_search.py); hold ~24h to match the offline
# leaderboard's forward-return convention.
_LOGGED_INTERVAL = "5m"
_DEFAULT_LOGGED_HOLD_BARS = 288
_DEFAULT_LOGGED_DAYS = 30


# ───────────────────── logged-verdict replay (pure core) ─────────────────────

def analysis_side(a: Dict[str, Any]) -> Optional[str]:
    """Map a persisted analysis verdict to long/short (None for PASS/CLOSE)."""
    v = str(a.get("verdict") or "")
    if v == "LONG":
        return "long"
    if v == "SHORT":
        return "short"
    return None


def iter_directional_analyses(mem: Dict[str, Any],
                              cutoff_ms: int) -> List[Dict[str, Any]]:
    """Return the time-ordered LONG/SHORT analyses at/after `cutoff_ms` that
    carry a coin (the only ones replayable on forward candles)."""
    out: List[Dict[str, Any]] = []
    for a in sorted((x for x in (mem.get("analyses") or []) if isinstance(x, dict)),
                    key=lambda x: x.get("created_at", 0)):
        if analysis_side(a) is None or not str(a.get("coin") or ""):
            continue
        if int(a.get("created_at", 0) or 0) < cutoff_ms:
            continue
        out.append(a)
    return out


def logged_tags(a: Dict[str, Any]) -> List[str]:
    """Context bucket labels for one LONG/SHORT analysis. Every analysis gets
    the base `llm_verdict` tag plus the provenance/context tags it satisfies."""
    tags = ["llm_verdict"]
    # Provenance: a real debate consensus, a single-model fallback, or a
    # failure-PASS (ai_down). ai_down LONG/SHORT should be rare; if seen it is
    # scored separately so a degraded call never pollutes the debate bucket.
    if a.get("ai_down"):
        tags.append("ai_down")
    elif a.get("debate_used"):
        tags.append("llm_debate")
    else:
        tags.append("llm_single")
    # Whale: oi+funding accumulation context is present on the perception.
    if a.get("whale_signal"):
        tags.append("whale_present")
    # News: only the AI's positive/negative read is persisted (none => no tag).
    news_risk = str(a.get("news_risk") or "none")
    if news_risk == "negative":
        tags.append("news_negative")
    elif news_risk == "positive":
        tags.append("news_positive")
    # Composite band (0-100) around the production gate neighbourhood.
    comp = float(a.get("composite_score") or 0.0)
    if comp < 50.0:
        tags.append("composite_lt50")
    elif comp < 70.0:
        tags.append("composite_50_70")
    else:
        tags.append("composite_ge70")
    # Confidence band around the min_ai_confidence default (0.70).
    conf = float(a.get("confidence") or 0.0)
    tags.append("conf_ge70" if conf >= 0.70 else "conf_lt70")
    # Fired-trigger context — measures which TA setups preceded good calls.
    if a.get("momentum_burst_fired"):
        tags.append("trig_momentum_burst")
    if a.get("breakout_fired"):
        tags.append("trig_breakout")
    if a.get("volume_spike_fired"):
        tags.append("trig_volume_spike")
    if a.get("uptrend_momentum_fired"):
        tags.append("trig_uptrend")
    if a.get("downtrend_momentum_fired"):
        tags.append("trig_downtrend")
    if a.get("daily_mover_fired"):
        tags.append("trig_daily_mover")
    try:
        if int(a.get("slow_burn_count") or 0) > 0 or a.get("slow_burn_fired"):
            tags.append("trig_slow_burn")
    except (TypeError, ValueError):
        pass
    return tags


def trade_from_forward(a: Dict[str, Any], forward: List[Candle],
                       hold_bars: int, cost_pct: float) -> Optional[pf.Trade]:
    """Score one analysis on its forward candles (already filtered to
    t >= created_at). Entry at the first bar OPEN, exit at bar `hold_bars`
    CLOSE (no look-ahead). Returns None without enough forward bars."""
    side = analysis_side(a)
    if side is None or len(forward) < hold_bars + 1:
        return None
    entry_px, exit_px = float(forward[0].o), float(forward[hold_bars].c)
    if entry_px <= 0:
        return None
    gross, net = pf.forward_trade(entry_px, exit_px, side, cost_pct)
    return pf.Trade(
        source="llm_verdict", side=side, coin=str(a.get("coin") or ""),
        entry_ts=int(a.get("created_at", 0) or 0),
        gross_pct=gross, net_pct=net, exit_ts=int(forward[hold_bars].t),
    )


def logged_verdict(stats: pf.PFStats, min_samples: int,
                   gate_pf: float) -> str:
    """Single-horizon gate: enough samples AND net PF clears the gate
    (None PF — no losses — clears)."""
    if stats.n < min_samples:
        return "INSUFFICIENT"
    if stats.net_pf is not None and stats.net_pf < gate_pf:
        return "FAIL"
    return "PASS"


# ──────────────────────────── collection (I/O) ──────────────────────────────

def _slip_for(mem: Dict[str, Any], coin: str, fallback: float) -> float:
    return (pf.measured_slip_bps(mem, coin) if mem else 0.0) or fallback


def collect_offline(args: argparse.Namespace, windows: List[Tuple[str, int]],
                    hold_bars: Dict[str, int], taker_bps: float,
                    mem: Dict[str, Any]) -> Dict[str, Dict[str, Dict[str, List[pf.Trade]]]]:
    """window -> source -> side(long/short/all) -> trades (candles-only replay)."""
    bucket: Dict[str, Dict[str, Dict[str, List[pf.Trade]]]] = {
        f"{iv}:{days}d": {spec.source: {"long": [], "short": [], "all": []}
                          for spec in pf.SIGNAL_SPECS}
        for iv, days in windows
    }
    now_ms = int(time.time() * 1000)
    for iv, days in windows:
        wkey = f"{iv}:{days}d"
        need = pf.bars_needed(iv, days, hold_bars[iv])
        eval_since = now_ms - days * 86_400_000
        for coin in args.coins_list:
            slip = _slip_for(mem, coin, args.slippage_bps)
            cost_pct = pf.round_trip_cost_pct(taker_bps, slip) if args.with_costs else 0.0
            candles = btlog.fetch_candles_at(coin, iv, need, now_ms)
            if not candles:
                continue
            for t in pf.replay_signals(candles, pf.SIGNAL_SPECS, hold_bars[iv],
                                       cost_pct, only_since_ts=eval_since, coin=coin):
                for side_key in (t.side, "all"):
                    bucket[wkey][t.source][side_key].append(t)
    return bucket


def collect_logged(args: argparse.Namespace, taker_bps: float,
                   mem: Dict[str, Any]) -> Dict[str, Dict[str, List[pf.Trade]]]:
    """tag -> side(long/short/all) -> trades, from persisted LONG/SHORT
    analyses replayed on forward 5m candles."""
    bucket: Dict[str, Dict[str, List[pf.Trade]]] = {}
    cutoff_ms = int(time.time() * 1000) - args.logged_days * 86_400_000
    analyses = iter_directional_analyses(mem, cutoff_ms)
    hold = args.logged_hold_bars
    interval_ms = pf._INTERVAL_MS[_LOGGED_INTERVAL]
    for a in analyses:
        coin = str(a.get("coin") or "")
        ts = int(a.get("created_at", 0) or 0)
        slip = _slip_for(mem, coin, args.slippage_bps)
        cost_pct = pf.round_trip_cost_pct(taker_bps, slip) if args.with_costs else 0.0
        end_ms = ts + (hold + 5) * interval_ms
        forward = btlog.fetch_candles_at(coin, _LOGGED_INTERVAL, hold + 10, end_ms)
        if not forward:
            continue
        forward = [b for b in forward if b.t >= ts]
        trade = trade_from_forward(a, forward, hold, cost_pct)
        if trade is None:
            continue
        for tag in logged_tags(a):
            slot = bucket.setdefault(tag, {"long": [], "short": [], "all": []})
            for side_key in (trade.side, "all"):
                slot[side_key].append(trade)
    return bucket


# ───────────────────────────────── report ───────────────────────────────────

def _rank_key(stats: pf.PFStats) -> Tuple[float, int]:
    return (stats.net_pf if stats.net_pf is not None else 999.0, stats.n)


def _pf_cell(st: pf.PFStats, with_costs: bool) -> str:
    pfv = st.net_pf if with_costs else st.gross_pf
    label = "net" if with_costs else "gross"
    return f"n={st.n:<5} {label} PF {pf._pf_str(pfv):<16}"


def _print_offline(bucket: Dict[str, Any], windows: List[Tuple[str, int]],
                   args: argparse.Namespace) -> List[Tuple[str, str, str]]:
    """Print the offline leaderboard; return below-gate (source, side, verdict)."""
    wkeys = [f"{iv}:{days}d" for iv, days in windows]
    longest_iv, longest_days = max(windows, key=lambda w: w[1])
    lwkey = f"{longest_iv}:{longest_days}d"
    print(f"## A. Offline signal leaderboard (candles-only, ranked by "
          f"{lwkey} {'net' if args.with_costs else 'gross'} PF)")
    header = (f"{'signal':<26} {'side':<5} | "
              + " | ".join(f"{wk:>26}" for wk in wkeys) + " | verdict")
    print(header)
    print("-" * len(header))
    rows = []
    for spec in pf.SIGNAL_SPECS:
        for side in ("long", "short", "all"):
            stats_list = [pf.pf_stats(bucket[wk][spec.source][side]) for wk in wkeys]
            rows.append((spec.source, side, stats_list))
    rows.sort(key=lambda r: _rank_key(
        pf.pf_stats(bucket[lwkey][r[0]][r[1]])), reverse=True)
    below: List[Tuple[str, str, str]] = []
    for source, side, stats_list in rows:
        cells = [_pf_cell(st, args.with_costs) for st in stats_list]
        verdict = pf.gate_verdict(stats_list, args.min_samples, args.gate_pf)
        if verdict == "FAIL":
            below.append((source, side, verdict))
        print(f"{source:<26} {side:<5} | "
              + " | ".join(f"{c:>26}" for c in cells) + f" | {verdict}")
    print()
    return below


def _print_logged(bucket: Dict[str, Dict[str, Dict[str, List[pf.Trade]]]],
                  args: argparse.Namespace) -> Tuple[List[Tuple[str, str, str]], int]:
    """Print the logged-verdict context leaderboard; return (below-gate rows,
    total directional analyses replayed)."""
    print(f"## B. Logged-verdict (LLM) context census — forward {_LOGGED_INTERVAL} "
          f"hold {args.logged_hold_bars} bars, last {args.logged_days}d")
    total = len({id(t) for tag in bucket for t in bucket[tag]["all"]})
    if total == 0:
        print(f"#   INSUFFICIENT: 0 persisted LONG/SHORT analyses in the last "
              f"{args.logged_days}d (whale/news/composite emit no verdict of "
              f"their own; the LLM is the only directional source). Buckets "
              f"score automatically once analyses accumulate.")
        print()
        return [], 0
    rows = []
    for tag in sorted(bucket):
        for side in ("long", "short", "all"):
            st = pf.pf_stats(bucket[tag][side])
            rows.append((tag, side, st))
    rows.sort(key=lambda r: _rank_key(r[2]), reverse=True)
    header = (f"{'context bucket':<24} {'side':<5} | {'n':>6} | "
              f"{'gross PF':>14} | {'net PF':>14} | 1st/2nd half net | verdict")
    print(header)
    print("-" * len(header))
    below: List[Tuple[str, str, str]] = []
    for tag, side, st in rows:
        verdict = logged_verdict(st, args.min_samples, args.gate_pf)
        if verdict == "FAIL":
            below.append((tag, side, verdict))
        halves = (f"{pf._pf_str(st.net_first_half)}/"
                  f"{pf._pf_str(st.net_second_half)}")
        print(f"{tag:<24} {side:<5} | {st.n:>6} | {pf._pf_str(st.gross_pf):>14} | "
              f"{pf._pf_str(st.net_pf):>14} | {halves:>15} | {verdict}")
    print()
    return below, total


def run(args: argparse.Namespace) -> int:
    windows: List[Tuple[str, int]] = [pf.parse_window(w) for w in args.windows.split(",")]
    hold_bars: Dict[str, int] = {}
    if args.hold_bars:
        for hb in args.hold_bars.split(","):
            iv, n = hb.split(":")
            hold_bars[iv] = int(n)
    for iv, _ in windows:
        hold_bars.setdefault(iv, pf._DEFAULT_HOLD_BARS.get(iv, 6))
    args.coins_list = [c.strip().upper() for c in args.coins.split(",") if c.strip()]

    taker_bps = args.taker_fee_bps
    if taker_bps is None:
        taker_bps = float(cfg_get("execution.taker_fee_pct", default=0.025)) * 100.0
    try:
        from _memory_io import load_memory
        mem = load_memory(args.memory_file)
    except Exception:
        mem = {}

    btlog._API_SLEEP_S = max(0.0, float(args.api_sleep or 0.0))
    btlog._load_disk_cache(args.cache_file)

    print("# S2 signal-source Profit Factor census (strategy-direction.md §4)")
    print(f"# coins: {', '.join(args.coins_list)} | gate net PF {args.gate_pf:g}, "
          f"min samples {args.min_samples}/bucket | costs "
          f"{'ON (taker ' + format(taker_bps, 'g') + 'bps + slip/side)' if args.with_costs else 'OFF (gross)'}")
    print()

    offline = collect_offline(args, windows, hold_bars, taker_bps, mem)
    logged = collect_logged(args, taker_bps, mem)
    btlog._save_disk_cache(args.cache_file)

    below_off = _print_offline(offline, windows, args)
    below_log, n_logged = _print_logged(logged, args)

    print("## C. Below-gate sources (net PF < "
          f"{args.gate_pf:g} with >= {args.min_samples} samples) — down-weight/cut:")
    found = False
    for source, side, _ in below_off:
        print(f"#   [offline] {source} / {side}")
        found = True
    for tag, side, _ in below_log:
        print(f"#   [logged]  {tag} / {side}")
        found = True
    if not found:
        print("#   (none — every sufficiently-sampled bucket clears the gate)")
    print()
    print(f"# summary: {len(pf.SIGNAL_SPECS)} offline signals x 2 windows; "
          f"{n_logged} directional LLM analyses replayed into "
          f"{len(logged)} context buckets.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--windows", default="4h:365d,1h:180d",
                    help="offline replay windows, comma list INTERVAL:SPANd")
    ap.add_argument("--coins", default="BTC,ETH", help="comma list of coins")
    ap.add_argument("--hold-bars", default="",
                    help="per-interval offline forward hold, e.g. '4h:6,1h:24'")
    ap.add_argument("--logged-hold-bars", type=int, default=_DEFAULT_LOGGED_HOLD_BARS,
                    help="forward hold bars for logged LLM verdicts at 5m (default 288 ~24h)")
    ap.add_argument("--logged-days", type=int, default=_DEFAULT_LOGGED_DAYS,
                    help="lookback days for persisted analyses (default 30, matches memory TTL)")
    ap.add_argument("--memory-file", default=str(_REPO / ".agent-memory.json"),
                    help="path to .agent-memory.json (analyses/perceptions/closes)")
    ap.add_argument("--with-costs", action="store_true",
                    help="deduct taker fee + slippage round trip (net PF); gross always shown")
    ap.add_argument("--taker-fee-bps", type=float, default=None,
                    help="per-side taker fee bps (default execution.taker_fee_pct, 2.5)")
    ap.add_argument("--slippage-bps", type=float, default=pf._DEFAULT_SLIPPAGE_BPS,
                    help="assumed adverse slippage bps/side when no measured fills exist")
    ap.add_argument("--min-samples", type=int, default=pf._MIN_SAMPLES_DEFAULT,
                    help="minimum instances per bucket to score (default 30)")
    ap.add_argument("--gate-pf", type=float, default=pf._GATE_PF_DEFAULT,
                    help="admission net PF (default 1.05)")
    ap.add_argument("--cache-file",
                    default=os.path.join(tempfile.gettempdir(), "hermes_pf_s2_census_candles.json"),
                    help="disk cache for historical candles; empty string disables")
    ap.add_argument("--api-sleep", type=float, default=0.0,
                    help="seconds to sleep before uncached Hyperliquid candle requests")
    args = ap.parse_args()
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
