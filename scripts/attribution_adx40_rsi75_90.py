#!/usr/bin/env python3
"""Attribution analysis: ADX>=40 + RSI 75-90 LONG signals for kPEPE/CRV/ETH.

Compares three veto regimes on identical signals:

  DYN     - current production: ADX-scaled RSI threshold (90 at ADX>=40),
            hard-coded 2.5 ATR extension veto, resonance exception (dead code).
  FIXED   - DYN + dead-code fix (resonance RSI cap linked to long_thresh)
            + suggestion-2 overextended-long strong-trend exception.
  REGIME  - data-driven per-coin regime score (5 weighted components) that
            replaces the ADX ladder + static blacklist entirely.

For every LONG signal in the ADX>=40 + RSI>=75 zone, the script classifies
each regime's verdict and replays blocked signals through the SAME DSL exit
to estimate counterfactual PnL.

Usage:
    HERMES_BACKTEST=1 python3 scripts/attribution_adx40_rsi75_90.py
"""
from __future__ import annotations

import os
import sys
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

# Reuse helpers + DSL from the A/B backtest module.
from backtest_ab_compare import (
    _REGIME_TABLE,
    DSL,
    ROUND_TRIP_FEE_BPS,
    _adx_val,
    _atr_val,
    _ema_val,
    _evaluate_entry,
    _obv_slope,
    _regime_score,
    _resample_4h,
)

from hermes_trader.agents.config import get_config
from hermes_trader.client.hl_client import fetch_hl_candles
from hermes_trader.client.universe import get_universe
from hermes_trader.indicators import math as ind

TARGET_COINS = ["kPEPE", "CRV", "ETH"]
WARMUP = 120
DAYS = 21              # analysis window (most recent N days)
LOOKBACK_DAYS = 50     # fetch extra history so 1d EMA21/RSI has enough data
# NOTE: backtest_ab_compare._evaluate_entry only computes 1d resonance when
# len(closes_1h) >= 24*30 (720 bars = 30 days). With the default --days 21
# run (654 bars total) this threshold is NEVER reached, so rsi_1d is always
# None and daily_uptrend is always False — a second layer of dead code on
# top of the RSI-cap contradiction. We mirror that threshold here so the
# attribution is honest about what the backtest can actually compute.
DAILY_REQUIRE_BARS = 24 * 30


def _dyn_context(window_1h: List[Any]) -> Dict[str, Any]:
    """Compute the dynamic-RSI context indicators for a single bar."""
    closes_1h = [c.c for c in window_1h]
    e8 = _ema_val(closes_1h, 8)
    e21 = _ema_val(closes_1h, 21)
    bullish = e8 is not None and e21 is not None and e8 > e21
    atr_v = _atr_val(window_1h)
    adx_v = _adx_val(window_1h)
    ext_atr = (closes_1h[-1] - e21) / atr_v if (atr_v and e21 and atr_v > 0) else 0.0

    # 4h RSI (same construction as _evaluate_entry).
    rsi = ind.rsi_last([c.c for c in window_1h], 14) or 50.0  # fallback; 4h set by caller

    # 1d resonance (mirror backtest_ab_compare threshold: >=720 1h bars).
    daily_closes: List[float] = []
    if len(closes_1h) >= DAILY_REQUIRE_BARS:
        for j in range(0, len(closes_1h) - 23, 24):
            daily_closes.append(closes_1h[j + 23])
    rsi_1d: Optional[float] = None
    daily_uptrend = False
    if daily_closes:
        rsi_1d = ind.rsi_last(daily_closes[-60:], 14)
        e_now = _ema_val(daily_closes, 21)
        e_prev = _ema_val(daily_closes[:-1], 21) if len(daily_closes) >= 2 else None
        daily_uptrend = e_now is not None and e_prev is not None and e_now > e_prev

    # Volume ratio (rvol vs prior 20 1h bars).
    vols = [c.v for c in window_1h[-21:]]
    if len(vols) >= 21:
        vmean = sum(vols[:-1]) / 20.0
        vol_ratio = vols[-1] / vmean if vmean > 0 else 1.0
    else:
        vol_ratio = 1.0

    obv_dir = _obv_slope(window_1h)

    # Regime score (same computation as backtest_ab_compare._evaluate_entry).
    regime_score, regime_label = _regime_score(
        window_1h, closes_1h, e8, e21, atr_v, adx_v, obv_dir, bullish,
    )

    return {
        "bullish": bullish,
        "rsi_1h": ind.rsi_last(closes_1h, 14),
        "rsi_4h": rsi,
        "ext_atr": ext_atr,
        "adx": adx_v if adx_v is not None else 0.0,
        "rsi_1d": rsi_1d,
        "daily_uptrend": daily_uptrend,
        "vol_ratio": vol_ratio,
        "obv_dir": obv_dir,
        "regime_score": regime_score,
        "regime_label": regime_label,
    }


