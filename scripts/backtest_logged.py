#!/usr/bin/env python3
"""Re-filter logged AI verdicts through CURRENT gates + DSL config.

Reads ~200 cached analyses from .agent-memory.json, joins each to its
perception for composite/triggers, then simulates execution + DSL exit
on historical 5m bars using the live config. Tells you "what would
today's strategy have done on yesterday's actual AI verdicts."

Free (no LLM calls). Runs in ~30s. The complement to backtest_full.py:
that one re-asks the AI fresh; this one trusts yesterday's AI verdicts.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# P3-17: backtest process — never load a live mainnet private key.
os.environ["HERMES_BACKTEST"] = "1"

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO))
_env = _REPO / ".env.local"
if _env.exists():
    for line in _env.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            if k.strip() == "HYPERLIQUID_PRIVATE_KEY":
                continue
            os.environ.setdefault(k.strip(), v.strip())

from dataclasses import replace

from _memory_io import load_memory

from hermes_trader.agents.config_store import cfg_get, read_agent_config
from hermes_trader.agents.dsl_exit import _build_policy_from_config
from hermes_trader.agents.sizing import atr_equal_risk_notional
from hermes_trader.backtest import cost as kcost
from hermes_trader.backtest.exit_dsl import DslBarExit
from hermes_trader.backtest.types import ExitEvent, ExitReason
from hermes_trader.client.exchange import get_max_leverage
from hermes_trader.data import historical_candles as hc
from hermes_trader.indicators.math import atr as calc_atr
from hermes_trader.models.types import Candle

# P4-7: this script no longer owns a candle cache. All historical bars come
# from the shared point-in-time data layer (hermes_trader.data.historical_candles),
# which is the same append-only (coin, interval, t) bar store that
# `collect_candles.py` pre-warms and the backfill_* research scripts use. The
# shims below preserve the old call surface (fetch_candles_at / _load_disk_cache
# / _save_disk_cache) for this script and its four pf_*/signal_* descendants.
_INTERVAL_MS = hc.INTERVAL_MS
_API_FAILURES = 0
_API_SLEEP_S = 0.0
_DISK_CACHE_FILE = ""


def _load_disk_cache(path: str) -> None:
    """Compatibility shim: point the kernel bar store at ``path``.

    The kernel lazily loads on first fetch, so this only pins the target file.
    An empty path disables a pinned location (the kernel then falls back to its
    HERMES_HIST_CANDLE_CACHE / /data default).
    """
    global _DISK_CACHE_FILE
    _DISK_CACHE_FILE = path or ""
    hc.set_cache_file(path or None)


def _save_disk_cache(path: str) -> bool:
    """Compatibility shim: atomically flush newly cached kernel bars."""
    return hc.flush_disk_cache(path or None)


def fetch_candles_at(coin: str, interval: str, count: int, end_ms: int) -> Optional[List[Candle]]:
    """Return the most recent ``count`` bars CLOSED at or before ``end_ms``.

    Thin PIT wrapper over the shared data layer. Bar-open ``t`` of the newest
    returned bar is ``<= end_ms - interval`` (no still-forming bar, no future
    price). Returns ``None`` on a fetch error (legacy contract: callers skip
    the verdict as no-data) and ``[]``-like short results when history is thin.
    """
    global _API_FAILURES
    if _API_SLEEP_S > 0:
        time.sleep(_API_SLEEP_S)
    try:
        bars = hc.closed_bars_as_of(
            coin, interval,
            max(0, end_ms - (count + 1) * hc.INTERVAL_MS[interval]),
            end_ms)
    except Exception:
        _API_FAILURES += 1
        return None
    return bars[-count:] if len(bars) > count else bars


def fetch_forward_bars(coin: str, interval: str,
                       entry_ms: int, end_ms: int) -> Optional[List[Candle]]:
    """Closed bars with bar-open ``t >= entry_ms`` grid and closed by ``end_ms``.

    ``bars[0]`` is the ENTRY bar (the bar opening at/just after ``entry_ms``),
    already closed in this offline replay — the DSL engine treats its open as
    the fill and its high/low as intra-bar path. This is the PIT-correct
    replacement for the old replay fetch, which could include a still-forming
    forward bar. ``None`` on a fetch error (caller skips as no-data).
    """
    global _API_FAILURES
    if _API_SLEEP_S > 0:
        time.sleep(_API_SLEEP_S)
    step = hc.INTERVAL_MS[interval]
    start_grid = entry_ms - (entry_ms % step)
    try:
        bars = hc.fetch_candle_range(coin, interval, start_grid, end_ms)
    except Exception:
        _API_FAILURES += 1
        return None
    return [b for b in bars if b.t + step <= end_ms]


def detect_regime_at(end_ms: int, proxy: str = "BTC") -> str:
    from hermes_trader.indicators.math import ema
    candles = fetch_candles_at(proxy, "1h", 100, end_ms)
    if not candles or len(candles) < 50:
        return "neutral"
    closes = [c.c for c in candles]
    fast = ema(closes, 20)
    slow = ema(closes, 50)
    if len(fast) < 9:
        return "neutral"
    f_prev = fast[-9]
    if f_prev == 0:
        return "neutral"
    slope = (fast[-1] - f_prev) / abs(f_prev)
    if fast[-1] > slow[-1] and slope > 0.002:
        return "up"
    if fast[-1] < slow[-1] and slope < -0.002:
        return "down"
    return "neutral"


def entry_atr4h(coin: str, end_ms: int) -> float:
    candles = fetch_candles_at(coin, "4h", 80, end_ms)
    if not candles or len(candles) < 20:
        return 0.0
    vals = [
        float(v) for v in calc_atr(candles, 14)
        if not (v != v or v in (float("inf"), float("-inf")))
    ]
    return vals[-1] if vals else 0.0


def max_leverage_for(coin: str, fallback: int) -> int:
    try:
        lev = int(get_max_leverage(coin))
    except Exception:
        lev = int(fallback)
    return max(1, min(int(fallback), lev))


def live_sized_notional(
    *,
    coin: str,
    entry_px: float,
    entry_ms: int,
    equity: float,
    equity_fraction: float,
    leverage: int,
    cfg: Dict[str, Any],
    dsl_cfg: Dict[str, Any],
) -> Tuple[float, str]:
    cap = float(cfg.get("max_trade_notional_usd", 0) or 0)
    coin_lev = max_leverage_for(coin, leverage)
    atr_cfg = cfg.get("atr_risk_sizing", {}) or {}
    if bool(atr_cfg.get("enabled", False)):
        risk_pct = float(atr_cfg.get("risk_per_trade_pct", 0.0075) or 0.0)
        basis = str(atr_cfg.get("sizing_basis", "atr_stop") or "atr_stop").lower()
        if basis in ("primary_stop", "dsl_stop"):
            # P2-9: no inline `or 2.0 / or 40.0` fallbacks — those stale values
            # diverged from the audited live gates (executor.py uses cfg_get with
            # the canonical default). Let a missing/None value surface from
            # cfg_get like the live path and simulate_dsl_exit do.
            stop_frac = min(
                float(cfg_get("dsl_exit.max_loss_pct", config=dsl_cfg)),
                float(cfg_get("dsl_exit.max_loss_roe_pct", config=dsl_cfg)) / max(1, coin_lev),
            ) / 100.0
            if equity <= 0 or risk_pct <= 0 or stop_frac <= 0:
                return 0.0, "primary_stop_invalid"
            notional = (risk_pct * equity) / stop_frac
            notional = min(notional, equity * coin_lev)
            if cap > 0:
                notional = min(notional, cap)
            return notional, f"primary_stop risk={risk_pct:g}"
        atr4h = entry_atr4h(coin, entry_ms)
        sz = atr_equal_risk_notional(
            equity=equity,
            risk_per_trade_pct=risk_pct,
            atr_abs=atr4h,
            entry_px=entry_px,
            sl_atr_mult=float(cfg_get("sl_atr_mult", config=cfg) or 1.5),
            max_trade_notional_usd=cap,
            coin_max_leverage=coin_lev,
            config_max_leverage=leverage,
        )
        return sz.notional_usd, f"atr_stop risk={risk_pct:g}"

    notional = equity * equity_fraction * coin_lev
    if cap > 0:
        notional = min(notional, cap)
    return notional, "legacy_fraction"


def passes_counter_regime(side: str, regime: str, conf: float, composite: float,
                          burst_fired: bool, slow_fired: bool, min_conf: float) -> bool:
    if regime == "neutral":
        return True
    aligned = (regime == "up" and side == "long") or (regime == "down" and side == "short")
    if aligned:
        return True
    return conf >= min_conf or composite >= 50 or burst_fired or slow_fired


def replay_exit_bars(
    entry_px: float,
    side: str,
    leverage: int,
    entry_ms: int,
    bars_5m: List[Candle],
    policy: Any,
    cost: kcost.CostModel,
    notional: float,
    entry_atr_pct: float = 0.0,
    entry_regime: str = "",
) -> Tuple[str, int, float, float, float, float]:
    """Drive the PRODUCTION DSL exit (P4 kernel) over one logged verdict.

    ``bars_5m[0]`` is the ENTRY bar (position fills at its open); the adapter
    feeds it as bar 0 so a same-bar gap stop is caught, then the remaining
    forward bars. If no exit fires inside the fetched window the position is
    marked at the last bar's close (``end_of_data``). Returns
    ``(reason, exit_bar_index, exit_ref_px, exit_fill_px, pnl_gross, pnl_net)``.
    """
    engine = DslBarExit(side=side, entry_px=entry_px, entry_time_ms=entry_ms,
                        policy=policy, leverage=max(1, leverage), coin="REPLAY",
                        entry_atr_pct=entry_atr_pct, entry_regime=entry_regime)
    event: Optional[ExitEvent] = None
    for idx, bar in enumerate(bars_5m):
        event = engine.on_bar(bar, idx)
        if event is not None:
            break
    if event is None:
        last = bars_5m[-1]
        event = ExitEvent(len(bars_5m) - 1, ExitReason.END_OF_DATA, last.c)

    fill_entry = cost.fill_entry(entry_px, side)
    fill_exit = cost.fill_exit(event.ref_px, side, event.reason)
    gross, net = cost.pnl_usd(side, fill_entry, fill_exit, notional)
    return (event.reason.value, event.bar_index, event.ref_px, fill_exit, gross, net)


def main() -> int:
    global _API_SLEEP_S, _DISK_CACHE_FILE
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=int, default=24)
    ap.add_argument("--equity", type=float, default=250.0)
    ap.add_argument("--dedup-min", type=int, default=None,
                    help="Treat same-coin analyses within N minutes as one trade "
                         "(default: live cooldown_min)")
    ap.add_argument("--loss-cooldown-min", type=int, default=None,
                    help="Block same-coin re-entry for N minutes after a simulated loss "
                         "(default: off; live uses loss_cooldown_min)")
    ap.add_argument("--mode", default="ai", choices=["ai", "lowconf", "force", "sidestep"],
                    help="ai=as-is; lowconf=lower min-conf; force=+composite-force PASS->LONG; "
                         "sidestep=ignore AI, take all TA-confirmed LONGs")
    ap.add_argument("--min-conf", type=float, default=0.60, help="min conf for lowconf mode")
    ap.add_argument("--force-bar", type=float, default=30.0, help="composite bar for force/sidestep")
    ap.add_argument("--sidestep-min-slow-burn", type=int, default=1,
                    help="slow_burn_count required for sidestep admission (default=live legacy 1)")
    ap.add_argument("--long-only", action="store_true", help="Skip admitted SHORT entries")
    ap.add_argument("--regime-mode", choices=["live", "neutral", "up", "down"], default="live",
                    help="Counter-regime model. live calls HL/BTC regime; fixed modes are deterministic.")
    ap.add_argument("--leverage", type=int, default=0, help="override leverage (0=use config)")
    ap.add_argument("--equity-fraction", type=float, default=0.0, help="override fraction (0=use config)")
    ap.add_argument("--max-notional", type=float, default=0.0, help="override max_trade_notional_usd (0=config)")
    ap.add_argument("--risk-pct", type=float, default=0.0, help="override atr_risk_sizing.risk_per_trade_pct (0=config)")
    ap.add_argument("--sizing-basis", default="", help="override atr_risk_sizing.sizing_basis")
    ap.add_argument("--roe-cap", type=float, default=0.0, help="override max_loss_roe_pct (0=config)")
    ap.add_argument("--max-loss", type=float, default=0.0, help="override max_loss_pct spot stop (0=config)")
    ap.add_argument("--protect", type=float, default=0.0, help="override dsl_exit.protect_pct (0=config)")
    ap.add_argument("--retrace", type=float, default=0.0, help="override dsl_exit.retrace_threshold (0=config)")
    ap.add_argument("--taker-fee-bps", type=float, default=2.5,
                    help="Per-side taker fee in bps, converted to ROE by leverage")
    ap.add_argument("--slippage-bps", type=float, default=0.0,
                    help="Optional adverse slippage per side in bps for stress tests")
    ap.add_argument("--exclude-hip3", action="store_true",
                    help="Skip colon-namespaced HIP-3 markets in the replay")
    ap.add_argument("--api-sleep", type=float, default=0.0,
                    help="Seconds to sleep before uncached Hyperliquid candle requests")
    ap.add_argument("--summary-only", action="store_true",
                    help="Suppress per-trade rows; print only aggregate results")
    ap.add_argument("--cache-file", default=None,
                    help="Kernel bar-cache file (default: HERMES_HIST_CANDLE_CACHE "
                         "or /data/.historical-candles.json, shared with collect_candles "
                         "and backfill scripts); set empty string for the kernel default")
    ap.add_argument("--apply-runner-gate", action="store_true",
                    help="Apply executor.runner_entry_gate to admitted trades")
    ap.add_argument("--runner-min-confidence", type=float, default=None,
                    help="Override runner_entry_gate.min_confidence for this replay")
    ap.add_argument("--runner-min-composite", type=float, default=None,
                    help="Override runner_entry_gate.min_composite for this replay")
    ap.add_argument("--runner-min-hip3-composite", type=float, default=None,
                    help="Override runner_entry_gate.min_hip3_composite for this replay")
    ap.add_argument("--runner-mover-min-confidence", type=float, default=None,
                    help="Override runner_entry_gate.mover_min_confidence for this replay")
    ap.add_argument("--runner-mover-min-composite", type=float, default=None,
                    help="Override runner_entry_gate.mover_min_composite for this replay")
    args = ap.parse_args()
    _API_SLEEP_S = max(0.0, float(args.api_sleep or 0.0))
    _DISK_CACHE_FILE = args.cache_file
    _load_disk_cache(_DISK_CACHE_FILE)

    cfg = read_agent_config()
    if args.max_notional:
        cfg = dict(cfg)
        cfg["max_trade_notional_usd"] = args.max_notional
    if args.risk_pct or args.sizing_basis:
        cfg = dict(cfg)
        atr_cfg = dict(cfg.get("atr_risk_sizing", {}) or {})
        if args.risk_pct:
            atr_cfg["risk_per_trade_pct"] = args.risk_pct
        if args.sizing_basis:
            atr_cfg["sizing_basis"] = args.sizing_basis
        cfg["atr_risk_sizing"] = atr_cfg
    dsl_cfg = dict(cfg.get("dsl_exit", {}))
    if args.roe_cap:
        dsl_cfg["max_loss_roe_pct"] = args.roe_cap
    if args.max_loss:
        dsl_cfg["max_loss_pct"] = args.max_loss
    if args.protect:
        dsl_cfg["protect_pct"] = args.protect
    if args.retrace:
        dsl_cfg["retrace_threshold"] = args.retrace
    # P4 kernel: one production ExitPolicy (CLI overrides applied on top), and a
    # single cost contract. The adapter copies the policy per trade, so no state
    # is shared across replays. --taker-fee/--slippage map to round-trip fee +
    # symmetric per-side slip (stop_delay=0 preserves the script's legacy
    # uniform-slip treatment).
    base_policy = replace(
        _build_policy_from_config(),
        max_loss_pct=float(dsl_cfg["max_loss_pct"]),
        max_loss_roe_pct=float(dsl_cfg["max_loss_roe_pct"]),
        protect_pct=float(dsl_cfg["protect_pct"]),
        retrace_threshold=float(dsl_cfg["retrace_threshold"]),
        hard_timeout_minutes=float(dsl_cfg["hard_timeout_minutes"]),
    )
    cost_model = kcost.CostModel(
        round_trip_fee_bps=args.taker_fee_bps * 2,
        entry_slip_bps=args.slippage_bps,
        exit_slip_bps=args.slippage_bps,
        stop_delay_slip_bps=0.0,
    )
    counter_regime_min_conf = float(cfg_get("counter_regime_min_conf", config=cfg))
    equity_fraction = float(args.equity_fraction or cfg.get("equity_fraction_per_trade", 0.04))
    base_leverage = int(args.leverage or cfg_get("leverage", config=cfg))
    min_ai_conf = float(cfg_get("min_ai_confidence", config=cfg))
    dedup_min = int(args.dedup_min if args.dedup_min is not None
                    else cfg.get("cooldown_min", 30))

    runner_cfg = cfg
    runner_overrides = {
        "min_confidence": args.runner_min_confidence,
        "min_composite": args.runner_min_composite,
        "min_hip3_composite": args.runner_min_hip3_composite,
        "mover_min_confidence": args.runner_mover_min_confidence,
        "mover_min_composite": args.runner_mover_min_composite,
    }
    if args.apply_runner_gate and any(v is not None for v in runner_overrides.values()):
        gate = dict(cfg.get("runner_entry_gate") or {})
        for key, val in runner_overrides.items():
            if val is not None:
                gate[key] = float(val)
        runner_cfg = dict(cfg)
        runner_cfg["runner_entry_gate"] = gate

    mem = load_memory(_REPO / ".agent-memory.json")
    analyses = mem.get("analyses", [])
    perceptions_by_id = {p["id"]: p for p in mem.get("perceptions", []) if "id" in p}

    now_ms = int(time.time() * 1000)
    cutoff = now_ms - args.hours * 3600_000
    analyses = [a for a in analyses if a.get("created_at", 0) >= cutoff]
    if args.exclude_hip3:
        analyses = [a for a in analyses if ":" not in (a.get("coin") or "")]
    analyses.sort(key=lambda a: a.get("created_at", 0))

    print(f"# Counterfactual replay of {len(analyses)} logged analyses (last {args.hours}h)")
    print(f"# Equity ${args.equity:.0f} | leverage {base_leverage}x | fraction {equity_fraction}")
    print(f"# Sizing: {cfg.get('atr_risk_sizing', {}) if cfg.get('atr_risk_sizing') else 'legacy fraction'} "
          f"| cap ${float(cfg.get('max_trade_notional_usd', 0) or 0):g}")
    print(f"# Same-coin cooldown/dedup: {dedup_min}min")
    round_trip_cost_roe = ((args.taker_fee_bps + args.slippage_bps) * 2 * base_leverage / 100.0)
    print(f"# Costs: taker {args.taker_fee_bps:g}bps/side"
          f"{' + slippage ' + str(args.slippage_bps) + 'bps/side' if args.slippage_bps else ''}"
          f" = {round_trip_cost_roe:.2f}% ROE/trade")
    if args.apply_runner_gate:
        print(f"# Runner gate: {runner_cfg.get('runner_entry_gate', {})}")
    print(f"# DSL: max_loss={dsl_cfg.get('max_loss_pct')}% / {dsl_cfg.get('max_loss_roe_pct')}% ROE | "
          f"protect={dsl_cfg.get('protect_pct')}% | timeout={dsl_cfg.get('hard_timeout_minutes')}min")
    print("# Exit engine: production DSLTracker via the P4 kernel adapter (live policy; entry bar "
          "is bar 0, so a same-bar gap stop is caught; end-of-window -> end_of_data)")
    print()

    # Dedup window: skip same-coin within N minutes of a previous trade
    dedup_ms = dedup_min * 60_000
    last_trade_by_coin: Dict[str, int] = {}

    # Cache regime per coarse 30-min bucket to save HL calls
    regime_cache: Dict[int, str] = {}
    def _regime_at(t: int) -> str:
        if args.regime_mode != "live":
            return args.regime_mode
        bucket = t // (30 * 60_000)
        if bucket not in regime_cache:
            regime_cache[bucket] = detect_regime_at(t)
        return regime_cache[bucket]

    pnl_total = 0.0
    wins, losses = [], []
    skipped_pass, skipped_dup, skipped_conf, skipped_regime = 0, 0, 0, 0
    skipped_nodata, skipped_size, skipped_loss_cooldown = 0, 0, 0
    by_reason: Dict[str, List[float]] = {}
    by_reason_pnl: Dict[str, List[float]] = {}
    trades: List[Tuple[Any, ...]] = []
    notionals: List[float] = []
    sizing_labels: Dict[str, int] = {}

    n_forced = 0
    loss_block_until: Dict[str, int] = {}
    loss_cooldown_ms = 0
    if args.loss_cooldown_min is not None:
        loss_cooldown_ms = max(0, int(args.loss_cooldown_min) * 60_000)
    for a in analyses:
        verdict = a.get("verdict")
        coin = a.get("coin")
        ts = int(a.get("created_at", 0))
        if not coin or ts == 0:
            continue
        conf = float(a.get("confidence", 0))
        # perception (composite/triggers) — needed for force/sidestep admission
        perc = perceptions_by_id.get(a.get("perception_id"))
        composite = float(
            (perc or {}).get("composite_score", a.get("composite_score", 0)) or 0
        )
        triggers = (perc or {}).get("triggers", []) or []
        burst_fired = (
            any(t.get("name") == "momentumBurst" and t.get("fired") for t in triggers)
            or bool(a.get("momentum_burst_fired", False))
        )
        slow_count = sum(
            1 for t in triggers
            if t.get("name") in ("volumeBuildup1h", "trendFlip1h", "higherLows1h")
            and t.get("fired")
        )
        if slow_count <= 0:
            slow_count = int(a.get("slow_burn_count", 0) or 0)
        slow_fired = slow_count > 0
        ta_confirmed = (
            composite >= args.force_bar
            or burst_fired
            or slow_count >= max(1, int(args.sidestep_min_slow_burn or 1))
        )

        # ── mode-aware admission ─────────────────────────────────────────────
        ai_ls = verdict in ("LONG", "SHORT")
        admit, side, forced, sidestep_override = False, None, False, False
        if args.mode == "ai":
            admit = ai_ls and conf >= min_ai_conf
            side = ("long" if verdict == "LONG" else "short") if ai_ls else None
        elif args.mode == "lowconf":
            admit = ai_ls and conf >= args.min_conf
            side = ("long" if verdict == "LONG" else "short") if ai_ls else None
        elif args.mode == "force":
            if ai_ls and conf >= min_ai_conf:
                admit, side = True, ("long" if verdict == "LONG" else "short")
            elif composite >= args.force_bar:            # composite-force PASS -> LONG
                admit, side, forced = True, "long", True
                conf = max(conf, min_ai_conf)
        elif args.mode == "sidestep":                    # ignore AI; take all TA-confirmed LONGs
            if ta_confirmed:
                admit, side, forced, sidestep_override = True, "long", (not ai_ls), True
                conf = max(conf, min_ai_conf)
            elif ai_ls and conf >= min_ai_conf:
                admit, side = True, ("long" if verdict == "LONG" else "short")
        if not admit:
            skipped_pass += 1
            continue
        if args.long_only and side == "short":
            skipped_pass += 1
            continue
        if forced:
            n_forced += 1
        if coin in last_trade_by_coin and (ts - last_trade_by_coin[coin]) < dedup_ms:
            skipped_dup += 1
            continue
        if loss_cooldown_ms > 0 and ts < loss_block_until.get(coin, 0):
            skipped_loss_cooldown += 1
            continue

        if args.apply_runner_gate:
            from hermes_trader.agents.executor import _runner_entry_block_reason
            gate_analysis = dict(a)
            gate_analysis["side"] = side
            if forced:
                gate_analysis["reasoning"] = "[structural override] " + str(gate_analysis.get("reasoning") or "")
                gate_analysis["confidence"] = conf
            if sidestep_override:
                gate_analysis["sidestep_override"] = True
            gate = runner_cfg.get("runner_entry_gate") or {}
            skip_runner = sidestep_override and bool(gate.get("bypass_sidestep_overrides", False))
            if not skip_runner:
                blocked = _runner_entry_block_reason(gate_analysis, runner_cfg)
                if blocked:
                    skipped_pass += 1
                    continue

        regime = _regime_at(ts)
        if not passes_counter_regime(side, regime, conf, composite, burst_fired, slow_fired,
                                     counter_regime_min_conf):
            skipped_regime += 1
            continue

        # Fetch the entry bar + forward 5m bars (DSL window). fetch_forward_bars
        # returns only CLOSED bars (no still-forming bar): bars[0] opens at the
        # analysis grid and is the entry bar the DSL fills at / paths through.
        timeout_min = float(cfg_get("dsl_exit.hard_timeout_minutes", config=dsl_cfg))
        forward_end = ts + int(timeout_min * 60_000) + 600_000  # +10min padding
        forward = fetch_forward_bars(coin, "5m", ts, forward_end)
        if forward is None:
            skipped_nodata += 1
            continue
        if not forward:
            skipped_nodata += 1
            continue

        entry_px = forward[0].o  # open of the first bar at/after analysis
        if entry_px <= 0:
            skipped_nodata += 1
            continue
        # bars_5m[0] IS the entry bar; the adapter feeds it as bar 0 so a
        # same-bar gap stop is caught (the legacy local simulator skipped it).
        bars_5m = forward
        if len(bars_5m) < 2:
            skipped_nodata += 1
            continue

        notional, sizing_label = live_sized_notional(
            coin=coin,
            entry_px=entry_px,
            entry_ms=ts,
            equity=args.equity,
            equity_fraction=equity_fraction,
            leverage=base_leverage,
            cfg=cfg,
            dsl_cfg=dsl_cfg,
        )
        if notional < 10.5:
            skipped_size += 1
            continue

        # Production ATR-stop scales off the 4h ATR% captured at entry; fetch it
        # only when the live policy actually has an ATR stop enabled.
        atr_pct = (entry_atr4h(coin, ts) / entry_px * 100.0) if base_policy.atr_stop_enabled else 0.0
        reason, exit_bar, _exit_ref, exit_fill_px, _pnl_gross, pnl_usd = replay_exit_bars(
            entry_px, side, base_leverage, bars_5m[0].t, bars_5m, base_policy,
            cost_model, notional, entry_atr_pct=atr_pct, entry_regime=regime,
        )
        margin = notional / max(1, base_leverage)
        roe = pnl_usd / margin * 100.0
        pnl_total += pnl_usd
        notionals.append(notional)
        sizing_labels[sizing_label] = sizing_labels.get(sizing_label, 0) + 1
        last_trade_by_coin[coin] = ts
        if pnl_usd < 0 and loss_cooldown_ms > 0:
            exit_ts = bars_5m[0].t + int((exit_bar + 1) * 5 * 60_000)
            loss_block_until[coin] = exit_ts + loss_cooldown_ms
        (wins if pnl_usd > 0 else losses).append(pnl_usd)
        by_reason.setdefault(reason, []).append(roe)
        by_reason_pnl.setdefault(reason, []).append(pnl_usd)
        trades.append((ts, coin, side, conf, composite, roe, reason, pnl_usd))
        if not args.summary_only:
            print(f"  {_iso(ts)}  {coin:<14} {side:<5} conf={conf:.2f} comp={composite:>4.0f}  "
                  f"entry={entry_px:.6g} exit={exit_fill_px:.6g}  {reason:<18} ROE={roe:+6.1f}%  ${pnl_usd:+6.2f}")

    n = len(trades)
    wr = len(wins) / n if n else 0
    print()
    print("=" * 80)
    print(f"Trades:       {n}  ({len(wins)}W / {len(losses)}L, win rate {wr*100:.0f}%)")
    print(f"Total PnL:    ${pnl_total:+.2f}  ({pnl_total/args.equity*100:+.1f}% on ${args.equity:.0f})")
    if notionals:
        print(f"Avg notional: ${sum(notionals)/len(notionals):.0f}  sizing={sizing_labels}")
    print(f"Skipped:      {skipped_pass} PASS, {skipped_dup} dedup, {skipped_conf} low-conf, "
          f"{skipped_regime} counter-regime, {skipped_nodata} no-data, "
          f"{skipped_size} below-size, {skipped_loss_cooldown} loss-cooldown")
    print(f"API failures: {_API_FAILURES}")
    print()
    print("Exits by reason:")
    for reason in sorted(by_reason.keys(), key=lambda r: -sum(by_reason_pnl.get(r, []))):
        roes = by_reason[reason]
        avg = sum(roes)/len(roes)
        tot_pnl = sum(by_reason_pnl.get(reason, []))
        print(f"  {reason:<18} n={len(roes):>3}  avg ROE {avg:+6.1f}%  total ${tot_pnl:+7.2f}")
    _save_disk_cache(_DISK_CACHE_FILE)
    return 0


def _iso(ms: int) -> str:
    import datetime
    return datetime.datetime.fromtimestamp(ms/1000, tz=datetime.timezone.utc).strftime("%m-%d %H:%M")


if __name__ == "__main__":
    raise SystemExit(main())