def _classify_dyn(ctx: Dict[str, Any]) -> Tuple[str, float]:
    """Classify a LONG signal under current DYN and under both proposed fixes.

    Returns (category, recoverable_size_mult).
      category ∈ {pass, block_late_long, block_overext, block_chop, none}
    recoverable_size_mult:
      0.0 = not recoverable
      0.5 = recoverable via exception path (half size)
      1.0 = already passes at full size
    """
    adx = ctx["adx"]
    rsi = ctx["rsi_4h"]
    ext = ctx["ext_atr"]

    # Mirror the long_thresh ladder from _evaluate_entry.
    if adx >= 40:
        long_thresh = 90.0
    elif adx >= 30:
        long_thresh = 80.0
    elif adx < 20:
        long_thresh = 70.0
    else:
        long_thresh = 75.0

    # Chop block (ADX<20) — not expected in this analysis but kept for honesty.
    if adx < 20:
        return "block_chop", 0.0

    if rsi > long_thresh:
        # Current code: hard block (resonance exception is dead due to rsi<=80 cap).
        return "block_late_long", 0.0

    if ext > 2.5:
        return "block_overext", 0.0

    return "pass", 1.0


def _fixed_recovery(ctx: Dict[str, Any]) -> Tuple[str, float]:
    """Return recovery verdict after BOTH fixes applied.

    Fix 1 (dead code): resonance RSI cap = min(90, long_thresh+10); allow half
                       size when rsi_1d<75 + daily uptrend + rsi<=cap +
                       vol_ratio>=1.2 + obv_dir>0.
    Fix 2 (overext)  : at ADX>=40 + rsi<70 + obv>0 + vol_ratio>=1.5, allow half.

    Returns (category, size_mult) where category is one of:
      pass_full, fix1_resonance_half, fix2_strongtrend_half, still_blocked
    """
    adx = ctx["adx"]
    rsi = ctx["rsi_4h"]
    ext = ctx["ext_atr"]
    rsi_1d = ctx["rsi_1d"]
    daily_up = ctx["daily_uptrend"]
    vol_ratio = ctx["vol_ratio"]
    obv_dir = ctx["obv_dir"]

    if adx >= 40:
        long_thresh = 90.0
    elif adx >= 30:
        long_thresh = 80.0
    elif adx < 20:
        long_thresh = 70.0
    else:
        long_thresh = 75.0

    # Fix 1: late-long recovery via resonance (dead code today).
    if rsi > long_thresh:
        cap = min(90.0, long_thresh + 10.0)
        resonance = (
            rsi_1d is not None and rsi_1d < 75.0 and daily_up
            and rsi <= cap and vol_ratio >= 1.2 and obv_dir > 0
        )
        if resonance:
            return "fix1_resonance_half", 0.5
        return "still_blocked", 0.0

    # Fix 2: overextended-long recovery in strong trend.
    if ext > 2.5:
        strong_trend = (
            adx >= 40 and rsi < 70 and obv_dir > 0 and vol_ratio >= 1.5
        )
        if strong_trend:
            return "fix2_strongtrend_half", 0.5
        return "still_blocked", 0.0

    return "pass_full", 1.0


def _classify_regime(ctx: Dict[str, Any]) -> Tuple[str, float, str]:
    """Classify a LONG signal under the data-driven REGIME regime score.

    Returns (category, size_mult, regime_label) where category is one of:
      regime_pass_full    - allowed at full size
      regime_pass_half    - allowed at half size (CHOP)
      regime_block_rsi    - blocked by RSI threshold
      regime_block_ext    - blocked by extension-over-ATR threshold
    """
    rsi = ctx["rsi_4h"]
    ext = ctx["ext_atr"]
    rp_name = ctx["regime_label"]
    rp = _REGIME_TABLE[rp_name]

    if rsi > rp.long_thresh:
        return "regime_block_rsi", 0.0, rp_name
    if ext > rp.ext_long_thresh:
        return "regime_block_ext", 0.0, rp_name
    return "regime_pass_full", rp.size_mult, rp_name


def _simulate_exit_pnl(
    candles: List[Any],
    entry_bar: int,
    entry_px: float,
    side: str,
    notional: float,
    *,
    max_loss: float = 2.5,
    protect: float = 1.5,
    retrace: float = 0.30,
) -> Tuple[float, str]:
    """Replay DSL exit; return (pnl_usd, reason)."""
    dsl = DSL(
        side=side, entry_px=entry_px, entry_bar=entry_bar, peak_px=entry_px,
        max_loss_pct=max_loss, protect_pct=protect, retrace_threshold=retrace,
    )
    fee = ROUND_TRIP_FEE_BPS / 10000.0
    for j in range(entry_bar, len(candles)):
        done, exit_px, reason = dsl.check_bar(j, candles[j])
        if done:
            gross = ((exit_px - entry_px) / entry_px if side == "long"
                     else (entry_px - exit_px) / entry_px)
            return notional * (gross - fee), reason
    # Last bar close fallback.
    last = candles[-1].c
    gross = ((last - entry_px) / entry_px if side == "long"
             else (entry_px - last) / entry_px)
    return notional * (gross - fee), "eod"


def analyze_coin(
    coin: str,
    candles_1h: List[Any],
    max_lev: int,
    cfg: Dict[str, Any],
    *,
    equity: float = 200.0,
    equity_fraction: float = 0.20,
    lev_ceiling: int = 12,
) -> Dict[str, Any]:
    candles_4h = _resample_4h(candles_1h)
    notional_full = equity * equity_fraction * min(lev_ceiling, max_lev)

    # Counters
    counts = {"pass": 0, "block_late_long": 0, "block_overext": 0, "block_chop": 0}
    pnl = {"pass": 0.0, "block_late_long": 0.0, "block_overext": 0.0}
    wins = {"pass": 0, "block_late_long": 0, "block_overext": 0}

    # Recovery stats (after both fixes)
    rec = {
        "fix1_resonance_half": {"n": 0, "pnl": 0.0, "wins": 0},
        "fix2_strongtrend_half": {"n": 0, "pnl": 0.0, "wins": 0},
        "still_blocked": {"n": 0, "pnl": 0.0},
        "pass_full": {"n": 0, "pnl": 0.0, "wins": 0},
    }

    # REGIME stats (data-driven regime score classification).
    regime_counts = {
        "regime_pass_full": 0, "regime_pass_half": 0,
        "regime_block_rsi": 0, "regime_block_ext": 0,
    }
    regime_pnl = {
        "regime_pass_full": 0.0, "regime_pass_half": 0.0,
        "regime_block_rsi": 0.0, "regime_block_ext": 0.0,
    }
    regime_wins = {
        "regime_pass_full": 0, "regime_pass_half": 0,
        "regime_block_rsi": 0, "regime_block_ext": 0,
    }
    # Per-regime-label breakdown.
    regime_label_dist: Dict[str, int] = {}

    blocked_examples: List[Dict[str, Any]] = []

    # Track how many analyzed bars actually had 1d resonance data available.
    daily_data_available = 0

    # Diagnostics: RSI distribution + resonance-condition pass rates.
    diag = {
        "rsi_buckets": {"75-80": 0, "80-85": 0, "85-90": 0, "90+": 0},
        "blocked_rsi_buckets": {"75-80": 0, "80-85": 0, "85-90": 0, "90+": 0},
        "late_long_diag": {
            "n": 0, "rsi_1d_ok": 0, "daily_up_ok": 0,
            "vol_ok": 0, "obv_ok": 0, "all_except_rsi_cap": 0,
        },
        "overext_diag": {
            "n": 0, "rsi_lt_70": 0, "vol_1_5": 0, "obv_ok": 0,
            "all_except_any": 0,
        },
    }

    def _rsi_bucket(r: float) -> str:
        if r >= 90:
            return "90+"
        if r >= 85:
            return "85-90"
        if r >= 80:
            return "80-85"
        return "75-80"

    # Only analyze the most recent DAYS*24 bars; earlier bars are warm-up/context.
    analysis_start = max(WARMUP, len(candles_1h) - DAYS * 24)
    for i in range(analysis_start, len(candles_1h) - 1):
        window_1h = candles_1h[: i + 1]
        t_now = candles_1h[i].t
        window_4h = [c for c in candles_4h if c.t <= t_now]
        if len(window_4h) < 20:
            window_4h = window_1h

        # OLD verdict: would the trade have fired at all?
        verdict_old, _, _, _, _, _, _ = _evaluate_entry(
            window_1h, window_4h, cfg, use_new_rules=False,
        )
        if verdict_old != "LONG":
            continue

        ctx = _dyn_context(window_1h)
        if ctx["rsi_1d"] is not None:
            daily_data_available += 1
        # Use 4h RSI (matching _evaluate_entry).
        ctx["rsi_4h"] = ind.rsi_last([c.c for c in window_4h], 14) or ctx.get("rsi_1h", 50.0)

        # Focus zone: ADX>=40 and RSI in 75-90 (plus >90 for dead-code check).
        if ctx["adx"] < 40:
            continue
        if ctx["rsi_4h"] < 75:
            continue

        category, _ = _classify_dyn(ctx)
        counts[category] = counts.get(category, 0) + 1

        # Diagnostics: RSI bucket.
        bk = _rsi_bucket(ctx["rsi_4h"])
        diag["rsi_buckets"][bk] += 1
        if category.startswith("block"):
            diag["blocked_rsi_buckets"][bk] += 1

        # Resonance-condition pass rates for blocked late-long.
        if category == "block_late_long":
            ld = diag["late_long_diag"]
            ld["n"] += 1
            c_rsi1d = ctx["rsi_1d"] is not None and ctx["rsi_1d"] < 75.0
            c_up = ctx["daily_uptrend"]
            c_vol = ctx["vol_ratio"] >= 1.2
            c_obv = ctx["obv_dir"] > 0
            if c_rsi1d:
                ld["rsi_1d_ok"] += 1
            if c_up:
                ld["daily_up_ok"] += 1
            if c_vol:
                ld["vol_ok"] += 1
            if c_obv:
                ld["obv_ok"] += 1
            # How many pass ALL resonance conditions except the (broken) rsi<=80 cap?
            if c_rsi1d and c_up and c_vol and c_obv:
                ld["all_except_rsi_cap"] += 1

        if category == "block_overext":
            od = diag["overext_diag"]
            od["n"] += 1
            if ctx["rsi_4h"] < 70:
                od["rsi_lt_70"] += 1
            if ctx["vol_ratio"] >= 1.5:
                od["vol_1_5"] += 1
            if ctx["obv_dir"] > 0:
                od["obv_ok"] += 1
            if ctx["adx"] >= 40 and ctx["rsi_4h"] < 70 and ctx["obv_dir"] > 0 \
                    and ctx["vol_ratio"] >= 1.5:
                od["all_except_any"] += 1

        # Simulate PnL at full notional (counterfactual: what if DYN had allowed).
        next_bar = candles_1h[i + 1]
        p, reason = _simulate_exit_pnl(
            candles_1h, i + 1, next_bar.o, "long", notional_full,
        )
        if category in pnl:
            pnl[category] += p
            if p > 0:
                wins[category] += 1

        # Recovery classification.
        rec_cat, size_mult = _fixed_recovery(ctx)
        rec[rec_cat]["n"] += 1
        if rec_cat in ("fix1_resonance_half", "fix2_strongtrend_half"):
            p_rec, _ = _simulate_exit_pnl(
                candles_1h, i + 1, next_bar.o, "long",
                notional_full * size_mult,
            )
            rec[rec_cat]["pnl"] += p_rec
            if p_rec > 0:
                rec[rec_cat]["wins"] += 1
            if len(blocked_examples) < 8 and category.startswith("block"):
                blocked_examples.append({
                    "bar": i, "rsi": ctx["rsi_4h"], "ext": ctx["ext_atr"],
                    "adx": ctx["adx"], "vol": ctx["vol_ratio"],
                    "obv": ctx["obv_dir"], "rsi_1d": ctx["rsi_1d"],
                    "daily_up": ctx["daily_uptrend"],
                    "category": category, "recovery": rec_cat,
                    "pnl_full": p, "pnl_rec": p_rec,
                })
        elif rec_cat == "pass_full":
            rec[rec_cat]["pnl"] += p
            if p > 0:
                rec[rec_cat]["wins"] += 1
        else:  # still_blocked
            rec[rec_cat]["pnl"] += 0.0  # no position

        # --- REGIME classification ---
        reg_cat, reg_size, reg_label = _classify_regime(ctx)
        regime_counts[reg_cat] += 1
        regime_label_dist[reg_label] = regime_label_dist.get(reg_label, 0) + 1

        if reg_cat in ("regime_pass_full", "regime_pass_half"):
            p_reg, _ = _simulate_exit_pnl(
                candles_1h, i + 1, next_bar.o, "long",
                notional_full * reg_size,
            )
            regime_pnl[reg_cat] += p_reg
            if p_reg > 0:
                regime_wins[reg_cat] += 1
        else:
            regime_pnl[reg_cat] += 0.0  # blocked

    return {
        "coin": coin,
        "counts": counts,
        "pnl": pnl,
        "wins": wins,
        "recovery": rec,
        "regime_counts": regime_counts,
        "regime_pnl": regime_pnl,
        "regime_wins": regime_wins,
        "regime_label_dist": regime_label_dist,
        "examples": blocked_examples,
        "diag": diag,
        "notional_full": notional_full,
        "daily_data_available": daily_data_available,
        "analyzed_bars": len(candles_1h) - 1 - analysis_start,
    }


def _print_coin(r: Dict[str, Any]) -> None:
    coin = r["coin"]
    c = r["counts"]
    p = r["pnl"]
    w = r["wins"]
    rec = r["recovery"]

    print(f"\n  --- {coin} (full notional ${r['notional_full']:.0f}) ---")
    print(f"  Analyzed bars: {r['analyzed_bars']} | "
          f"bars with 1d resonance data: {r['daily_data_available']} "
          f"({r['daily_data_available']/max(1,r['analyzed_bars'])*100:.0f}%)")
    print("  ADX>=40 + RSI>=75 LONG signal breakdown (current DYN):")
    for cat in ("pass", "block_late_long", "block_overext", "block_chop"):
        n = c.get(cat, 0)
        pnl_v = p.get(cat, 0.0)
        w_v = w.get(cat, 0)
        wr = w_v / n * 100 if n else 0
        print(f"    {cat:20s}: {n:4d}  trades | WR {wr:5.1f}% | "
              f"counterfactual PnL ${pnl_v:+8.2f}")

    print("  After BOTH fixes (dead-code + overext exception):")
    for key in ("pass_full", "fix1_resonance_half", "fix2_strongtrend_half",
                "still_blocked"):
        d = rec[key]
        n = d["n"]
        pnl_v = d.get("pnl", 0.0)
        w_v = d.get("wins", 0)
        wr = w_v / n * 100 if n else 0
        label = key.replace("_", " ")
        print(f"    {label:26s}: {n:4d}  trades | WR {wr:5.1f}% | "
              f"PnL ${pnl_v:+8.2f}")

    # --- REGIME results ---
    rc = r["regime_counts"]
    rp = r["regime_pnl"]
    rw = r["regime_wins"]
    rld = r["regime_label_dist"]
    total_sig = sum(rc.values()) or 1
    print("  REGIME (data-driven score):")
    for key in ("regime_pass_full", "regime_pass_half",
                "regime_block_rsi", "regime_block_ext"):
        n = rc.get(key, 0)
        pnl_v = rp.get(key, 0.0)
        w_v = rw.get(key, 0)
        wr = w_v / n * 100 if n else 0
        label = key.replace("regime_", "").replace("_", " ")
        print(f"    {label:26s}: {n:4d}  trades | WR {wr:5.1f}% | "
              f"PnL ${pnl_v:+8.2f}")
    total_pass = rc["regime_pass_full"] + rc["regime_pass_half"]
    total_pass_pnl = rp["regime_pass_full"] + rp["regime_pass_half"]
    print(f"    {'-- TOTAL PASSED':26s}: {total_pass:4d} trades "
          f"({total_pass/total_sig*100:.0f}%) | PnL ${total_pass_pnl:+8.2f}")
    print(f"    regime label distribution: {dict(rld)}")

    if r["examples"]:
        print(f"  Blocked signal examples (first {len(r['examples'])}):")
        for ex in r["examples"]:
            rsi1d_str = "  n/a" if ex["rsi_1d"] is None else f"{ex['rsi_1d']:5.1f}"
            print(f"    bar={ex['bar']:4d} RSI={ex['rsi']:5.1f} "
                  f"ext={ex['ext']:+5.2f} ADX={ex['adx']:4.1f} "
                  f"vol={ex['vol']:.2f} OBV={ex['obv']:+d} "
                  f"RSI1d={rsi1d_str} "
                  f"up={'Y' if ex['daily_up'] else 'N'} "
                  f"| {ex['category']} -> {ex['recovery']} "
                  f"| PnL full=${ex['pnl_full']:+6.2f} "
                  f"rec=${ex['pnl_rec']:+6.2f}")

    d = r["diag"]
    print("  Diagnostics:")
    print(f"    RSI distribution (all ADX>=40 + RSI>=75 LONG signals): "
          f"{d['rsi_buckets']}")
    print(f"    RSI distribution (blocked only):                   "
          f"{d['blocked_rsi_buckets']}")
    ld = d["late_long_diag"]
    if ld["n"]:
        print(f"    late_long resonance conditions (n={ld['n']}):")
        print(f"      rsi_1d<75   : {ld['rsi_1d_ok']:3d} "
              f"({ld['rsi_1d_ok']/ld['n']*100:.0f}%)")
        print(f"      daily up    : {ld['daily_up_ok']:3d} "
              f"({ld['daily_up_ok']/ld['n']*100:.0f}%)")
        print(f"      vol>=1.2    : {ld['vol_ok']:3d} "
              f"({ld['vol_ok']/ld['n']*100:.0f}%)")
        print(f"      obv>0       : {ld['obv_ok']:3d} "
              f"({ld['obv_ok']/ld['n']*100:.0f}%)")
        print(f"      ALL except rsi<=cap (would be rescued by fix1): "
              f"{ld['all_except_rsi_cap']:3d} "
              f"({ld['all_except_rsi_cap']/ld['n']*100:.0f}%)")
    od = d["overext_diag"]
    if od["n"]:
        print(f"    overext exception conditions (n={od['n']}):")
        print(f"      rsi<70      : {od['rsi_lt_70']:3d} "
              f"({od['rsi_lt_70']/od['n']*100:.0f}%)")
        print(f"      vol>=1.5    : {od['vol_1_5']:3d} "
              f"({od['vol_1_5']/od['n']*100:.0f}%)")
        print(f"      obv>0       : {od['obv_ok']:3d} "
              f"({od['obv_ok']/od['n']*100:.0f}%)")
        print(f"      ALL (would be rescued by fix2): "
              f"{od['all_except_any']:3d} "
              f"({od['all_except_any']/od['n']*100:.0f}%)")


def main() -> int:
    from hermes_trader.agents.config_store import cfg_get, read_agent_config

    cfg = get_config()
    live = read_agent_config()
    equity_fraction = float(live.get("equity_fraction_per_trade", 0.10))
    lev_ceiling = int(cfg_get("leverage", config=live))

    universe = get_universe()
    perps = [m for m in universe
             if m.get("type") == "perp" and not m["coin"].startswith("@")]
    by_coin = {m["coin"]: m for m in perps}

    total_bars = LOOKBACK_DAYS * 24 + 150

    print("=" * 90)
    print(f"  ATTRIBUTION: ADX>=40 + RSI 75-90 LONG signals — {DAYS} days, 1h")
    print(f"  Coins: {', '.join(TARGET_COINS)} | "
          f"equity_fraction={equity_fraction:.0%} lev={lev_ceiling}x")
    print("=" * 90)

    grand = {
        "counts": {}, "pnl": {}, "wins": {},
        "rec_pass": 0, "rec_pass_pnl": 0.0,
        "rec_fix1": 0, "rec_fix1_pnl": 0.0,
        "rec_fix2": 0, "rec_fix2_pnl": 0.0,
        "rec_still": 0, "rec_still_pnl": 0.0,
        "regime": {
            "regime_pass_full": {"n": 0, "pnl": 0.0, "wins": 0},
            "regime_pass_half": {"n": 0, "pnl": 0.0, "wins": 0},
            "regime_block_rsi": {"n": 0, "pnl": 0.0},
            "regime_block_ext": {"n": 0, "pnl": 0.0},
        },
        "regime_labels": {},
    }

    for coin in TARGET_COINS:
        m = by_coin.get(coin)
        if not m:
            print(f"  {coin}: not found in universe, skipping")
            continue
        max_lev = int(m.get("maxLeverage", 5))
        try:
            candles = fetch_hl_candles(coin, "1h", total_bars)
        except Exception as e:
            print(f"  {coin}: fetch error: {e}")
            continue
        if len(candles) < 150:
            print(f"  {coin}: only {len(candles)} bars, skipping")
            continue

        r = analyze_coin(
            coin, candles, max_lev, cfg,
            equity_fraction=equity_fraction, lev_ceiling=lev_ceiling,
        )
        _print_coin(r)

        for cat in ("pass", "block_late_long", "block_overext", "block_chop"):
            grand["counts"][cat] = grand["counts"].get(cat, 0) + r["counts"].get(cat, 0)
            grand["pnl"][cat] = grand["pnl"].get(cat, 0.0) + r["pnl"].get(cat, 0.0)
            grand["wins"][cat] = grand["wins"].get(cat, 0) + r["wins"].get(cat, 0)

        grand["rec_pass"] += r["recovery"]["pass_full"]["n"]
        grand["rec_pass_pnl"] += r["recovery"]["pass_full"]["pnl"]
        grand["rec_fix1"] += r["recovery"]["fix1_resonance_half"]["n"]
        grand["rec_fix1_pnl"] += r["recovery"]["fix1_resonance_half"]["pnl"]
        grand["rec_fix2"] += r["recovery"]["fix2_strongtrend_half"]["n"]
        grand["rec_fix2_pnl"] += r["recovery"]["fix2_strongtrend_half"]["pnl"]
        grand["rec_still"] += r["recovery"]["still_blocked"]["n"]
        grand["rec_still_pnl"] += r["recovery"]["still_blocked"]["pnl"]

        for key in ("regime_pass_full", "regime_pass_half",
                    "regime_block_rsi", "regime_block_ext"):
            grand["regime"][key]["n"] += r["regime_counts"].get(key, 0)
            grand["regime"][key]["pnl"] += r["regime_pnl"].get(key, 0.0)
            if "wins" in grand["regime"][key]:
                grand["regime"][key]["wins"] += r["regime_wins"].get(key, 0)
        for lbl, cnt in r["regime_label_dist"].items():
            grand["regime_labels"][lbl] = grand["regime_labels"].get(lbl, 0) + cnt

    print("\n" + "=" * 90)
    print("  GRAND TOTAL (3 coins)")
    print("=" * 90)
    print("  Current DYN classification:")
    for cat in ("pass", "block_late_long", "block_overext", "block_chop"):
        n = grand["counts"].get(cat, 0)
        pnl_v = grand["pnl"].get(cat, 0.0)
        w_v = grand["wins"].get(cat, 0)
        wr = w_v / n * 100 if n else 0
        print(f"    {cat:20s}: {n:4d} trades | WR {wr:5.1f}% | "
              f"counterfactual PnL ${pnl_v:+8.2f}")

    print("  After both fixes:")
    print(f"    pass_full              : {grand['rec_pass']:4d} trades | "
          f"PnL ${grand['rec_pass_pnl']:+8.2f}")
    print(f"    fix1 resonance (0.5x)  : {grand['rec_fix1']:4d} trades | "
          f"PnL ${grand['rec_fix1_pnl']:+8.2f}  "
          f"<-- dead-code fix recovery")
    print(f"    fix2 strongtrend (0.5x): {grand['rec_fix2']:4d} trades | "
          f"PnL ${grand['rec_fix2_pnl']:+8.2f}  "
          f"<-- overext exception recovery")
    print(f"    still_blocked          : {grand['rec_still']:4d} trades | "
          f"PnL ${grand['rec_still_pnl']:+8.2f}")

    total_recovered = grand["rec_fix1_pnl"] + grand["rec_fix2_pnl"]
    print(f"\n  >> Total recoverable PnL from both fixes "
          f"(ADX>=40 + RSI>=75 zone, 3 coins): ${total_recovered:+8.2f}")
    print(f"     - Fix 1 (dead code)   : ${grand['rec_fix1_pnl']:+8.2f} "
          f"from {grand['rec_fix1']} trades")
    print(f"     - Fix 2 (overext exc.) : ${grand['rec_fix2_pnl']:+8.2f} "
          f"from {grand['rec_fix2']} trades")

    # --- REGIME grand total ---
    gr = grand["regime"]
    total_r_pass = gr["regime_pass_full"]["n"] + gr["regime_pass_half"]["n"]
    total_r_pnl = gr["regime_pass_full"]["pnl"] + gr["regime_pass_half"]["pnl"]
    total_r_block = gr["regime_block_rsi"]["n"] + gr["regime_block_ext"]["n"]
    total_r_sig = total_r_pass + total_r_block

    print("\n  REGIME (data-driven score) — grand total:")
    for key in ("regime_pass_full", "regime_pass_half",
                "regime_block_rsi", "regime_block_ext"):
        d = gr[key]
        n = d["n"]
        pnl_v = d["pnl"]
        w_v = d.get("wins", 0)
        wr = w_v / n * 100 if n else 0
        label = key.replace("regime_", "").replace("_", " ")
        print(f"    {label:26s}: {n:4d} trades | WR {wr:5.1f}% | "
              f"PnL ${pnl_v:+8.2f}")
    print(f"    {'-- TOTAL PASSED':26s}: {total_r_pass:4d} trades "
          f"({total_r_pass/max(1,total_r_sig)*100:.0f}% of {total_r_sig}) | "
          f"PnL ${total_r_pnl:+8.2f}")
    print(f"    regime label distribution: {grand['regime_labels']}")

    dyn_passed_pnl = grand["pnl"].get("pass", 0.0)
    dyn_blocked = grand["counts"].get("block_late_long", 0) + grand["counts"].get("block_overext", 0)
    print("\n  COMPARISON (ADX>=40 + RSI>=75 zone, 3 coins, 21 days):")
    print(f"    DYN     : passed {grand['counts'].get('pass',0)} trades, "
          f"blocked {dyn_blocked}, passed-PnL ${dyn_passed_pnl:+8.2f}")
    print(f"    FIXED   : rescued {grand['rec_fix1']+grand['rec_fix2']} trades, "
          f"recovered PnL ${total_recovered:+8.2f}")
    print(f"    REGIME  : passed {total_r_pass} trades, "
          f"blocked {total_r_block}, passed-PnL ${total_r_pnl:+8.2f}")
    print("=" * 90)
    return 0


if __name__ == "__main__":
    sys.exit(main())
