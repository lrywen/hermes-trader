"""Auto-executor: validates through risk gates, sizes the trade, executes LIVE.

Integrates the DSL exit engine for two-phase trailing stops
(loss protection -> profit locking).
"""

from __future__ import annotations

import logging
import json
import math
import os
import re
import time
import uuid
from typing import Any, Dict, List

from hermes_trader.agents.config_store import read_agent_config, cfg_get, apply_coin_override
from hermes_trader.agents.dsl_exit import (
    ExitPolicy,
    RetraceTier,
    active_position_coins,
    check_all_positions,
    deregister_position,
    get_tracker,
    register_position,
    set_bracket,
)
from hermes_trader.agents.market_regime import REGIME_WEIGHTS
from hermes_trader.agents.memory import memory
from hermes_trader.agents.risk_gates import GateContext, eval_all_gates
from hermes_trader.client.exchange import (
    HL_LEVERAGE,
    MIN_ORDER_USD,
    cancel_open_orders_for_coin,
    entry_size_for_notional,
    get_hl_atr,
    get_hl_price,
    get_max_leverage,
    get_orderbook_spread,
    min_entry_notional_usd,
    modify_sl_trigger,
    place_hl_order,
    place_hl_trigger_order,
    set_leverage,
)
from hermes_trader.client.hl_client import fetch_account_state, resolve_user_address

logger = logging.getLogger(__name__)

# Backup server-side stop multiplier. RETUNED 2026-06-02 (microscope audit): was
# 3.5 -> ~5.5% spot on median names, far too wide to catch anything. The data showed
# 54% of max_loss exits GAP PAST the 1.2% DSL cap (median realized -1.56%, worst -3.6%)
# because the DSL loop only checks every 60s. A tighter server-side backup fires
# INSTANTLY at the exchange between our scans, catching the gap cluster. 1.5x ATR sits
# ~2.4% on median names (above the 1.2% DSL so DSL still fires first on normal exits,
# but tight enough to cap the gap-throughs that were the asymmetry killer). Config-tunable.
_DEFAULT_SL_ATR_MULT = 1.5
_DEFAULT_SL_CEILING_PCT = 3.0
TP_ATR_MULT = 1.0

# Hyperliquid perp taker fee, in PERCENT (HL = 2.5 bps = 0.025%). Used to model
# round-trip entry+exit cost in realized-PnL bookkeeping. Env-overridable so a
# future fee change doesn't require a code edit; 2 round-trip fills modeled.
_HL_TAKER_FEE_PCT = float(os.environ.get("HERMES_TAKER_FEE_PCT", "0.025"))
_HL_ROUND_TRIP_FILLS = 2

# Pending SL retry queue — positions whose server-side SL failed twice
# and need aggressive retry at sub-60s intervals.
_pending_sl_retries: Dict[str, Dict[str, Any]] = {}


# ── Pullback-long shadow audit (Suggestion A 48h grayscale) ────────────────
# When runner_entry_gate.pullback_long.shadow_mode is enabled, every signal
# that WOULD be admitted by the pullback-long bypass is appended here as a
# JSONL record instead of being traded. A post-run reconciliation script
# joins entry_px against subsequent candles to compute what the PnL would
# have been, so the bypass can be evaluated on real data before going live.
_PULLBACK_SHADOW_FILE = os.environ.get(
    "HERMES_PULLBACK_SHADOW_FILE",
    os.path.expanduser("~/.hermes-trading/pullback_shadow.jsonl"),
)


def _record_pullback_shadow(*, coin: str, side: str, score: float,
                            conf: float, slow_count: int,
                            rsi4h: Any, extension_atr: Any,
                            entry_px: float, trace_id: str = "") -> None:
    """Best-effort append a pullback-long shadow signal to the audit JSONL."""
    from datetime import datetime, timezone
    rec = {
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "trace_id": trace_id or "",
        "coin": coin,
        "side": side,
        "entry_px": float(entry_px or 0.0),
        "composite_score": float(score),
        "confidence": float(conf),
        "slow_burn_count": int(slow_count),
        "rsi4h": (float(rsi4h) if rsi4h is not None else None),
        "extension_atr": (float(extension_atr) if extension_atr is not None else None),
        "outcome": None,  # filled by reconciliation script
        "exit_px": None,
        "pnl_usd": None,
    }
    try:
        parent = os.path.dirname(_PULLBACK_SHADOW_FILE)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(_PULLBACK_SHADOW_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        logger.info(f"[executor] pullback-long SHADOW recorded for {coin} "
                    f"-> {_PULLBACK_SHADOW_FILE}")
    except OSError as e:
        logger.warning(f"[executor] pullback shadow write failed: {e}")

# ── Dynamic exchange-SL mover (Phase 2 trailing coordination) ───────────────
# When the DSL floor ratchets tighter in Phase 2, move the exchange backup SL
# to trail just behind the floor so a server-side stop can still cap gap-
# throughs without firing BEFORE the (15s-polled) DSL. Throttled per coin to
# avoid spamming batchModify on every tick and to respect HL rate limits.
_SL_MOVE_MIN_INTERVAL_SEC = 30.0
# Floor must move at least this many bps (relative to entry) before we issue a
# modify — filters micro-ratchets that aren't worth a cancel+replace.
_SL_MOVE_MIN_BPS = 15.0
# Backup SL sits this many bps BEHIND the DSL floor (long: below floor; short:
# above floor) so DSL is the primary trigger and the exchange order is the net.
_SL_BUFFER_BPS = 10.0
# Last modification attempt per coin: {coin: (timestamp, target_px)}.
_sl_move_state: Dict[str, tuple] = {}

# Static fallback 24h volumes, used ONLY when the live universe lookup fails.
# WIRING FIX 2026-06-11: these constants used to be the ONLY volume source for
# the liquidity gates — every non-major coin read $10M, so min_short_volume_usd
# (50M) blocked ALL non-major shorts (including the measured short winners) and
# min_market_volume_usd never blocked anything. Real dayNtlVlm now feeds the
# gates; this map is the degraded-read fallback.
_MAJOR_VOLUMES = {
    "BTC": 1e8, "ETH": 1e8, "SOL": 1e8, "BNB": 1e8,
    "XRP": 1e8, "DOGE": 1e8, "ADA": 1e8, "AVAX": 1e8,
}


def _get_market_volume_24h(coin: str) -> float:
    """Real 24h notional volume from the (disk-cached) universe; static fallback."""
    try:
        from hermes_trader.client.universe import get_universe
        for m in get_universe(include_hip3=(":" in coin)):
            if m.get("coin") == coin:
                vol = float(m.get("dayNtlVlm", 0) or 0)
                if vol > 0:
                    return vol
                break
    except Exception as e:
        logger.warning(f"[executor] live volume lookup failed for {coin}: {e} — using static fallback")
    return _MAJOR_VOLUMES.get(coin, 1e7)


# ---------------------------------------------------------------------------
# Position sizing — conviction tier model (replaces the legacy half-Kelly
# formula that predated the tiered confidence bands). Kelly sizing is dead
# code in production: sizing is driven by conviction_sizing tiers in
# .agent-config.json. The half-Kelly helper and its unit test were removed
# together to avoid giving future maintainers the impression it is live.
# ---------------------------------------------------------------------------


# Conviction sizing: scale the per-trade equity fraction by AI confidence so
# high-conviction setups bet bigger. The tiers are configurable via the
# `conviction_tiers` config key; these are the defaults if it's unset.
_DEFAULT_CONVICTION_TIERS = [(0.80, 1.5), (0.65, 1.0), (0.0, 0.7)]


def _parse_conviction_tiers(raw: Any) -> List[tuple]:
    """Parse `conviction_tiers` config into descending (threshold, mult) pairs.

    Accepts a list of [min_confidence, size_multiplier] pairs. Falls back to the
    defaults on any malformed input — this runs in the live trade path and must
    never raise. Drops non-positive multipliers; sorts highest-threshold-first
    so the multiplier lookup picks the best tier the confidence clears."""
    if not raw:
        return _DEFAULT_CONVICTION_TIERS
    try:
        tiers = [(float(t[0]), float(t[1])) for t in raw if float(t[1]) > 0]
    except (TypeError, ValueError, IndexError):
        return _DEFAULT_CONVICTION_TIERS
    if not tiers:
        return _DEFAULT_CONVICTION_TIERS
    tiers.sort(key=lambda t: t[0], reverse=True)
    return tiers


def _conviction_multiplier(confidence: float, tiers: List[tuple]) -> float:
    """First tier (descending) whose threshold `confidence` meets wins. Below
    every threshold → the lowest tier's multiplier."""
    for threshold, mult in tiers:
        if confidence >= threshold:
            return mult
    return tiers[-1][1]


def select_exit_params(dsl_config: Dict[str, Any], regime: str) -> tuple:
    """Regime-aware exit selection. The base dsl_config is the SCALP config
    (bank fast — +EV in chop/down per the controlled backtest: scalp +$1536/63%
    vs trend-ride -$757/47%). When regime is directional ('up'/'down') and
    regime_aware is enabled, LOOSEN to trend-ride protect/retrace so we RIDE the
    rippers, AND widen the hard stop (Plan C: 0.8% spot / ROE 10% vs the tight
    0.4% / 5% in chop/neutral) so trending positions aren't shaken out by 1h
    noise before the trailing protect kicks in. 'neutral'/'chop' keep scalp
    params and tight stop.
    Returns (protect_pct, retrace_threshold, phase2_tiers_raw,
             max_loss_pct, max_loss_roe_pct, label)."""
    base_protect = float(dsl_config.get("protect_pct", cfg_get("dsl_exit.protect_pct")))
    base_retrace = float(dsl_config.get("retrace_threshold", cfg_get("dsl_exit.retrace_threshold")))
    base_tiers = dsl_config.get("phase2_tiers")
    base_max_loss = float(dsl_config.get("max_loss_pct", cfg_get("dsl_exit.max_loss_pct")))
    base_max_loss_roe = float(dsl_config.get("max_loss_roe_pct", cfg_get("dsl_exit.max_loss_roe_pct")))

    ra = dsl_config.get("regime_aware") or {}
    if ra.get("enabled", False) and regime in ("up", "down"):
        tr = ra.get("trend_ride") or {}
        ml = ra.get("max_loss") or {}
        trend_ml = ml.get("trend") or {}
        return (float(tr.get("protect_pct", 3.0)),
                float(tr.get("retrace_threshold", 0.55)),
                tr.get("phase2_tiers", base_tiers),
                float(trend_ml.get("max_loss_pct", 0.8)),
                float(trend_ml.get("max_loss_roe_pct", 10.0)),
                f"trend_ride({regime}-regime)")
    # Non-trend (neutral/chop) or disabled: scalp exit; optional non_trend
    # max-loss override falls back to the top-level dsl_exit defaults.
    ml = ra.get("max_loss") or {}
    nt_ml = ml.get("non_trend") or {}
    return (base_protect, base_retrace, base_tiers,
            float(nt_ml.get("max_loss_pct", base_max_loss)),
            float(nt_ml.get("max_loss_roe_pct", base_max_loss_roe)),
            "scalp")


# Plan B regime-strength score uses the same 5-component weights as
# market_regime.REGIME_WEIGHTS (byte-aligned with
# scripts/backtest_ab_compare._regime_score). Imported above — do not
# duplicate here. Production's detect_regime() only yields the 4-state
# up/down/neutral/chop (no strength component), so the 5-component score
# splits mid-strength TREND (Plan B: size x0.5) from STRONG_TREND (full
# size — the core profit driver). We recompute it from the 1h indicator
# snapshot that research() already gathered (no extra fetch).


def regime_strength_label(analysis: Dict[str, Any]) -> str:
    """Classify the coin's OWN 1h tape into STRONG_TREND / TREND / NEUTRAL /
    CHOP using the backtest's 5-component weighted score.

    Direction is read from EMA8 vs EMA21 (the backtest reference) but the score
    itself is direction-agnostic — it measures how strongly price is trending
    whichever way EMA points. Returns "" when the 1h snapshot is too thin to
    score (caller should then not apply Plan B).
    """
    e8 = analysis.get("ema8_1h")
    e21 = analysis.get("ema21_1h")
    atr_v = analysis.get("atr1h")
    adx_v = analysis.get("adx1h")
    close = analysis.get("close1h")
    obv_dir = analysis.get("obv_slope_1h") or 0
    if e8 is None or e21 is None or atr_v is None or adx_v is None or not close:
        return ""
    try:
        e8 = float(e8); e21 = float(e21); atr_v = float(atr_v)
        adx_v = float(adx_v); close = float(close)
    except (TypeError, ValueError):
        return ""
    bullish = e8 > e21

    # ADX 15 -> 0, 45 -> 1.
    adx_c = max(0.0, min(1.0, (adx_v - 15.0) / 30.0))
    # ATR% 0.2% -> 0, 1.0% -> 1.
    atr_pct = atr_v / close * 100 if close else 0.0
    atr_c = max(0.0, min(1.0, (atr_pct - 0.2) / 0.8)) if atr_v else 0.0
    # |EMA8-EMA21| gap%: 0% -> 0, 0.5% -> 1.
    ema_c = 0.0
    if e21 > 0:
        gap_pct = abs(e8 - e21) / e21 * 100
        ema_c = max(0.0, min(1.0, gap_pct / 0.5))
    # Price vs EMA21 in ATR units: 0 -> 0, 2.0 ATR -> 1.
    ext_c = 0.0
    if atr_v > 0 and e21:
        ext = abs((close - e21) / atr_v)
        ext_c = max(0.0, min(1.0, ext / 2.0))
    # OBV: aligned with EMA direction = 1.0, flat = 0.3, opposing = 0.0.
    if (bullish and obv_dir > 0) or (not bullish and obv_dir < 0):
        obv_c = 1.0
    elif obv_dir == 0:
        obv_c = 0.3
    else:
        obv_c = 0.0

    w = REGIME_WEIGHTS
    score = max(0.0, min(1.0, (
        w["adx"] * adx_c + w["atr"] * atr_c + w["ema_align"] * ema_c
        + w["price_ext"] * ext_c + w["obv"] * obv_c
    )))
    if score >= cfg_get("strong_trend_threshold"):
        return "STRONG_TREND"
    if score >= cfg_get("trend_threshold"):
        return "TREND"
    if score >= cfg_get("neutral_threshold"):
        return "NEUTRAL"
    return "CHOP"


def plan_b_size_multiplier(analysis: Dict[str, Any],
                           plan_b_cfg: Dict[str, Any]) -> tuple:
    """Plan B: in a mid-strength TREND (not STRONG_TREND), RSI 40-60 has no
    directional edge (backtest attribution: these bars bleed equally long/short).
    Halve size to cut risk while keeping signal coverage.

    Returns (multiplier, reason). multiplier=1.0 means no reduction. Reads the
    4h RSI(14) that research() already computed (matches the backtest's RSI
    timeframe). Disabled / missing data → 1.0 (safe no-op).
    """
    if not bool(plan_b_cfg.get("enabled", False)):
        return 1.0, ""
    rsi4h = analysis.get("rsi4h")
    if rsi4h is None:
        return 1.0, ""
    try:
        rsi_val = float(rsi4h)
    except (TypeError, ValueError):
        return 1.0, ""
    rsi_lo = float(plan_b_cfg.get("rsi_low", 40.0))
    rsi_hi = float(plan_b_cfg.get("rsi_high", 60.0))
    if not (rsi_lo <= rsi_val < rsi_hi):
        return 1.0, ""
    label = regime_strength_label(analysis)
    if label != "TREND":
        return 1.0, ""
    mult = float(plan_b_cfg.get("size_mult", 0.5))
    return mult, (f"plan_b_trend_mid_rsi (regime=TREND, RSI4h={rsi_val:.1f} "
                  f"in [{rsi_lo:.0f},{rsi_hi:.0f}), size x{mult:.2f})")


def momentum_reentry_allowed(last_exit_px, last_side, current_mid, composite,
                             cfg: Dict[str, Any]) -> tuple:
    """Should we BYPASS the loss-cooldown because a stopped name has RESUMED its
    uptrend? (The autopsy leak: SPCX was force-entered, noise-stopped, then the
    180m loss-cooldown locked us out of its +29% run.) The cooldown is anti-revenge
    — correct for a FALLING name; but a name that breaks back ABOVE where it stopped
    us, with strong composite, is a momentum-continuation re-entry, not revenge.

    Conservative + whipsaw-guarded: requires price to reclaim `reclaim_pct`% ABOVE
    the prior stop-out price AND composite >= min_composite. LONG-only. Each
    re-entry that loses re-arms the cooldown at a NEW (higher) stop, so repeated
    whipsaw must clear an ever-rising bar. Returns (allow, reason)."""
    mr = cfg.get("momentum_reentry") or {}
    if not mr.get("enabled", False):
        return (False, "")
    try:
        last_exit_px = float(last_exit_px or 0)
        current_mid = float(current_mid or 0)
    except (TypeError, ValueError):
        return (False, "")
    if (last_side or "").lower() != "long" or last_exit_px <= 0 or current_mid <= 0:
        return (False, "")
    reclaim = float(mr.get("reclaim_pct", 1.0)) / 100.0
    min_comp = float(mr.get("min_composite", 30))
    if current_mid >= last_exit_px * (1 + reclaim) and float(composite or 0) >= min_comp:
        gain = (current_mid / last_exit_px - 1) * 100
        return (True, f"reclaimed +{gain:.1f}% above stop {last_exit_px:g}, "
                      f"composite {float(composite or 0):.0f}")
    return (False, "")


def maybe_execute(analysis: Dict[str, Any], _rotation_retry: bool = False) -> Dict[str, Any]:
    """Execute an analysis through risk gates and into the market.

    `_rotation_retry` is set on the single self-retry after capital rotation
    closed a weak position to free room — it blocks a second rotation so we can
    never loop.
    """
    config = read_agent_config()
    # Per-coin override: deep-merge coin_overrides[<coin>] so every downstream
    # gate/sizer/exit policy reads the coin-specific values transparently.
    # This is the single chokepoint for 币种配置 isolation.
    _coin = analysis.get("coin")
    config = apply_coin_override(config, _coin)
    mode = str(config.get("mode", "OFF")).upper()

    if mode == "OFF":
        return {
            "executed": False, "mode": mode,
            "analysis_id": analysis["id"], "reason": "mode_off",
        }
    # Per-coin enabled flag (set by the portal 币种配置 module). False here
    # disables trading for THIS coin only without changing the global mode.
    if config.get("enabled") is False:
        return {
            "executed": False, "mode": mode,
            "analysis_id": analysis["id"],
            "reason": f"coin_disabled ({_coin} disabled in 币种配置)",
        }
    shadow_mode = mode == "SHADOW"

    # Asset-class gate. Mirrors the perception-time filter so a stale
    # perception (e.g. one re-evaluated from memory after the operator
    # flips the flag) can't sneak through to a real trade. Crypto =
    # native HL coin (no colon); HIP-3 = colon-namespaced (`xyz:MU`).
    is_hip3 = ":" in (analysis.get("coin") or "")
    if is_hip3 and not bool(config.get("enable_hip3", False)):
        return {
            "executed": False, "mode": mode,
            "analysis_id": analysis["id"],
            "reason": "hip3_disabled (set enable_hip3=true to trade tokenized-equity perps)",
        }
    if (not is_hip3) and not bool(config.get("enable_crypto", True)):
        return {
            "executed": False, "mode": mode,
            "analysis_id": analysis["id"],
            "reason": "crypto_disabled (set enable_crypto=true to trade native HL perps)",
        }

    # Shadow-signals (free-signal suite): log what GEX / FINRA short-vol / whale /
    # news WOULD say about this candidate, to validate them forward before any is
    # allowed to gate entries. Fire-and-forget on a daemon thread so it can NEVER
    # add latency or amplify the execute hot path. Gated + hot-read reversible.
    _shadow_cfg = config.get("shadow_signals") or {}
    if bool(_shadow_cfg.get("enabled", False)):
        try:
            from hermes_trader.agents.shadow_signals import run_shadow_async
            run_shadow_async(analysis["coin"], analysis.get("side", "long"), _shadow_cfg)
        except Exception as _sh_e:
            logger.debug(f"[shadow-signals] dispatch failed (non-fatal): {_sh_e}")

    # AI zero-confidence guard.
    #
    # confidence=0 can mean two very different things:
    #   (1) AI is DOWN (empty response → ai_down=True), OR the response was
    #       unparseable prose with no extractable verdict at all (neither the
    #       JSON block nor the NLP fallback produced one).
    #       In both cases this PASS is an error code, not an opinion — block.
    #   (2) AI returned a valid but low-conviction opinion — either structured
    #       JSON ({"verdict":"PASS","confidence":0}) or prose that NLP extracted
    #       (HTA "neutral, low confidence"). This is a real verdict; structural
    #       override below should be allowed to upgrade strong momentum setups.
    #
    # We only block case (1). Case (2) flows through so the override logic and
    # confidence gates can make an informed decision.
    _ai_conf = float(analysis.get("confidence", 0) or 0)
    _verdict_raw = (analysis.get("verdict") or "").upper()
    _ai_down_flag = bool(analysis.get("ai_down", False))
    _nlp_parsed_flag = bool(analysis.get("nlp_parsed", False))
    _json_parsed_flag = bool(analysis.get("json_parsed", False))
    # Records written before these flags existed (and hand-built analysis dicts)
    # carry neither. Absence is not evidence of a parse failure, so fail OPEN
    # there and let the downstream gates judge — blocking on a missing key would
    # silently kill every legacy record.
    _parse_flags_present = ("json_parsed" in analysis) or ("nlp_parsed" in analysis)
    _unparseable = _parse_flags_present and not (_json_parsed_flag or _nlp_parsed_flag)
    if _ai_conf <= 0 and _verdict_raw in ("PASS", "LONG", "SHORT"):
        # Genuine failure: empty response OR text that yielded no verdict.
        #
        # ai_down + PASS is deliberately NOT blocked here: the dedicated
        # ai_verdict_pass branch below owns that case and reports a
        # more specific reason (and honours override_requires_ai=false). We
        # only pre-empt it for a directional verdict, which has no such branch.
        _defer_to_ai_down_branch = _ai_down_flag and _verdict_raw == "PASS"
        if (_ai_down_flag or _unparseable) and not _defer_to_ai_down_branch:
            _why = "ai_down (empty/failed response)" if _ai_down_flag \
                else "unparseable response (no JSON, no NLP verdict)"
            logger.warning(
                f"[executor] SKIP {analysis.get('coin')}: AI confidence=0 "
                f"({_why}; verdict={_verdict_raw}, "
                f"entry={analysis.get('entry')}, stop={analysis.get('stop')}, "
                f"tp={analysis.get('tp')})) — not executing with default params"
            )
            return {
                "executed": False, "mode": mode,
                "analysis_id": analysis["id"],
                "reason": f"ai_zero_confidence ({_why})",
            }
        # else: NLP extracted a real (low-conviction) opinion — fall through.

    # Structural-override: don't let a hedging AI PASS leave an objectively
    # strong accumulation setup on the table. Upgrade to LONG conf 0.70 and
    # let the gates do the real risk check. Two independent triggers, both
    # LONG-biased (we never force a SHORT):
    #   (a) composite >= 40 AND 2+ slow-burn 1h triggers fired, OR
    #   (b) a whale-accumulation signal fired (oi_funding_anomaly) —
    #       whale signals get their own override because smart-money loading
    #       (negative funding, flat price, high OI) is a high-conviction
    #       contrarian-to-retail setup we want to capitalize on even when the
    #       AI hedges and even against trend.
    # Live signal enforcement (Veto + Boost, 2026-06-16): consult our free signals
    # (GEX / FINRA short-vol / aggTrades whale / news) to gate the FORCED-OVERRIDE
    # path. CACHE-ONLY (never fetches here — the async shadow advisor above warms
    # the caches; cold cache => fail-open). BOOST lowers the override bar for a name
    # with a strong catalyst (breaking news / whale buying / crowded-short squeeze)
    # so we catch more rippers; VETO (applied below) blocks chop-traps / whales
    # dumping. Bounded: never bypasses the risk/regime/counter-trend/kill gates.
    _enf = None
    _base_override_composite = float(cfg_get("force_execute_composite", config=config))
    override_composite = _base_override_composite
    try:
        from hermes_trader.agents.shadow_signals import enforce_signals
        _enf = enforce_signals(analysis["coin"], "long", config)
        if _enf and _enf.boost:
            _delta = float((config.get("signal_enforcement") or {}).get("boost_bar_delta", 4))
            override_composite = max(0.0, _base_override_composite - _delta)
            logger.info(f"[executor] signal BOOST on {analysis['coin']}: "
                        f"override bar {_base_override_composite:.0f}→{override_composite:.0f} "
                        f"({_enf.boost_reason})")
    except Exception as _enf_e:
        logger.debug(f"[executor] signal enforcement failed (non-fatal): {_enf_e}")
    override_min_slow_burn = int(config.get("force_execute_slow_burn_count", 2))
    # whale_force_execute gates whether a whale signal alone can upgrade a PASS.
    whale_fired = bool(analysis.get("whale_signal")) and bool(config.get("whale_force_execute", False))
    slow_burn_strong = (
        bool(config.get("composite_force_execute", False))
        and
        float(analysis.get("composite_score", 0) or 0) >= override_composite
        and int(analysis.get("slow_burn_count", 0) or 0) >= override_min_slow_burn
    )
    # Breakout force-execute (O'Neil rule, added 2026-06-12): a 20-period-high
    # break WITH a volume spike and composite >= bar is an objectively strong
    # setup — the AI hedged these to PASS 21x on XPL while it ran +32% (38
    # researches, zero LONG verdicts, no gate ever blocked it). LONG path only:
    # forced shorts are the audit's worst bucket (AI shorts 0/8).
    # RETUNED same-day (forensic on own rule): XPL's composite never exceeded
    # 4.6 — the >=40 bar was DEAD for volume-surge setups (normalized composite
    # barely moves on 1-2 fired triggers), and `breakout` never co-fired at scan
    # times. XPL's actual signature: volumeSpike + uptrendMomentum + >=1 slow-burn
    # (volumeBuildup1h/higherLows1h). Qualify on that, with composite>=bar kept
    # as an alternative for true high-composite breaks.
    breakout_strong = (
        bool(config.get("breakout_force_execute", False))
        and bool(analysis.get("volume_spike_fired"))
        and (bool(analysis.get("breakout_fired"))
             or bool(analysis.get("uptrend_momentum_fired")))
        and (int(analysis.get("slow_burn_count", 0) or 0) >= 1
             or float(analysis.get("composite_score", 0) or 0) >= override_composite)
    )
    # Composite force-execute (validated 2026-06-15): a TA-CONFIRMED signal whose
    # composite clears the bar enters even on an AI PASS, REGARDLESS of the
    # breakout/whale pattern. Root cause it fixes: the AI lagged/vetoed real movers
    # the funnel had already confirmed (GRASS confirmed at the base → AI PASS 8h;
    # xyz equities WDC/BIRD at composite 34 → PASS, never entered). Replay across
    # the 64 PASS'd composite-30 names was net +121.8% spot / 64% win, duds capped
    # at the ROE stop (−1.85% @10x); xyz subset +17.4%. LONG-only; the market_regime
    # gate STILL blocks counter-trend longs (override conf < counter_regime bar), so
    # downtrend duds are filtered. Gated `composite_force_execute` (hot-read → revert
    # instantly to 40/off if a down regime floods duds).
    composite_strong = (
        bool(config.get("composite_force_execute", False))
        and float(analysis.get("composite_score", 0) or 0) >= override_composite
    )
    # TA sidestep. The slow-burn count is a HARD floor, not one of three
    # interchangeable alternatives: when these were OR'd, a lone
    # momentum_burst_fired satisfied the whole clause and
    # ta_sidestep_min_slow_burn_count was dead config (live 2026-08-20: 5/5
    # overrides fired at slow_burn_count=1 against min=2). Require the
    # accumulation base AND at least one confirmation, mirroring
    # breakout_strong's shape.
    ta_sidestep_strong = (
        bool(config.get("ta_sidestep_force_execute", False))
        and int(analysis.get("slow_burn_count", 0) or 0) >=
        int(config.get("ta_sidestep_min_slow_burn_count", 99) or 99)
        and (
            float(analysis.get("composite_score", 0) or 0) >= override_composite
            or bool(analysis.get("momentum_burst_fired"))
        )
    )
    override_strong = (
        slow_burn_strong or whale_fired or breakout_strong
        or composite_strong or ta_sidestep_strong
    )
    # A PASS verdict — whether from a hedged multi-agent debate (HTA) or from
    # a failed LLM call — is the AI's explicit "do not trade" signal. Upgrading
    # it to a blind LONG via structural/whale overrides means entering with no
    # AI conviction behind the entry AND no AI judgment behind the exit. Refuse
    # the upgrade unless the operator explicitly opts out via
    # override_requires_ai=false (reversible).
    _ai_down_block = bool(analysis.get("ai_down")) and \
        bool(config.get("override_requires_ai", True))
    if analysis.get("verdict") == "PASS" \
            and override_strong \
            and _ai_down_block:
        logger.info(
            f"[executor] Structural override SKIPPED on {analysis['coin']}: "
            f"AI verdict is PASS (ai_verdict_pass) — no blind upgrade"
        )
        return {
            "executed": False, "mode": mode,
            "analysis_id": analysis["id"],
            "reason": "ai_verdict_pass (AI returned PASS; structural override refused)",
        }
    # Signal VETO (Veto+Boost live, 2026-06-16): block a FORCED override LONG when
    # our free signals say it's a trap — xyz pinned in long-gamma against the call
    # wall (GEX pin-trap), or crypto with whales aggressively DUMPING (aggTrades).
    # CACHE-ONLY (computed above in `_enf`, no network here). The broader HIP-3
    # GEX entry veto for normal LONGs lives in _runner_entry_block_reason below.
    # Fully reversible via signal_enforcement.enabled / .veto (hot-read).
    # gex_signal.shadow_mode (if set) still downgrades the GEX veto to log-only.
    if analysis.get("verdict") == "PASS" \
            and override_strong \
            and _enf is not None and _enf.veto:
        _gex_shadow = ":" in analysis["coin"] and \
            bool((config.get("gex_signal") or {}).get("shadow_mode", False))
        if _gex_shadow:
            logger.info(f"[executor] signal VETO [GEX SHADOW — not blocked] on "
                        f"{analysis['coin']}: {_enf.veto_reason}")
            analysis = dict(analysis)
            analysis["signal_veto"] = _enf.veto_reason
        else:
            logger.info(f"[executor] signal VETO — forced override SKIPPED on "
                        f"{analysis['coin']}: {_enf.veto_reason}")
            return {
                "executed": False, "mode": mode,
                "analysis_id": analysis["id"],
                "reason": f"signal_veto ({_enf.veto_reason})",
            }

    if analysis.get("verdict") == "PASS" and override_strong:
        trigger = ("whale-accumulation" if whale_fired
                   else f"composite={analysis.get('composite_score'):.0f}+{analysis.get('slow_burn_count')} slow-burn"
                   if slow_burn_strong
                   else "breakout+volume (O'Neil)" if breakout_strong
                   else "TA sidestep" if ta_sidestep_strong
                   else f"composite={analysis.get('composite_score'):.0f}>={override_composite:.0f} (momentum force)")
        # Upgrade to the configured confidence floor (not a hardcoded 0.70) so a
        # structural/whale override still clears the confidence_gate after the bar
        # is raised. Otherwise raising min_ai_confidence would silently kill the
        # whale overrides — empirically the one flat-positive bucket.
        _conf_floor = float(config.get("min_ai_confidence", 0.70))
        _ai_conf_raw = float(analysis.get("confidence", 0) or 0)
        _ai_down = bool(analysis.get("ai_down", False))
        _nlp_parsed = bool(analysis.get("nlp_parsed", False))
        _reasoning = (analysis.get("reasoning", "") or "")[:200]
        _composite = float(analysis.get("composite_score", 0) or 0)
        _slow_count = int(analysis.get("slow_burn_count", 0) or 0)
        _fired = analysis.get("fired_triggers") or []
        # Detailed override decision audit — logs every input so a post-mortem
        # can reconstruct why a PASS was upgraded (composite, each slow-burn
        # trigger, and the five override booleans) and what confidence it landed
        # on. Kept on one logger.info call per block for grep-ability.
        logger.info(
            f"[override] {analysis['coin']} STRUCTURAL OVERRIDE FIRE — "
            f"trigger={trigger}"
        )
        logger.info(
            f"[override] {analysis['coin']} scoring — "
            f"composite={_composite:.1f} (bar={override_composite:.0f}), "
            f"slow_burn_count={_slow_count} (min={override_min_slow_burn}), "
            f"fired_triggers={_fired}"
        )
        logger.info(
            f"[override] {analysis['coin']} gates — "
            f"slow_burn_strong={slow_burn_strong}, whale_fired={whale_fired}, "
            f"breakout_strong={breakout_strong}, composite_strong={composite_strong}, "
            f"ta_sidestep_strong={ta_sidestep_strong}, override_strong={override_strong}"
        )
        logger.info(
            f"[override] {analysis['coin']} AI diagnostics — "
            f"verdict=PASS, raw_confidence={_ai_conf_raw:.3f}, "
            f"ai_down={_ai_down}, nlp_parsed={_nlp_parsed}, "
            f"reasoning={_reasoning!r}"
        )
        # Fetch order book spread/depth for forced-trade diagnostics
        _ob = get_orderbook_spread(analysis["coin"])
        if _ob.get("ok"):
            logger.info(
                f"[executor]   Order book — bid={_ob['best_bid']}, ask={_ob['best_ask']}, "
                f"spread={_ob['spread_pct']:.3f}%, "
                f"bid_depth_1%=${_ob['bid_depth_1pct_usd']:,.0f}, "
                f"ask_depth_1%=${_ob['ask_depth_1pct_usd']:,.0f}"
            )
        else:
            logger.warning(
                f"[executor]   Order book fetch failed: {_ob.get('error', 'unknown')}"
            )
        analysis = dict(analysis)
        analysis["verdict"] = "LONG"
        analysis["side"] = "long"
        # Preserve the model's own conviction before the floor masks it. The
        # runner gate judges on this raw value so a structural override can
        # still route/size as a LONG without laundering a 0.40 PASS into a
        # 0.70 that trivially clears min_confidence.
        analysis["ai_confidence_raw"] = _ai_conf_raw
        analysis["confidence"] = max(_conf_floor, float(analysis.get("confidence", 0) or 0))
        logger.info(
            f"[override] {analysis['coin']} UPGRADED PASS → LONG — "
            f"confidence {_ai_conf_raw:.3f} → {analysis['confidence']:.3f} "
            f"(floor={_conf_floor:.2f})"
        )
        if ta_sidestep_strong:
            analysis["sidestep_override"] = True
        analysis["reasoning"] = (
            "[structural override] " + (analysis.get("reasoning", "") or "")
        )[:500]

    # Safety guard: a PASS that did NOT qualify for the structural override must
    # never reach order placement (trade_side defaults to "long" downstream, so
    # an un-upgraded PASS would otherwise silently fire a long). route_verdict
    # only sends a PASS here when an override HINT applies — this is the real
    # check that no-ops cleanly when the override doesn't actually hold.
    if analysis.get("verdict") == "PASS":
        return {
            "executed": False, "mode": mode,
            "analysis_id": analysis["id"], "reason": "pass_no_override",
        }

    _runner_cfg = config.get("runner_entry_gate") or {}
    _sidestep_bypasses_runner = (
        bool(analysis.get("sidestep_override"))
        and bool(_runner_cfg.get("bypass_sidestep_overrides", False))
    )
    if not _sidestep_bypasses_runner:
        runner_block = _runner_entry_block_reason(analysis, config)
        if runner_block:
            return {
                "executed": False, "mode": mode,
                "analysis_id": analysis["id"], "reason": runner_block,
            }

    # Loss cooldown: refuse re-entry on a coin whose last close was a LOSS and
    # whose extended block hasn't expired (armed in close_position_market).
    _lc_remaining = memory.loss_cooldown_remaining_min(analysis["coin"])
    if _lc_remaining > 0:
        # Momentum-continuation re-entry: if the name has reclaimed above where it
        # stopped us (resumed uptrend, strong composite), bypass the anti-revenge
        # cooldown — that's a run we got shaken out of, not a falling knife.
        _last = memory.last_close_for(analysis["coin"]) or {}
        _mr_ok, _mr_why = momentum_reentry_allowed(
            _last.get("exit_px"), _last.get("side"),
            analysis.get("mid"), analysis.get("composite_score"), config)
        if _mr_ok:
            logger.info(f"[executor] momentum re-entry on {analysis['coin']}: "
                        f"{_mr_why} — bypassing {_lc_remaining:.0f}min loss cooldown")
        else:
            return {
                "executed": False, "mode": mode,
                "analysis_id": analysis["id"],
                "reason": (f"loss_cooldown ({analysis['coin']} closed at a loss recently — "
                           f"{_lc_remaining:.0f}min remaining)"),
            }

    # Idempotency: don't double-execute
    already = next(
        (t for t in memory.get_recent_trades(100)
         if t.get("analysis_id") == analysis["id"] and t.get("size_usd", 0) > 0),
        None,
    )
    if already:
        return {
            "executed": False, "mode": mode,
            "analysis_id": analysis["id"], "reason": "already_executed",
            "order_id": already.get("order_id"),
        }

    user = resolve_user_address()

    # HIP-3 dex-balance preflight: dexes are separate clearinghouses, so
    # refuse cleanly when the target dex truly has no funds. Distinguishes
    # "API returned $0" from "API call failed / returned no marginSummary" —
    # the latter is a transient lookup failure (the per-dex endpoint flakes
    # intermittently) and shouldn't be reported as "underfunded" when funds
    # are sitting on the dex. One retry, then back off rather than block
    # falsely with a wire-USDC-to-dex error.
    coin_for_dex_check = analysis["coin"]
    if ":" in coin_for_dex_check:
        dex_name = coin_for_dex_check.split(":", 1)[0]
        from hermes_trader.client.hl_client import _http_post

        def _read_dex_value() -> tuple[bool, float]:
            try:
                state_resp = _http_post("/info", {
                    "type": "clearinghouseState", "user": user, "dex": dex_name,
                })
            except Exception as e:
                logger.warning(f"[executor] HIP-3 dex query raised for {dex_name}: {e}")
                return (False, 0.0)
            ms = (state_resp or {}).get("marginSummary")
            if not ms:
                return (False, 0.0)  # No marginSummary → response missing/malformed
            return (True, float(ms.get("accountValue", 0) or 0))

        ok, dex_value = _read_dex_value()
        if not ok:
            time.sleep(0.3)
            ok, dex_value = _read_dex_value()

        if not ok:
            logger.warning(f"[executor] HIP-3 dex-balance lookup failed twice for {dex_name}; letting HL adjudicate")
            # Fall through and let HL reject if it has to — better than
            # falsely claiming the dex is empty when we couldn't verify.
        elif dex_value < 1.0:
            return {
                "executed": False, "mode": mode,
                "analysis_id": analysis["id"],
                "reason": (
                    f"hip3_dex_underfunded ({dex_name}: ${dex_value:.2f}). "
                    f"Transfer USDC to '{dex_name}' via the HL frontend."
                ),
            }

    # include_hip3=True so the concurrency + exposure gates COUNT every open
    # position, including tokenized-equity (xyz:) HIP-3 perps. The old main-only
    # fetch returned 0 positions / $0 notional whenever the book was all HIP-3,
    # so max_concurrent and equity_risk never capped it — the book ballooned
    # past max_concurrent (to ~22) and to ~17x notional. Sizing still uses the
    # MAIN-dex clearinghouse ("") equity/available below, so per-trade size is
    # unchanged; only the gate inputs are corrected to the aggregated book.
    # The per-dex clearinghouse endpoint flakes under burst load (several
    # executes in one cycle), returning $0 for the MAIN dex even when funds are
    # there — which used to spuriously block real trades with "equity_unavailable
    # (live account state returned 0)" while the account was healthy. Read once,
    # and on a $0 main-equity read retry up to twice before believing it. A
    # genuine $0 still refuses (never size an unsized order); a transient blip
    # recovers.
    # PER-DEX FIX 2026-06-12: each dex is a separate clearinghouse, so a HIP-3
    # trade must be sized and margin-checked against ITS OWN dex's equity and
    # available margin — not the main dex's. Before this, xyz:DRAM was blocked
    # with "available $0.00 / equity $39.90" (main dex) while the xyz dex held
    # $59.04 free: HIP-3 entries starved whenever main margin was committed,
    # and vice versa. Main-dex (crypto) trades behave exactly as before.
    _target_dex = analysis["coin"].split(":", 1)[0] if ":" in analysis["coin"] else ""

    def _read_state() -> tuple[dict, float, float]:
        st = fetch_account_state(user, include_hip3=True) or {}
        deq = st.get("dex_equity") or {}
        dav = st.get("dex_available") or {}
        if _target_dex:
            eq = float(deq.get(_target_dex, 0) or 0)
            av = float(dav.get(_target_dex, 0) or 0)
        else:
            eq = float(deq.get("", st.get("equity")) or 0)
            av = float(dav.get("", st.get("available")) or 0)
        return st, eq, av

    state, equity, available = _read_state()
    for _attempt in range(2):
        if equity > 0:
            break
        time.sleep(0.4)
        state, equity, available = _read_state()
    agg_equity = float(state.get("equity") or equity)                # aggregated → exposure gate
    total_open_notional = float(state.get("total_ntl") or 0)         # aggregated → notional gate
    if equity <= 0:
        # Persisted across retries — refuse rather than send an unsized order.
        return {
            "executed": False, "mode": mode,
            "analysis_id": analysis["id"],
            "reason": "equity_unavailable (live account state returned 0 after retries)",
        }

    # Free-margin floor: leave headroom for maintenance + slippage so HL
    # doesn't reject mid-pipeline with "Insufficient margin".
    min_avail_pct = float(config.get("min_available_margin_pct", 0.10))
    if equity > 0 and (available / equity) < min_avail_pct:
        return {
            "executed": False, "mode": mode,
            "analysis_id": analysis["id"],
            "reason": (f"insufficient_free_margin on dex '{_target_dex or 'main'}' "
                       f"(available ${available:.2f} / equity ${equity:.2f} = "
                       f"{100*available/equity:.1f}%, floor {100*min_avail_pct:.0f}%)"),
        }

    # Track daily PnL off the AGGREGATE equity (main + HIP-3), not main-dex-only
    # `equity` (which is kept main-only for margin sizing). Using main-only here
    # poisoned daily_pnl/peak vs the heartbeat's aggregate — it read ~$30 low and
    # spuriously fired the daily give-back breaker (saw day $24 vs true $54).
    memory.track_daily_pnl(agg_equity)
    daily_pnl = memory.get_daily_pnl()

    positions = [
        {
            "coin": p["position"]["coin"],
            "side": "long" if float(p["position"]["szi"]) > 0 else "short",
            "size_usd": abs(float(p["position"]["szi"])) * (analysis.get("entry_px") or 0),
        }
        for p in state["asset_positions"]
    ]

    # Restart-safe re-entry backstop: a flaky/empty live account read can drop a
    # held position from asset_positions, letting opposite_direction_guard fail
    # open and STACK the position (observed: xyz:SP500 pyramided to ~8x during a
    # restart's rehydration window). The DSL registry rehydrates from disk, so
    # merge any tracked coin the live read missed — a held position then blocks
    # re-entry even when the API momentarily forgets it. (Skipping a trade costs
    # $0; a silent pyramid does not.)
    _live_coins = {p["coin"] for p in positions}
    for _coin, _side in active_position_coins().items():
        if _coin not in _live_coins:
            logger.warning(
                f"[executor] {_coin} tracked by DSL but absent from live account "
                f"read — treating as held (re-entry backstop)")
            positions.append({"coin": _coin, "side": _side, "size_usd": 0})

    # `tp_px` / `stop_px` are fallbacks for bracket calculation when ATR
    # is unavailable; the executor uses a fresh live mid as entry.
    tp_px = analysis.get("tp_px")
    stop_px = analysis.get("stop_px")

    leverage = min(int(config.get("leverage", HL_LEVERAGE)),
                   get_max_leverage(analysis["coin"]))
    _notional_cap = float(config.get("max_trade_notional_usd", 0) or 0)
    _atr_sizing = config.get("atr_risk_sizing", {}) or {}
    _atr_sizing_enabled = bool(_atr_sizing.get("enabled", False))
    mid_price = 0.0
    atr = 0.0
    size_in_coin = 0.0

    if _atr_sizing_enabled:
        coin = analysis["coin"]
        mid_price = get_hl_price(coin)
        if mid_price <= 0:
            return {"executed": False, "mode": mode, "analysis_id": analysis["id"],
                    "reason": f"invalid_price_for_{coin}"}
        atr = get_hl_atr("4h", 14, coin)
        if atr <= 0:
            atr = get_hl_atr("4h", 14, coin)
        if atr <= 0:
            return {
                "executed": False, "mode": mode, "analysis_id": analysis["id"],
                "reason": f"no_atr_no_stop ({coin}: insufficient candle history to size a stop)",
            }

        from hermes_trader.agents.sizing import atr_equal_risk_notional
        _max_total_pct = float(config.get("max_total_notional_pct", 0) or 0)
        _room = (_max_total_pct * agg_equity - total_open_notional) if _max_total_pct > 0 else 0.0
        _cap = _notional_cap
        if _room > 0:
            _cap = min(_cap, _room) if _cap > 0 else _room
        _risk_pct = float(_atr_sizing.get("risk_per_trade_pct", 0.02))
        _sizing_basis = str(_atr_sizing.get("sizing_basis", "atr_stop") or "atr_stop").lower()
        if _sizing_basis in ("primary_stop", "dsl_stop"):
            _dsl = config.get("dsl_exit", {}) or {}
            _max_loss = float(_dsl.get("max_loss_pct", 0.4) or 0.4)
            _max_roe = float(_dsl.get("max_loss_roe_pct", 5.0) or 5.0)
            _lev = max(1, leverage)
            _stop_frac = min(_max_loss, _max_roe / _lev) / 100.0
            if agg_equity <= 0 or _risk_pct <= 0 or _stop_frac <= 0:
                return {
                    "executed": False, "mode": mode, "analysis_id": analysis["id"],
                    "reason": f"primary_stop_sizing_zero ({coin}: invalid inputs)",
                }
            trade_notional = (_risk_pct * agg_equity) / _stop_frac
            _lev_cap = min(get_max_leverage(coin), int(config.get("leverage", HL_LEVERAGE)))
            _max_by_lev = max(1, _lev_cap) * agg_equity
            _clamped = []
            if trade_notional > _max_by_lev:
                trade_notional = _max_by_lev
                _clamped.append("max_leverage")
            if _cap > 0 and trade_notional > _cap:
                trade_notional = _cap
                _clamped.append("notional_cap")
            logger.info(
                f"[executor] primary-stop equal-risk sizing {coin}: notional ${trade_notional:.0f} "
                f"(risk ${trade_notional*_stop_frac:.2f} @ {_stop_frac*100:.2f}% stop"
                f"{', clamped:'+','.join(_clamped) if _clamped else ''})")
        else:
            _sz = atr_equal_risk_notional(
                equity=agg_equity,
                risk_per_trade_pct=_risk_pct,
                atr_abs=atr,
                entry_px=mid_price,
                sl_atr_mult=float(config.get("sl_atr_mult", _DEFAULT_SL_ATR_MULT)),
                max_trade_notional_usd=_cap,
                coin_max_leverage=get_max_leverage(coin),
                config_max_leverage=int(config.get("leverage", HL_LEVERAGE)),
            )
            if _sz.notional_usd <= 0:
                return {
                    "executed": False, "mode": mode, "analysis_id": analysis["id"],
                    "reason": f"atr_sizing_zero ({coin}: {_sz.clamped_by or 'invalid inputs'})",
                }
            trade_notional = _sz.notional_usd
            logger.info(
                f"[executor] ATR equal-risk sizing {coin}: notional ${trade_notional:.0f} "
                f"(impl_lev {_sz.implied_leverage:.1f}x, risk ${_sz.risk_usd:.2f} @ "
                f"{_sz.stop_distance_frac*100:.2f}% stop"
                f"{', clamped:'+_sz.clamped_by if _sz.clamped_by else ''})")
    else:
        # Legacy fallback when ATR equal-risk sizing is explicitly disabled:
        # equity × fraction × leverage × optional conviction multiplier.
        base_fraction = float(config.get("equity_fraction_per_trade", 0.2))
        if bool(config.get("conviction_sizing", False)):
            conf = float(analysis.get("confidence", 0) or 0)
            tiers = _parse_conviction_tiers(config.get("conviction_tiers"))
            conviction_mult = _conviction_multiplier(conf, tiers)
            # Whale-signal boost: when smart-money accumulation is flagged on this
            # coin, bet bigger. Clamps so a whale + high-conf trade can't exceed 2× base.
            if analysis.get("whale_signal"):
                whale_mult = float(config.get("whale_size_multiplier", 1.0))
                conviction_mult = min(conviction_mult * whale_mult, 2.0)
        else:
            conviction_mult = 1.0
        equity_fraction = base_fraction * conviction_mult
        trade_notional = equity * equity_fraction * leverage
        # Clamp to the per-trade notional ceiling so an oversized conviction bet is
        # SIZED DOWN to the cap rather than REJECTED by the notional gate.
        if _notional_cap > 0 and trade_notional > _notional_cap:
            logger.info(f"[executor] notional ${trade_notional:.0f} > cap "
                        f"${_notional_cap:.0f} — clamping to cap")
            trade_notional = _notional_cap

    # Plan B: halve size in mid-strength TREND with RSI4h in [40, 60). The
    # backtest (backtest_ab_compare L613-618) shows this regime band loses on
    # both long/short because trend confirmation is weak; reducing notional cuts
    # the per-trade loss without touching STRONG_TREND (the core profit engine).
    _plan_b_cfg = config.get("plan_b") or {}
    _pb_mult, _pb_reason = plan_b_size_multiplier(analysis, _plan_b_cfg)
    if _pb_mult < 1.0:
        trade_notional *= _pb_mult
        logger.info(f"[executor] Plan B size reduction {analysis['coin']}: "
                    f"{_pb_reason}, notional -> ${trade_notional:.0f}")

    # Normalize to the exact HL-valid entry size BEFORE risk gates. The order
    # layer enforces a $10.50 minimum and coin-size precision; if we wait until
    # place_hl_order() to apply that, the gates, DSL tracker, memory, and SL/TP
    # brackets all believe a smaller position exists than the one actually sent.
    coin = analysis["coin"]
    if mid_price <= 0:
        mid_price = get_hl_price(coin)
        if mid_price <= 0:
            return {"executed": False, "mode": mode, "analysis_id": analysis["id"],
                    "reason": f"invalid_price_for_{coin}"}
    try:
        min_notional = min_entry_notional_usd(coin, mid_price)
        if min_notional > 0 and trade_notional < min_notional:
            # Minimum-order floor: ATR equal-risk sizing can produce a notional
            # below Hyperliquid's per-coin minimum (e.g. (0.02×$10)/0.02=$10
            # vs HL min ~$10.50). Instead of rejecting, BUMP UP to the exchange
            # minimum so the order is valid. This slightly increases risk on
            # tiny accounts; the gap is small (<10% in practice) and the max-loss
            # stop still caps downside. If the gap is absurd (>50%), reject to
            # avoid unintended oversized risk.
            bump_gap = (min_notional - trade_notional) / trade_notional if trade_notional > 0 else 999
            if bump_gap <= 0.50:
                logger.info(
                    f"[executor] {coin} notional ${trade_notional:.2f} below HL minimum "
                    f"${min_notional:.2f} — bumping up (+{bump_gap*100:.1f}% risk)"
                )
                trade_notional = min_notional
            else:
                return {
                    "executed": False, "mode": mode,
                    "analysis_id": analysis["id"],
                    "reason": (f"below_min_order_notional ({coin}: sized "
                               f"${trade_notional:.2f}, HL minimum after precision "
                               f"${min_notional:.2f}; gap {bump_gap*100:.0f}% > 50% — refusing auto-bump)"),
                }
        size_in_coin = entry_size_for_notional(coin, trade_notional, mid_price)
    except Exception as e:
        return {
            "executed": False, "mode": mode,
            "analysis_id": analysis["id"],
            "reason": f"entry_size_unavailable ({coin}: {e})",
        }
    if size_in_coin <= 0:
        return {
            "executed": False, "mode": mode,
            "analysis_id": analysis["id"],
            "reason": f"entry_size_zero ({coin})",
        }
    normalized_notional = size_in_coin * mid_price
    if abs(normalized_notional - trade_notional) >= 0.01:
        logger.info(
            f"[executor] normalized entry size {coin}: target ${trade_notional:.2f} "
            f"→ {size_in_coin:g} coin (${normalized_notional:.2f})")
    trade_notional = normalized_notional

    recent_trades = memory.get_recent_trades(10)
    last_trade = next(
        (t for t in recent_trades if t.get("coin") == analysis["coin"]),
        None,
    )
    last_trade_time = last_trade.get("executed_at") if last_trade else None

    # News blackout: stand down only on GENUINELY adverse news. The AI judges
    # the recent (last 48h) headlines and emits news_risk; only "negative"
    # blocks. This replaced a dumb keyword blocklist that fired on the mere
    # mention of "earnings"/"SEC" etc. — an earnings BEAT is bullish and must
    # not block. Sentiment also makes the old equity-perp exemption unnecessary:
    # the AI won't flag a beat as negative, but WILL flag a miss/fraud.
    news_text = analysis.get("news_context") or ""
    news_risk = str(analysis.get("news_risk") or "none").lower()
    has_binary_news = news_risk == "negative"
    binary_news_match = ""
    if has_binary_news and news_text:
        # Surface a representative adverse headline so the log says what tripped it.
        m = re.search(
            r"\b(hack|exploit|lawsuit|halt|delist|miss|crash|plunge|fraud)\w*"
            r"|\bfomc\b|\bcpi\b|\bsec\b|\bfed(eral)?\b",
            news_text, re.IGNORECASE,
        )
        if m:
            term = m.group(0)
            headline = next(
                (h.strip() for h in news_text.split("|") if term.lower() in h.lower()),
                news_text[:140],
            )
            binary_news_match = f"'{term}' in: {headline}"
        else:
            binary_news_match = news_text[:140]

    trade_side = analysis.get("side", "long") or "long"
    ctx = GateContext(
        confidence=analysis["confidence"],
        current_positions=positions,
        trade_notional_usd=trade_notional,
        daily_pnl=daily_pnl,
        market_volume_24h_usd=_get_market_volume_24h(analysis["coin"]),
        coin=analysis["coin"],
        trade_side=trade_side,
        has_binary_news_risk=has_binary_news,
        binary_news_match=binary_news_match,
        equity=agg_equity,
        total_open_notional=total_open_notional,
        composite_score=float(analysis.get("composite_score", 0) or 0),
        momentum_burst_fired=bool(analysis.get("momentum_burst_fired", False)),
        slow_burn_fired=bool(analysis.get("slow_burn_fired", False)),
        # whale_regime_bypass gates whether a whale signal can bypass the
        # counter-regime gate. Missing config fails closed.
        whale_signal_fired=bool(analysis.get("whale_signal")) and bool(config.get("whale_regime_bypass", False)),
        peak_daily_pnl=memory.peak_daily_pnl(),
    )

    gate_output = eval_all_gates(
        ctx,
        config,
        last_trade_time,
        analysis=analysis,
        trace_id=str(analysis.get("trace_id") or ""),
    )

    if gate_output["blocked"]:
        # ── Capital-rotation (Phase-1 lever) — SHADOW by default ─────────────
        # Phase-1 finding: 94% of missed movers die at the 300% cap / max_concurrent
        # (book full), not at the signal. When a strong fresh candidate is blocked
        # PURELY by capital, evaluate whether it should displace the weakest stale
        # non-winner. shadow_mode logs the decision WITHOUT acting so we validate
        # the ranking on live data before it ever moves real money. Fully wrapped:
        # a rotation bug can never break the (already-blocked) execution path.
        try:
            _rot = config.get("capital_rotation", {}) or {}
            if bool(_rot.get("enabled", False)):
                from hermes_trader.agents.rotation import decide_rotation
                _now_ms = time.time() * 1000
                _trade_ts = memory.latest_trade_ts_by_coin(50)
                _opos = []
                for _p in (state.get("asset_positions") or []):
                    _pp = _p.get("position", {}) or {}
                    _c = _pp.get("coin")
                    if not _c:
                        continue
                    _opos.append({
                        "coin": _c,
                        "roe_pct": float(_pp.get("returnOnEquity", 0) or 0) * 100,
                        "age_minutes": (_now_ms - _trade_ts.get(_c, _now_ms)) / 60000.0,
                    })
                _d = decide_rotation(
                    candidate_coin=analysis["coin"],
                    candidate_composite=float(analysis.get("composite_score", 0) or 0),
                    blocked_reasons=gate_output["block_reasons"],
                    open_positions=_opos,
                    min_candidate_composite=float(_rot.get("min_candidate_composite", 40.0)),
                    min_hold_minutes=float(_rot.get("min_hold_minutes", 30.0)),
                    protect_winner_roe_pct=float(_rot.get("protect_winner_roe_pct", 3.0)),
                )
                if _d.should_rotate and not _rotation_retry:
                    if bool(_rot.get("shadow_mode", True)):
                        logger.warning(f"[rotation][SHADOW] {_d.reason} "
                                       f"(would execute if rotation goes live)")
                    else:
                        # LIVE: close the weakest non-winner to free capital, then
                        # retry THIS candidate once. The retry re-reads account state
                        # (sees the freed margin/slot) and goes through every risk
                        # gate again — rotation only relieves the capital constraint,
                        # it never bypasses a real veto. _rotation_retry=True blocks a
                        # second rotation so this can't loop.
                        logger.warning(f"[rotation][LIVE] {_d.reason} — closing {_d.evict_coin}")
                        _cr = close_position_market(_d.evict_coin)
                        if _cr.get("ok"):
                            logger.warning(f"[rotation][LIVE] evicted {_d.evict_coin} "
                                           f"(rl {_cr.get('realized_pnl_pct')}%) → retrying {analysis['coin']}")
                            return maybe_execute(analysis, _rotation_retry=True)
                        logger.warning(f"[rotation][LIVE] evict {_d.evict_coin} failed "
                                       f"({_cr.get('error')}) — no rotation")
        except Exception as _e:
            logger.warning(f"[rotation] eval failed (non-fatal): {_e}")

        # Don't write blocked attempts to memory._trades — the cooldown gate
        # keys off the most recent trade-by-coin and would self-perpetuate.
        # Visibility comes from the `execute` event in the session log.
        return {
            "executed": False, "mode": mode,
            "analysis_id": analysis["id"],
            "blocked_by": gate_output["block_reasons"],
            "gate_results": gate_output["results"],
        }

    if shadow_mode:
        return {
            "executed": False, "mode": mode,
            "analysis_id": analysis["id"],
            "reason": "shadow_mode_would_execute",
            "gate_results": gate_output["results"],
            "size_usd": trade_notional,
        }

    # ── R2 fix: apply HTA-driven size reduction ──────────────────────────
    # The hta_risk gate now returns an actionable `size_factor` when HTA
    # recommends "reduce" (R2) or when the gate fails open on a long
    # (R4 — dead AI advisor no longer green-lights full size). Prior to
    # this fix the advisory was a dead string and executor opened full
    # size. We apply the factor AFTER all gates pass but BEFORE any live
    # order is sent, then re-normalize through entry_size_for_notional so
    # DSL/SL/TP/memory all see the reduced size. If reduction drops
    # notional below the exchange minimum we abort rather than silently
    # bumping back up — that would defeat the HTA veto.
    _hta_res = (gate_output.get("results") or {}).get("hta_risk") or {}
    _size_factor = _hta_res.get("size_factor")
    if _size_factor is not None:
        try:
            _size_factor = float(_size_factor)
        except (TypeError, ValueError):
            _size_factor = 1.0
        _size_factor = max(0.01, min(1.0, _size_factor))
        if _size_factor < 0.999:
            _orig_notional = trade_notional
            trade_notional *= _size_factor
            # Re-validate against HL minimum; do NOT auto-bump because
            # bumping would undo the risk reduction HTA demanded.
            try:
                _min_n = min_entry_notional_usd(coin, mid_price)
            except Exception:
                _min_n = 0.0
            if _min_n > 0 and trade_notional < _min_n:
                logger.warning(
                    f"[executor] HTA size_factor {_size_factor:.2f} would reduce "
                    f"{coin} below HL minimum (${trade_notional:.2f} < ${_min_n:.2f}) "
                    f"— aborting rather than overriding HTA"
                )
                return {
                    "executed": False, "mode": mode,
                    "analysis_id": analysis["id"],
                    "reason": (
                        f"hta_reduce_below_min ({coin}: factor {_size_factor:.2f} "
                        f"-> ${trade_notional:.2f} < HL min ${_min_n:.2f})"
                    ),
                    "gate_results": gate_output["results"],
                }
            # Re-normalize coin size through the same precision layer used
            # for the original sizing so every downstream component agrees.
            size_in_coin = entry_size_for_notional(coin, trade_notional, mid_price)
            trade_notional = size_in_coin * mid_price
            logger.info(
                f"[executor] HTA size reduction applied for {coin}: "
                f"factor={_size_factor:.2f}, "
                f"${_orig_notional:.2f} -> ${trade_notional:.2f} "
                f"({size_in_coin:g} @ {mid_price:.6g})"
            )

    if not os.environ.get("HYPERLIQUID_PRIVATE_KEY"):
        return {
            "executed": False, "mode": mode,
            "analysis_id": analysis["id"],
            "reason": "private_key_missing",
        }

    is_buy = trade_side == "long"

    # Fetch live mid if legacy sizing did not already need it for ATR sizing.
    if mid_price <= 0:
        mid_price = get_hl_price(coin)
        if mid_price <= 0:
            return {"executed": False, "mode": mode, "analysis_id": analysis["id"],
                    "reason": f"invalid_price_for_{coin}"}

    position_notional = trade_notional

    if atr <= 0:
        atr = get_hl_atr("4h", 14, coin)
        if atr <= 0:
            atr = get_hl_atr("4h", 14, coin)
        if atr <= 0:
            return {
                "executed": False, "mode": mode, "analysis_id": analysis["id"],
                "reason": f"no_atr_no_stop ({coin}: insufficient candle history to size a stop)",
            }

    # Pre-trade ATR volatility gate (post-HYPE postmortem 2026-08-21):
    # Reject entries whose 4h ATR(14) exceeds a ceiling of spot. HYPE printed
    # 28.75% ATR at entry, which pushed the backup SL to -43.1% and created a
    # 40pp gap vs the DSL floor (3% cap). Such names are unmanageable even
    # with the new 3% ceiling clamp because a 3% stop on a 28%-ATR coin fires
    # on noise within 1-2 candles. Config key: max_atr_pct (default 15.0);
    # legacy env HERMES_MAX_ATR_PCT still honored for backward compatibility.
    _max_atr_pct = float(
        os.environ.get("HERMES_MAX_ATR_PCT")
        or cfg_get("max_atr_pct", config=config)
    )
    _atr_pct = (atr / mid_price * 100.0) if mid_price > 0 else 0.0
    if _atr_pct > _max_atr_pct:
        logger.warning(
            f"[executor] SKIPPING {coin}: ATR% {_atr_pct:.2f}% > {_max_atr_pct:.1f}% max "
            f"(atr={atr:.6g}, mid={mid_price:.6g}, lev={leverage}x) — volatility too high for risk caps"
        )
        return {
            "executed": False, "mode": mode,
            "analysis_id": analysis["id"],
            "reason": f"atr_too_high ({_atr_pct:.2f}% > {_max_atr_pct:.1f}%)",
        }

    set_leverage(coin, leverage)

    # Pre-trade spread gate: skip coins with illiquid order books to avoid
    # catastrophic slippage on testnet / low-cap names.
    # Config key: max_spread_pct (default 1.0); legacy env HERMES_MAX_SPREAD_PCT
    # still honored.
    _max_spread_pct = float(
        os.environ.get("HERMES_MAX_SPREAD_PCT")
        or cfg_get("max_spread_pct", config=config)
    )
    _ob = get_orderbook_spread(coin)
    if _ob.get("ok"):
        logger.info(
            f"[executor] Pre-trade {coin}: spread={_ob['spread_pct']:.3f}%, "
            f"bid_depth=${_ob['bid_depth_1pct_usd']:,.0f}, "
            f"ask_depth=${_ob['ask_depth_1pct_usd']:,.0f}, "
            f"notional=${trade_notional:,.0f}"
        )
        if _ob["spread_pct"] > _max_spread_pct:
            logger.warning(
                f"[executor] SKIPPING {coin}: spread {_ob['spread_pct']:.3f}% "
                f"> {_max_spread_pct:.1f}% max (bid={_ob['best_bid']}, ask={_ob['best_ask']})"
            )
            return {
                "executed": False, "mode": mode,
                "analysis_id": analysis["id"],
                "reason": f"spread_too_wide ({_ob['spread_pct']:.2f}% > {_max_spread_pct:.1f}%)",
            }
    else:
        # Fail CLOSED by default: an unreadable order book is exactly the
        # condition (liquidity drought / API outage / delisting) where crossing
        # 1% past best ask/ask is most dangerous. Operators may opt back into
        # the legacy fail-open behaviour via config spread_gate_fail_open=true
        # or env HERMES_SPREAD_GATE_FAIL_OPEN=1.
        _fail_open = (
            os.environ.get("HERMES_SPREAD_GATE_FAIL_OPEN", "0") == "1"
            or bool(cfg_get("spread_gate_fail_open", config=config))
        )
        if _fail_open:
            logger.warning(
                f"[executor] Pre-trade {coin}: orderbook unavailable "
                f"({_ob.get('error', 'unknown')}) — FAIL_OPEN overridden, proceeding"
            )
        else:
            logger.error(
                f"[executor] SKIPPING {coin}: orderbook unavailable "
                f"({_ob.get('error', 'unknown')}) — spread gate fail-closed"
            )
            return {
                "executed": False, "mode": mode,
                "analysis_id": analysis["id"],
                "reason": f"orderbook_unavailable ({_ob.get('error', 'unknown')})",
                "gate_results": gate_output["results"],
            }

    order_res = place_hl_order(is_buy, size_in_coin, mid_price, coin)

    if not order_res.get("ok"):
        return {
            "executed": False, "mode": mode, "analysis_id": analysis["id"],
            "reason": f"order_failed: {order_res.get('error', 'unknown')}",
            "gate_results": gate_output["results"],
        }

    # Phase-1: local state persistence — wrap in try/except to prevent
    # orphaned positions (exchange has position but local state doesn't).
    try:
        arrival_mid = float(mid_price or 0)
        try:
            filled_px = float(order_res.get("avg_px") or 0)
        except (TypeError, ValueError):
            filled_px = 0.0
        try:
            filled_size = float(order_res.get("total_sz") or 0)
        except (TypeError, ValueError):
            filled_size = 0.0
        entry_px = filled_px if filled_px > 0 else mid_price
        if filled_size > 0:
            size_in_coin = filled_size
        position_notional = abs(size_in_coin) * entry_px

        # Register the position with the DSL tracker; it re-evaluates the exit
        # floor on every scan tick (loss protection -> profit locking).
        dsl_config = config.get("dsl_exit", {})
        # Regime-aware exits: scalp (base) in chop/down to bank fast; trend-ride params
        # when regime=='up' to ride rippers. detect_regime is cached (TTL) and already
        # computed by the market_regime gate in this same execute flow — no extra fetch.
        _regime = "neutral"
        try:
            from hermes_trader.agents.market_regime import detect_regime
            _regime = detect_regime(analysis["coin"])
        except Exception as _re_e:
            logger.debug(f"[executor] regime lookup failed (non-fatal): {_re_e}")
        _ex_protect, _ex_retrace, _tiers_raw, _ex_ml_pct, _ex_ml_roe, _ex_label = \
            select_exit_params(dsl_config, _regime)
        # phase2_tiers is optional in config; when present it OVERRIDES the class
        # default ladder so profit-locking tightness is tunable without code edits.
        _tiers = [RetraceTier(**t) for t in _tiers_raw] if _tiers_raw else None
        _atr_cfg = dsl_config.get("atr_stop", {}) or {}
        _noise_cfg = dsl_config.get("noise_band", {}) or {}
        logger.info(f"[executor] exit policy = {_ex_label} (regime={_regime}) "
                    f"protect={_ex_protect} retrace={_ex_retrace} "
                    f"max_loss={_ex_ml_pct}% max_loss_roe={_ex_ml_roe}%")
        policy = ExitPolicy(
            max_loss_pct=_ex_ml_pct,
            max_loss_roe_pct=_ex_ml_roe,
            protect_pct=_ex_protect,
            retrace_threshold=_ex_retrace,
            hard_timeout_minutes=dsl_config.get("hard_timeout_minutes", cfg_get("dsl_exit.hard_timeout_minutes")),
            breakeven_trigger_pct=dsl_config.get("breakeven_trigger_pct", 0.0),
            breakeven_lock_pct=dsl_config.get("breakeven_lock_pct", 0.0),
            atr_stop_enabled=bool(_atr_cfg.get("enabled", False)),
            atr_stop_mult=float(_atr_cfg.get("atr_mult", 1.5)),
            atr_stop_floor_pct=float(_atr_cfg.get("floor_pct", 1.0)),
            atr_stop_ceiling_pct=float(_atr_cfg.get("ceiling_pct", 4.0)),
            stale_flat_timeout_minutes=float(dsl_config.get("stale_flat_timeout_minutes", 0.0) or 0.0),
            consecutive_breaches_required=int(dsl_config.get("consecutive_breaches_required", 1) or 1),
            noise_band_enabled=bool(_noise_cfg.get("enabled", False)),
            noise_band_atr_mult=float(_noise_cfg.get("atr_mult", 1.0)),
            phase2_tiers=_tiers if _tiers else ExitPolicy().phase2_tiers,
        )
        # ATR as % of entry — captured once here so the DSL stop width is stable
        # for the life of the trade (the atr_stop feature scales off this).
        entry_atr_pct = (atr / entry_px * 100) if entry_px > 0 else 0.0
        # Use actual fill time from the exchange response when available;
        # this gives the DSL hard_timeout an accurate baseline instead of
        # counting from "now" (which would be early for slow fills).
        _fill_ms = order_res.get("filled_at_ms")
        _entry_time_sec = (_fill_ms / 1000.0) if _fill_ms else time.time()
        register_position(coin, trade_side, entry_px, entry_time=_entry_time_sec,
                          policy=policy, leverage=leverage,
                          entry_atr_pct=entry_atr_pct, entry_regime=_regime)
        logger.info(f"[executor] Registered DSL exit for {coin} {trade_side} @ {entry_px} ({leverage}x)")

        _entry_ts = int(time.time() * 1000)
        memory.record_trade({
            "id": str(uuid.uuid4()),
            "analysis_id": analysis["id"],
            "coin": coin,
            "side": trade_side,
            "entry_px": entry_px,
            "size_usd": position_notional,
            "order_id": order_res.get("order_id"),
            "executed_at": _entry_ts,
        })

        # Entry-context snapshot for the forward signal backtest: record WHEN we opened
        # and WHAT the free signals said at entry (cache-only — no network on the hot
        # path) plus the enforcement decision. The matching close pulls this so each
        # outcome row carries (entry_time, signals_at_entry) — the join the backtest
        # needs and that the outcome store previously lacked.
        try:
            from hermes_trader.agents.shadow_signals import gather_shadow_signals
            _entry_sig = gather_shadow_signals(coin, trade_side,
                                               config.get("shadow_signals") or {}, allow_fetch=False)
            # Execution-quality capture: arrival mid vs actual fill = real entry
            # slippage (the # the backtests don't model). Signed as adverse cost bps
            # (long paying above mid / short selling below = positive cost).
            _arr_mid = arrival_mid
            _fill = filled_px
            _slip_bps = None
            if _arr_mid > 0 and _fill > 0:
                raw = (_fill - _arr_mid) / _arr_mid * 1e4
                _slip_bps = round(raw if trade_side == "long" else -raw, 1)
            # Funding carry: capture the latest hourly funding rate at entry (one call;
            # entries are rare so this isn't the rate-sensitive scan path). Realized
            # funding cost is estimated at close from rate × hold_hrs × notional × side.
            _funding_hr = None
            try:
                from hermes_trader.client.hl_client import fetch_funding_history
                _fh = fetch_funding_history(coin, int(time.time() * 1000) - 86_400_000)
                if _fh:
                    _r = float(_fh[-1].get("fundingRate", 0) or 0)
                    _funding_hr = _r if _r == _r else None  # NaN guard
            except Exception:
                _funding_hr = None
            memory.record_entry_context(coin, trade_side, {
                "entry_time": _entry_ts,
                "arrival_mid": _arr_mid,
                "entry_fill": _fill,
                "entry_slip_bps": _slip_bps,
                "funding_rate_hr": _funding_hr,
                "regime": _regime,          # market_regime at entry (already computed above)
                "signals": _entry_sig,
                "enforcement": ({"veto": _enf.veto, "veto_reason": _enf.veto_reason,
                                 "boost": _enf.boost, "boost_reason": _enf.boost_reason}
                                if _enf is not None else {}),
                "override_bar": override_composite,
                "forced_override": analysis.get("verdict") == "LONG"
                                   and "[structural override]" in (analysis.get("reasoning") or ""),
            })
        except Exception as _ec_e:
            logger.debug(f"[executor] entry-context capture failed (non-fatal): {_ec_e}")
    except Exception as _state_e:
        logger.critical(f"[executor] LOCAL STATE WRITE FAILED after {coin} order "
                        f"placed on exchange — position is ORPHANED: {_state_e}")
        # Fall through: exchange position exists, next rehydrate cycle will
        # pick it up via DSL dsl_exit_manager.rehydrate_from_exchange().

    # Backup exchange stop-loss bracket — fires server-side (instantly, between our
    # DSL checks) to cap the gap-throughs the DSL loop misses. DSL is still the
    # primary/normal exit; this is the fast safety net.
    #
    # CEILING CLAMP (post-HYPE postmortem 2026-08-21): previously the backup SL
    # used a raw `entry - atr*mult` with no upper bound, so on high-volatility
    # coins (HYPE ATR=28.75% at entry) the server-side stop landed 43% away from
    # entry while the DSL floor sat at -3% — a 40pp unprotected gap. HYPE flash-
    # crashed through the DSL floor between two 60s polls, the backup SL never
    # fired (price never reached -43%), and the position realized -252% ROE.
    # The clamp keeps the backup SL within `sl_ceiling_pct` of entry so it always
    # overlaps the DSL floor's blast radius.
    # H3: validate config-derived SL widths. A zero/negative sl_ceiling_pct
    # would place the backup stop AT or on the WRONG SIDE of entry (immediate
    # / inverted trigger); a giant ceiling recreates the HYPE 43% gap. Clamp
    # to sane bounds and fall back to defaults on non-finite / non-positive
    # values rather than trusting arbitrary config.
    sl_atr_mult = float(config.get("sl_atr_mult", _DEFAULT_SL_ATR_MULT))
    sl_ceiling_pct = float(config.get("sl_ceiling_pct", _DEFAULT_SL_CEILING_PCT))
    if not (math.isfinite(sl_atr_mult) and sl_atr_mult > 0):
        logger.warning(f"[executor] invalid sl_atr_mult={sl_atr_mult} — falling back to {_DEFAULT_SL_ATR_MULT}")
        sl_atr_mult = _DEFAULT_SL_ATR_MULT
    if not (math.isfinite(sl_ceiling_pct) and sl_ceiling_pct > 0):
        logger.warning(f"[executor] invalid sl_ceiling_pct={sl_ceiling_pct} — falling back to {_DEFAULT_SL_CEILING_PCT}")
        sl_ceiling_pct = _DEFAULT_SL_CEILING_PCT
    # Hard upper bound: a backup stop wider than 15% from entry leaves the same
    # unprotected gap class as the HYPE incident regardless of config.
    _SL_CEILING_HARD_MAX_PCT = 15.0
    if sl_ceiling_pct > _SL_CEILING_HARD_MAX_PCT:
        logger.warning(f"[executor] sl_ceiling_pct={sl_ceiling_pct}% exceeds hard max "
                       f"{_SL_CEILING_HARD_MAX_PCT}% — clamping")
        sl_ceiling_pct = _SL_CEILING_HARD_MAX_PCT
    sl_missing = False
    if atr > 0 and size_in_coin > 0:
        atr_stop_pct = (atr / entry_px) * sl_atr_mult * 100
        sl_width_pct = min(atr_stop_pct, sl_ceiling_pct)
        sl_px = entry_px * (1 - sl_width_pct / 100) if is_buy else entry_px * (1 + sl_width_pct / 100)
        sl_res = place_hl_trigger_order(is_buy, size_in_coin, sl_px, "sl", coin)
        if not sl_res.get("ok"):
            # One retry after a beat — observed failures are transient 429s; a
            # position with no server-side stop carries the full gap-through
            # risk between DSL checks, so a single retry is cheap insurance.
            time.sleep(2)
            sl_res = place_hl_trigger_order(is_buy, size_in_coin, sl_px, "sl", coin)
        if sl_res.get("ok"):
            logger.info(f"[executor] Placed backup SL at {sl_px:.6g} "
                        f"({sl_width_pct:.2f}% from entry; atr_mult={sl_atr_mult}, "
                        f"ceiling={sl_ceiling_pct}%)")
            # Persist the new SL oid/px/size on the tracker so the dynamic mover
            # (batchModify) can target it and a restart reconciles correctly.
            set_bracket(coin, trade_side,
                        sl_oid=sl_res.get("order_id"),
                        sl_px=sl_px, sl_size=size_in_coin)
        else:
            sl_missing = True
            logger.error(f"[executor] Backup SL FAILED twice for {coin} — POSITION HAS "
                         f"NO SERVER-SIDE STOP (DSL loop is sole protection): "
                         f"target_px={sl_px}, is_buy={is_buy}, size={size_in_coin}")
            _pending_sl_retries[coin] = {
                "is_buy": is_buy,
                "size": size_in_coin,
                "sl_px": sl_px,
                "coin": coin,
                "side": trade_side,
                "retry_count": 0,
                "last_attempt": time.time(),
            }

    # Take-profit scale-out — the OFFENSIVE complement to the backup SL. Banks a
    # fraction of the position SERVER-SIDE at the TP target so a winner is
    # CAPTURED at target (instantly, between 60s DSL checks) instead of running
    # to a peak and round-tripping back into the trailing stop — the documented
    # "we had it all and gave it back" leak. The remainder rides the DSL trail,
    # so we lock realized profit AND keep upside. Disable with tp_scale_fraction<=0.
    tp_scale_fraction = float(config.get("tp_scale_fraction", 0.5))
    if atr > 0 and size_in_coin > 0 and 0 < tp_scale_fraction <= 1.0:
        tp_px_trig = entry_px + atr * TP_ATR_MULT if is_buy else entry_px - atr * TP_ATR_MULT
        tp_size = size_in_coin * tp_scale_fraction
        tp_intended_notional = tp_size * tp_px_trig
        full_notional = size_in_coin * entry_px

        # HL rejects any order whose notional is below $10 (MIN_ORDER_USD = $10.5
        # with buffer). A 50% scale-out on a small position (e.g. $11 notional ->
        # $5.5 TP) is ACCEPTED at placement time as a resting trigger order, then
        # ASYNCHRONOUSLY REJECTED with "minTradeNtlRejected" when it fires — the
        # SDK reports ok at submit time so we never saw the failure (BCH 2026-08-21
        # incident: TP 0.024 @ $243.59 = $5.85 was silently rejected after trigger).
        # Upsize tp_size to the exchange minimum when needed; if that would consume
        # >=90% of the full position the scale-out is meaningless — skip it and let
        # the DSL trail handle the entire exit.
        tp_min_size = entry_size_for_notional(coin, tp_size * tp_px_trig, tp_px_trig)
        tp_min_notional = tp_min_size * tp_px_trig

        # Hard safety clamp: a scale-out TP must NEVER be sized larger than the
        # position it is supposed to partially close. entry_size_for_notional()
        # rounds UP to size precision, which on integer-size markets can push an
        # upsized tp_min_size slightly above size_in_coin (overflow / flip).
        if tp_min_size >= size_in_coin:
            logger.warning(
                f"[executor:tp] SKIP {coin} — min TP size {tp_min_size} "
                f"(${tp_min_notional:.2f}) >= full position {size_in_coin}; "
                f"a scale-out would over-close. DSL trail handles the full exit. "
                f"[skip_reason=min_size_ge_position, tp_px={tp_px_trig:.6g}]"
            )
            tp_size = 0.0

        logger.info(
            f"[executor:tp] {coin} evaluating scale-out: side={'long' if is_buy else 'short'} "
            f"full_size={size_in_coin} (${full_notional:.2f}), "
            f"intended_frac={tp_scale_fraction:.0%}, intended_size={tp_size} "
            f"(${tp_intended_notional:.2f}), tp_px={tp_px_trig:.6g} "
            f"(atr={atr:.6g}, {TP_ATR_MULT}x ATR), "
            f"hl_min_size={tp_min_size} (${tp_min_notional:.2f})"
        )

        if tp_size <= 0:
            pass  # clamped/skipped above
        elif tp_size < tp_min_size:
            if tp_min_size >= size_in_coin * 0.9:
                tp_pct_of_position = (tp_min_size / size_in_coin) * 100
                logger.warning(
                    f"[executor:tp] SKIP {coin} — min TP size {tp_min_size} "
                    f"(${tp_min_notional:.2f}) would consume {tp_pct_of_position:.1f}% "
                    f"of full position {size_in_coin} (>=90% threshold). "
                    f"Intended TP size {tp_size} (${tp_intended_notional:.2f}) is "
                    f"below HL minimum (${MIN_ORDER_USD:.2f}). The DSL trailing "
                    f"floor will handle the full exit. "
                    f"[skip_reason=min_size_ge_90pct, tp_px={tp_px_trig:.6g}]"
                )
            else:
                tp_old_notional = tp_size * tp_px_trig
                tp_upsized_pct = (tp_min_size / size_in_coin) * 100
                logger.warning(
                    f"[executor:tp] UPSIZE {coin} — intended TP size {tp_size} "
                    f"(${tp_old_notional:.2f}) is below HL minimum {tp_min_size} "
                    f"(${tp_min_notional:.2f}). Upsizing TP to {tp_min_size} "
                    f"({tp_upsized_pct:.1f}% of position {size_in_coin}). "
                    f"[reason=notional_below_min, min_usd=${MIN_ORDER_USD:.2f}, "
                    f"tp_px={tp_px_trig:.6g}]"
                )
                tp_size = tp_min_size
                tp_res = place_hl_trigger_order(is_buy, tp_size, tp_px_trig, "tp", coin)
                if tp_res.get("ok"):
                    oid = tp_res.get("order_id", "N/A")
                    logger.info(
                        f"[executor:tp] PLACED (upsized) {coin}: size={tp_size} "
                        f"(${tp_size * tp_px_trig:.2f}), trigger_px={tp_px_trig:.6g}, "
                        f"fraction={tp_size / size_in_coin:.1%}, order_id={oid}"
                    )
                    set_bracket(coin, trade_side, tp_oid=oid, tp_px=tp_px_trig)
                else:
                    logger.error(
                        f"[executor:tp] FAILED (upsized) {coin}: size={tp_size} "
                        f"(${tp_size * tp_px_trig:.2f}), trigger_px={tp_px_trig:.6g}, "
                        f"error={tp_res.get('error')}"
                    )
        else:
            tp_res = place_hl_trigger_order(is_buy, tp_size, tp_px_trig, "tp", coin)
            if tp_res.get("ok"):
                oid = tp_res.get("order_id", "N/A")
                logger.info(
                    f"[executor:tp] PLACED {coin}: size={tp_size} "
                    f"(${tp_intended_notional:.2f}), trigger_px={tp_px_trig:.6g}, "
                    f"fraction={tp_scale_fraction:.0%}, order_id={oid}"
                )
                set_bracket(coin, trade_side, tp_oid=oid, tp_px=tp_px_trig)
            else:
                logger.error(
                    f"[executor:tp] FAILED {coin}: size={tp_size} "
                    f"(${tp_intended_notional:.2f}), trigger_px={tp_px_trig:.6g}, "
                    f"error={tp_res.get('error')}"
                )

    if atr > 0 and size_in_coin > 0:
        atr_stop_pct = (atr / entry_px) * sl_atr_mult * 100
        sl_width_pct = min(atr_stop_pct, sl_ceiling_pct)
        final_sl = entry_px * (1 - sl_width_pct / 100) if is_buy else entry_px * (1 + sl_width_pct / 100)
    else:
        final_sl = stop_px
    final_tp = (entry_px + atr * TP_ATR_MULT) if is_buy else (entry_px - atr * TP_ATR_MULT) if atr > 0 else tp_px

    return {
        "executed": True, "mode": mode,
        "analysis_id": analysis["id"],
        "order_id": order_res.get("order_id"),
        "gate_results": gate_output["results"],
        "size_usd": position_notional,
        "entry_px": entry_px,
        "stop_px": final_sl,
        "tp_px": final_tp,
        "dsl_registered": True,
        "sl_missing": sl_missing,
    }


def monitor_exits(mids: Dict[str, float]) -> List[Dict[str, Any]]:
    """Check all DSL-tracked positions and return those that should be closed.

    `side` is the long/short of the actual position; `phase` is the DSL phase
    (phase1/phase2/timeout). `leveraged_pct` ≈ spot move × leverage and matches
    what Hyperliquid's UI shows on the user's margin.
    """
    exits = check_all_positions(mids)
    return [
        {
            "coin": v.coin,
            "side": v.position_side,
            "phase": v.phase,
            "leverage": v.leverage,
            "reason": v.reason,
            "unrealized_pct": v.unrealized_pct,
            "leveraged_pct": v.unrealized_pct * v.leverage,
            # Exit telemetry for per-regime stop audit:
            "entry_regime": v.entry_regime,
            "hold_min": v.hold_min,
            "mfe_pct": v.mfe_pct,
        }
        for v in exits
    ]


def sync_exchange_sl(mids: Dict[str, float]) -> None:
    """Move each position's exchange backup SL to trail the DSL floor in Phase 2.

    The DSL software floor (polled every ~15s) is the PRIMARY exit. The static
    exchange SL is a disaster net for gap-throughs between polls. Once a position
    is in Phase 2 (profit >= protect_pct) and the DSL floor ratchets tighter, we
    pull the exchange SL along behind it (floor ± buffer) so the safety net keeps
    overlapping the locked-in profit instead of sitting at the initial 3% ceiling.

    Safety guards (all enforced here):
      * Only TIGHTENS — a long's SL never moves down, a short's never moves up.
      * Never places the exchange SL AHEAD of (inside) the DSL floor: it trails by
        _SL_BUFFER_BPS so the DSL triggers first on a normal pull-back.
      * Min-move threshold (_SL_MOVE_MIN_BPS) skips micro-ratchets.
      * Per-coin throttle (_SL_MOVE_MIN_INTERVAL_SEC) limits batchModify rate.
      * On success the NEW oid (HL cancel+replace) is persisted via set_bracket.

    Best-effort: never raises — a failure is logged and retried next cycle.
    """
    from hermes_trader.agents import dsl_exit

    now = time.time()
    for tracker in list(dsl_exit._active_positions.values()):
        coin = tracker.coin
        mark_px = mids.get(coin)
        if mark_px is None:
            continue
        try:
            mark_f = float(mark_px)
        except (TypeError, ValueError):
            continue
        if mark_f <= 0:
            continue

        # Need a known resting SL to modify; backfill runs in rehydrate, but if
        # it's still missing there's nothing to move (the pending-retry queue or
        # the next placement will establish one).
        if not tracker.sl_oid:
            continue

        # The DSL exit pass already called tracker.check(mark_f) this tick (via
        # monitor_exits / check_all_positions), which recomputed and ratcheted
        # `_last_floor`. Do NOT call check again here — that would double-count
        # consecutive_breaches. Reuse the already-computed floor and gate on the
        # Phase-2 profit threshold directly.
        floor = tracker._last_floor
        if floor is None or floor <= 0:
            continue

        # Only coordinate in Phase 2 (profit >= protect_pct). In Phase 1 the
        # initial static SL already sits behind the max-loss floor and shouldn't
        # move. Peak-based tier ratchets only kick in past protect_pct.
        if tracker._unrealized_pct(mark_f) < tracker.policy.protect_pct:
            continue

        is_long = tracker.is_long()
        # Target trigger: buffer BEHIND the floor (adverse side) so DSL fires first.
        if is_long:
            target = floor * (1.0 - _SL_BUFFER_BPS / 10_000.0)
        else:
            target = floor * (1.0 + _SL_BUFFER_BPS / 10_000.0)

        size = tracker.sl_size
        if not size or size <= 0:
            continue

        # ── Only-tighten guard ──────────────────────────────────────────
        cur = tracker.sl_px
        if cur is not None and cur > 0:
            tighter = (is_long and target >= cur) or (not is_long and target <= cur)
            if not tighter:
                continue
            # Min-move filter (relative to entry): skip sub-threshold ratchets.
            move_bps = abs(target - cur) / tracker.entry_px * 10_000.0
            if move_bps < _SL_MOVE_MIN_BPS:
                continue

        # ── Per-coin throttle ───────────────────────────────────────────
        st = _sl_move_state.get(coin)
        if st is not None:
            last_ts, last_target = st
            if now - last_ts < _SL_MOVE_MIN_INTERVAL_SEC:
                continue
            if last_target is not None and abs(last_target - target) < 1e-12:
                continue
        _sl_move_state[coin] = (now, target)

        try:
            res = modify_sl_trigger(
                is_long_position=is_long,
                size=size,
                new_trigger_px=target,
                coin=coin,
                oid=int(tracker.sl_oid),
            )
        except Exception as e:
            logger.warning(f"[sl-move] {coin} exception: {e}")
            continue

        if res.get("ok"):
            new_oid = res.get("order_id")
            # Capture the OLD oid BEFORE set_bracket overwrites it, otherwise
            # the log below would report old==new and the cancel+replace chain
            # would be untraceable.
            old_oid = tracker.sl_oid
            # batchModify is cancel+replace: persist the NEW oid and target px.
            set_bracket(coin, tracker.side,
                        sl_oid=new_oid, sl_px=target, sl_size=size)
            logger.info(
                f"[sl-move] {coin} {tracker.side} moved exchange SL: "
                f"old_oid={old_oid} new_oid={new_oid} "
                f"floor={floor:.6g} target_sl={target:.6g}"
            )
        else:
            logger.warning(
                f"[sl-move] {coin} modify FAILED (will retry next cycle): "
                f"oid={tracker.sl_oid} target={target:.6g} error={res.get('error')}"
            )


def route_verdict(analysis: Dict[str, Any], *, execute_fn=None, close_fn=None) -> Dict[str, Any]:
    """Route an analysis to the right action based on its verdict.

    Pure routing logic with the side-effecting functions injected, so EVERY
    verdict path is unit-testable. This exists because the dropped-CLOSE bug
    hid inside the trading loop's inline `if verdict in (...)` — orchestration
    that couldn't be tested. Now the loop calls this and just logs the result.

    Returns {"action": <str>, "verdict": <str>, "result": <dict|None>}:
      - LONG / SHORT  → action="execute", result = execute_fn(analysis)
      - CLOSE         → action="close",   result = close_fn(coin)
      - PASS          → action="none"
      - anything else → action="unknown" (logged loudly; never silently dropped)
    """
    execute_fn = execute_fn or maybe_execute
    close_fn = close_fn or close_position_market
    verdict = (analysis.get("verdict") or "").upper()
    coin = analysis.get("coin")

    if verdict in ("LONG", "SHORT"):
        return {"action": "execute", "verdict": verdict, "result": execute_fn(analysis)}
    if verdict == "CLOSE":
        return {"action": "close", "verdict": verdict, "result": close_fn(coin)}
    if verdict == "PASS":
        # A hedging AI PASS can still carry a structural-override HINT: a whale
        # accumulation signal, or a strong slow-burn composite. maybe_execute
        # owns the real override decision and all gates, but it's only ever
        # reached via this router — so route only PASS verdicts whose force path
        # is actually enabled. A disabled force path should be a true no-op, not
        # an executor round-trip that looks like a missed trade in the logs.
        _rv_cfg = read_agent_config()
        _bar = float(cfg_get("force_execute_composite", config=_rv_cfg))
        has_whale = (
            bool(analysis.get("whale_signal"))
            and bool(_rv_cfg.get("whale_force_execute", False))
        )
        slow_burn_hint = (
            bool(_rv_cfg.get("composite_force_execute", False))
            and float(analysis.get("composite_score", 0) or 0) >= _bar
            and int(analysis.get("slow_burn_count", 0) or 0)
            >= int(_rv_cfg.get("force_execute_slow_burn_count", 2))
        )
        breakout_hint = (
            bool(_rv_cfg.get("breakout_force_execute", False))
            and
            bool(analysis.get("volume_spike_fired"))
            and (bool(analysis.get("breakout_fired"))
                 or bool(analysis.get("uptrend_momentum_fired")))
            and (int(analysis.get("slow_burn_count", 0) or 0) >= 1
                 or float(analysis.get("composite_score", 0) or 0) >= _bar)
        )
        # Composite hint: route a TA-confirmed composite>=bar PASS to maybe_execute
        # so its (gated) composite_force_execute path can upgrade it. No-ops cleanly
        # in maybe_execute when the flag is off, so this is safe either way.
        composite_hint = (
            bool(_rv_cfg.get("composite_force_execute", False))
            and float(analysis.get("composite_score", 0) or 0) >= _bar
        )
        # Keep this in lockstep with ta_sidestep_strong in maybe_execute — a
        # looser hint here just routes candidates the executor will refuse.
        sidestep_hint = (
            bool(_rv_cfg.get("ta_sidestep_force_execute", False))
            and int(analysis.get("slow_burn_count", 0) or 0) >=
            int(_rv_cfg.get("ta_sidestep_min_slow_burn_count", 1) or 1)
            and (
                float(analysis.get("composite_score", 0) or 0) >= _bar
                or bool(analysis.get("momentum_burst_fired"))
            )
        )
        if has_whale or slow_burn_hint or breakout_hint or composite_hint or sidestep_hint:
            return {"action": "execute", "verdict": "PASS",
                    "result": execute_fn(analysis)}
        return {"action": "none", "verdict": "PASS", "result": None}
    # Should be unreachable (parse_verdict normalizes to one of the above),
    # but never silently drop — surface it so a new verdict can't go unhandled.
    logger.warning(f"[router] unhandled verdict {verdict!r} for {coin} — treating as no-op")
    return {"action": "unknown", "verdict": verdict, "result": None}


def _runner_entry_block_reason(analysis: Dict[str, Any], config: Dict[str, Any]) -> str:
    """Block entries that are not fresh runner setups.

    The live ledger's repeated loss mode is not "no runners exist"; it is broad
    admission of late trend-only names and whale-only PASS upgrades. This gate
    keeps execution focused on fresh impulse setups: breakout (self-confirmed via
    RVOL >= 1.5x), volume+burst, or high-score burst — backed by either 1h
    structure or a strong composite score.
    """
    gate = config.get("runner_entry_gate") or {}
    if not bool(gate.get("enabled", False)):
        return ""

    coin = analysis.get("coin") or ""
    is_hip3 = ":" in coin
    side = (analysis.get("side") or "").lower()
    conf = float(analysis.get("confidence", 0) or 0)
    # A structural override rewrites confidence up to min_ai_confidence, which is
    # normally the same number as min_confidence — so the floored value clears
    # this gate by construction and the confidence bar becomes a no-op. Judge on
    # the model's pre-floor conviction when it is available; plain (non-override)
    # candidates have no such field and fall back to confidence unchanged.
    gate_conf = float(analysis.get("ai_confidence_raw", conf) or 0)
    score = float(analysis.get("composite_score", 0) or 0)
    min_conf = float(gate.get("min_confidence", 0.70))
    min_score = float(gate.get("min_composite", 30.0))
    min_hip3_score = float(gate.get("min_hip3_composite", 50.0))

    volume = bool(analysis.get("volume_spike_fired"))
    breakout = bool(analysis.get("breakout_fired"))
    burst = bool(analysis.get("momentum_burst_fired"))
    daily_mover = bool(analysis.get("daily_mover_fired"))
    uptrend = bool(analysis.get("uptrend_momentum_fired"))
    downtrend = bool(analysis.get("downtrend_momentum_fired"))
    slow_count = int(analysis.get("slow_burn_count", 0) or 0)
    whale = bool(analysis.get("whale_signal"))
    forced = "[structural override]" in (analysis.get("reasoning") or "")

    # fresh_impulse: a genuine new-impulse entry signal.
    #   - breakout ALONE qualifies because breakout_fired already requires
    #     RVOL >= 1.5x + 2 consecutive closed bars beyond the range edge,
    #     so it is self-confirming on volume. Requiring an *additional*
    #     volumeSpike (z >= 2.0σ) double-gated the same volume dimension
    #     and rejected ~13% of otherwise valid breakouts.
    #   - volume+burst qualifies (a burst without volume is just a low-
    #     liquidity wick; a burst on volume is institutional participation).
    #   - burst+score>=min_score qualifies (a strong-scoring burst may not
    #     print a 2σ volume spike but still carries enough confluence).
    fresh_impulse = breakout or (volume and burst) or (burst and score >= min_score)

    logger.info(
        f"[runner_gate] {coin} side={side} conf={gate_conf:.2f}/{min_conf:.2f} "
        f"score={score:.1f}/{min_score:.0f} slow={slow_count} | "
        f"vol={int(volume)} brk={int(breakout)} burst={int(burst)} "
        f"dMover={int(daily_mover)} up={int(uptrend)} down={int(downtrend)} "
        f"whale={int(whale)} forced={int(forced)} → fresh_impulse={int(fresh_impulse)}"
    )

    if gate_conf < min_conf:
        logger.info(f"[runner_gate] {coin} BLOCKED: confidence {gate_conf:.2f} < {min_conf:.2f}")
        return f"runner_gate_blocked (confidence {gate_conf:.2f} < {min_conf:.2f})"

    # --- Late-entry veto: RSI extremes + over-extension from EMA21 (4h) ---
    # These catch the "buying the top tick / selling the bottom tick" failure
    # mode that fresh_impulse alone doesn't — a momentum burst at RSI 82 after
    # a 12h run is still a "fresh" burst but a terrible entry. Both checks use
    # the 4h snapshot carried in the analysis dict (computed by research()).
    rsi4h = analysis.get("rsi4h")
    rsi_overbought = float(gate.get("rsi_overbought", 75.0))
    rsi_oversold = float(gate.get("rsi_oversold", 25.0))
    if rsi4h is not None:
        try:
            rsi_val = float(rsi4h)
            if side == "long" and rsi_val > rsi_overbought:
                logger.info(f"[runner_gate] {coin} BLOCKED: RSI {rsi_val:.0f} > {rsi_overbought:.0f} (overbought)")
                return (f"runner_gate_blocked (RSI {rsi_val:.0f} > {rsi_overbought:.0f}, "
                        f"overbought — late long chase)")
            if side == "short" and rsi_val < rsi_oversold:
                logger.info(f"[runner_gate] {coin} BLOCKED: RSI {rsi_val:.0f} < {rsi_oversold:.0f} (oversold)")
                return (f"runner_gate_blocked (RSI {rsi_val:.0f} < {rsi_oversold:.0f}, "
                        f"oversold — late short chase)")
        except (TypeError, ValueError):
            pass

    ext_mult = float(gate.get("max_extension_atr", 2.5))
    atr4h = analysis.get("atr4h")
    ema21_4h = analysis.get("ema21_4h")
    close4h = analysis.get("close4h")
    if ext_mult > 0 and atr4h and ema21_4h and close4h:
        try:
            atr_val = float(atr4h)
            ema_val = float(ema21_4h)
            close_val = float(close4h)
            if atr_val > 0 and ema_val > 0:
                extension = (close_val - ema_val) / atr_val
                if side == "long" and extension > ext_mult:
                    logger.info(f"[runner_gate] {coin} BLOCKED: extension {extension:.1f}x ATR > {ext_mult}x (over-extended long)")
                    return (f"runner_gate_blocked (extension {extension:.1f}x ATR "
                            f"above EMA21 — over-extended long)")
                if side == "short" and extension < -ext_mult:
                    logger.info(f"[runner_gate] {coin} BLOCKED: extension {extension:.1f}x ATR < -{ext_mult}x (over-extended short)")
                    return (f"runner_gate_blocked (extension {extension:.1f}x ATR "
                            f"below EMA21 — over-extended short)")
        except (TypeError, ValueError):
            pass

    if side == "short":
        if not bool(gate.get("allow_shorts", False)):
            logger.info(f"[runner_gate] {coin} BLOCKED: shorts disabled")
            return "runner_gate_blocked (shorts disabled)"
        short_min_score = float(gate.get("min_short_composite", min_score))
        short_min_conf = float(gate.get("min_short_confidence", min_conf))
        if conf < short_min_conf:
            logger.info(f"[runner_gate] {coin} BLOCKED: short confidence {conf:.2f} < {short_min_conf:.2f}")
            return f"runner_gate_blocked (short confidence {conf:.2f} < {short_min_conf:.2f})"
        structured_short = (
            downtrend
            or (score >= short_min_score and (slow_count >= 1 or fresh_impulse))
            or (fresh_impulse and score >= min_score)
        )
        if not structured_short:
            logger.info(f"[runner_gate] {coin} BLOCKED: short needs downtrend or fresh impulse+structure (score={score:.0f}, slow={slow_count})")
            return (f"runner_gate_blocked (short needs downtrend momentum or "
                    f"fresh impulse+structure; score={score:.0f}, slow={slow_count})")
        logger.info(f"[runner_gate] {coin} short ADMITTED: score={score:.0f}, slow={slow_count}, fresh={int(fresh_impulse)}, down={int(downtrend)}")
        return ""

    if side != "long":
        return ""

    structured_daily_mover = (
        daily_mover
        and gate_conf >= float(gate.get("mover_min_confidence", 0.80))
        and score >= float(gate.get("mover_min_composite", 45.0))
        and (slow_count >= 1 or volume or breakout or burst)
    )
    structured_runner = fresh_impulse and (slow_count >= 1 or score >= min_score)

    if is_hip3:
        en = config.get("signal_enforcement") or {}
        gex_cfg = config.get("gex_signal") or {}
        if (
            bool(en.get("enabled", False))
            and bool(en.get("veto", True))
            and bool(en.get("gex_veto", True))
            and bool(gex_cfg.get("enabled", True))
        ):
            try:
                from hermes_trader.agents.options_gex import gex_override_caution
                near = float(gex_cfg.get("caution_near_wall_pct", 1.0))
                suppress, why = gex_override_caution(
                    coin, "long", near_wall_pct=near, allow_fetch=False
                )
                if suppress:
                    if bool(gex_cfg.get("shadow_mode", False)):
                        logger.info(f"[executor] GEX entry veto [SHADOW - not blocked] "
                                    f"on {coin}: {why}")
                    else:
                        return f"runner_gate_blocked ({why})"
            except Exception as e:
                logger.debug(f"[executor] GEX entry veto check failed for {coin}: {e}")
    if is_hip3 and score < min_hip3_score:
        logger.info(f"[runner_gate] {coin} BLOCKED: HIP-3 composite {score:.0f} < {min_hip3_score:.0f}")
        return (f"runner_gate_blocked (HIP-3 composite {score:.0f} "
                f"< {min_hip3_score:.0f})")
    if forced and whale and not fresh_impulse:
        logger.info(f"[runner_gate] {coin} BLOCKED: whale-only forced override without fresh breakout/burst")
        return "runner_gate_blocked (whale-only forced override; no fresh breakout/burst)"

    # --- Suggestion A: pullback-long bypass ---------------------------------
    # Admits longs in a confirmed uptrend with structural (slow-burn) backing
    # that have pulled back to a lower-risk entry zone (not over-extended, not
    # overbought).  Evaluated BEFORE the "late trend-only chase" veto because
    # the bypass's own RSI/extension guards are stricter — a candidate that
    # satisfies them is, by construction, NOT a late chase.  When shadow_mode
    # is on, the signal is recorded to a JSONL audit feed but the trade is
    # still blocked, so 48h of real outcomes can be paper-reconciled before
    # live enablement.
    pb_cfg = gate.get("pullback_long") or {}
    if bool(pb_cfg.get("enabled", False)) and not (structured_runner or structured_daily_mover):
        pb_min_score = float(pb_cfg.get("min_composite", 20.0))
        pb_max_rsi = float(pb_cfg.get("max_rsi", 70.0))
        pb_max_ext = float(pb_cfg.get("max_extension_atr", 2.0))
        pb_min_slow = int(pb_cfg.get("min_slow_burn", 1) or 1)
        pb_extension = None
        if ext_mult > 0 and atr4h and ema21_4h and close4h:
            try:
                _a = float(atr4h); _e = float(ema21_4h); _c = float(close4h)
                if _a > 0 and _e > 0:
                    pb_extension = (_c - _e) / _a
            except (TypeError, ValueError):
                pb_extension = None
        pullback_long = (
            side == "long"
            and uptrend
            and slow_count >= pb_min_slow
            and score >= pb_min_score
            and not fresh_impulse
            and (rsi4h is None or float(rsi4h) < pb_max_rsi)
            and (pb_extension is None or pb_extension < pb_max_ext)
        )
        if pullback_long:
            if bool(pb_cfg.get("shadow_mode", False)):
                _record_pullback_shadow(
                    coin=coin, side="long", score=score, conf=gate_conf,
                    slow_count=slow_count, rsi4h=rsi4h,
                    extension_atr=pb_extension,
                    entry_px=analysis.get("mid") or analysis.get("price") or 0.0,
                    trace_id=str(analysis.get("trace_id", "") or ""),
                )
                return (f"runner_gate_blocked (pullback-long SHADOW - "
                        f"recorded, not traded; score={score:.0f}, slow={slow_count})")
            logger.info(f"[executor] pullback-long bypass ADMITS {coin}: "
                        f"score={score:.0f}, slow={slow_count}, "
                        f"rsi4h={rsi4h}, ext={pb_extension}")
            return ""

    if uptrend and not (fresh_impulse or structured_daily_mover):
        logger.info(f"[runner_gate] {coin} BLOCKED: late trend-only chase (uptrend without fresh breakout/burst/daily-mover)")
        return "runner_gate_blocked (late trend-only chase; no fresh breakout/burst)"

    if not (structured_runner or structured_daily_mover):
        logger.info(f"[runner_gate] {coin} BLOCKED: needs fresh impulse + structure (score={score:.0f}, slow={slow_count}, fresh={int(fresh_impulse)})")
        return (f"runner_gate_blocked (needs fresh breakout/burst and structure; "
                f"score={score:.0f}, slow={slow_count})")

    logger.info(
        f"[runner_gate] {coin} long ADMITTED: score={score:.0f}, slow={slow_count}, "
        f"fresh={int(fresh_impulse)}, dMover={int(structured_daily_mover)}, "
        f"runner={int(structured_runner)}"
    )
    return ""


def close_position_market(coin: str) -> Dict[str, Any]:
    """Market-close any open perp position for `coin`. Deregisters the DSL tracker on success.

    Returns include `entry_px`, `fill_px`, and `realized_pnl_pct` (leveraged,
    net of taker fees) whenever the close fills with a parseable avgPx — so the
    trading loop can log the actual realized PnL instead of an estimate based
    on the pre-trade mid.
    """
    user = resolve_user_address()
    if not user:
        return {"ok": False, "coin": coin, "error": "no_user_address"}

    # include_hip3=True so we can resolve HIP-3 positions (xyz:MU, vntl:*, ...).
    # Without this every close call for a HIP-3 position would fall into the
    # `already_flat` branch even when the position is real.
    state = fetch_account_state(user, include_hip3=True)
    pos = next(
        (p for p in state.get("asset_positions", [])
         if p.get("position", {}).get("coin") == coin),
        None,
    )
    if not pos:
        # Already flat — drop any stale tracker so we don't keep retrying.
        deregister_position(coin, "long")
        deregister_position(coin, "short")
        return {"ok": True, "coin": coin, "noop": "already_flat"}

    try:
        szi = float(pos["position"].get("szi", "0") or 0)
        entry_px = float(pos["position"].get("entryPx") or 0)
    except (TypeError, ValueError):
        return {"ok": False, "coin": coin, "error": "bad_szi"}
    if szi == 0:
        deregister_position(coin, "long")
        deregister_position(coin, "short")
        return {"ok": True, "coin": coin, "noop": "zero_szi"}

    is_long = szi > 0
    side = "long" if is_long else "short"
    mid_price = get_hl_price(coin)
    if mid_price <= 0:
        return {"ok": False, "coin": coin, "error": f"invalid_price_for_{coin}"}

    # Look up tracker leverage before close so the realized PnL can be computed
    # at the right multiplier even after deregister. If the tracker is missing
    # (rehydrate failed / state wiped), fall back to the leverage the exchange
    # actually reports for this position — NOT a blind 1x, which would understate
    # ROE, mis-compute fees, and potentially fail to arm the loss cooldown.
    from hermes_trader.agents import dsl_exit
    tracker = dsl_exit._active_positions.get(f"{coin}_{side}")
    if tracker is not None:
        leverage = tracker.leverage
    else:
        _lev_raw = pos["position"].get("leverage", {})
        try:
            leverage = int(_lev_raw.get("value", 0) or 0) if isinstance(_lev_raw, dict) else int(_lev_raw or 0)
        except (TypeError, ValueError):
            leverage = 0
        if leverage <= 0:
            leverage = int(read_agent_config().get("leverage", 1) or 1)
        logger.error(
            f"[executor] close {coin}: tracker MISSING; using exchange/config "
            f"leverage={leverage}x for PnL/fees (loss cooldown may be approximate)"
        )

    # reduce_only: a close must only FLATTEN. Without it, the $10-min size floor in
    # place_hl_order overshoots a sub-$10 position and flips it to the opposite side
    # (the BIRD short<->long churn loop). reduce_only makes HL ignore the excess.
    res = place_hl_order(is_buy=not is_long, size=abs(szi), mid_price=mid_price, coin=coin,
                         reduce_only=True)
    out: Dict[str, Any] = {**res, "coin": coin, "side": side,
                            "entry_px": entry_px, "leverage": leverage}

    if res.get("ok"):
        deregister_position(coin, side)
        # Cancel the now-stranded reduce-only SL/TP trigger bracket so stale
        # orders don't pile up and reject a future reduce-only order on this coin.
        cancel_open_orders_for_coin(coin)
        # MEDIUM: release per-coin bookkeeping that would otherwise leak for the
        # lifetime of the process. _sl_move_state throttles SL moves; _pending_sl_retries
        # holds naked positions awaiting SL replacement — a closed position must
        # not remain in either (a stale retry entry would keep firing reduce-only
        # trigger errors forever).
        _sl_move_state.pop(coin, None)
        _pending_sl_retries.pop(coin, None)
        fill_px = res.get("avg_px")
        if fill_px and entry_px > 0:
            # Spot move from the perspective of the position: long earns when
            # mark rises, short earns when mark falls.
            if is_long:
                spot_pct = (fill_px - entry_px) / entry_px * 100
            else:
                spot_pct = (entry_px - fill_px) / entry_px * 100
            # round-trip taker fills (entry + exit) at the HL taker rate × leverage
            fees_pct = _HL_TAKER_FEE_PCT * _HL_ROUND_TRIP_FILLS * leverage
            out["fill_px"] = fill_px
            out["spot_pct"] = round(spot_pct, 4)
            out["realized_pnl_pct"] = round(spot_pct * leverage - fees_pct, 4)
            out["fees_pct"] = round(fees_pct, 4)
            # ── Trade-outcome store ─────────────────────────────────────────
            # Persist the realized exit so win-rate / payoff / risk-of-ruin /
            # Phase-3 stats have a real source (trades[].pnl was never written).
            # Single chokepoint → covers DSL, AI-close, and kill-switch exits.
            # Wrapped: a bookkeeping failure must never abort a close.
            try:
                _notional_entry = abs(szi) * entry_px
                _closed_at = int(time.time() * 1000)
                # Pull the entry-context snapshot (entry time + signals at entry +
                # enforcement) so this outcome row is self-contained for the forward
                # signal backtest. Empty {} for positions opened before this shipped.
                _ec = memory.pop_entry_context(coin, side)
                _entry_time = _ec.get("entry_time")
                _hold_min = (round((_closed_at - _entry_time) / 60000.0, 1)
                             if _entry_time else None)
                _gross_pnl_usd = _notional_entry * spot_pct / 100.0
                _fee_usd = _notional_entry * (fees_pct / max(leverage, 1)) / 100.0
                _funding_cost_usd = (
                    round(_ec["funding_rate_hr"]
                          * (_hold_min / 60.0 if _hold_min else 0)
                          * _notional_entry
                          * (1 if is_long else -1), 4)
                    if _ec.get("funding_rate_hr") is not None else None
                )
                _net_pnl_usd = _gross_pnl_usd - _fee_usd
                if _funding_cost_usd is not None:
                    _net_pnl_usd -= _funding_cost_usd
                memory.record_close({
                    "coin": coin, "side": side,
                    "entry_px": entry_px, "exit_px": fill_px,
                    "size_coin": abs(szi), "notional_usd": round(_notional_entry, 4),
                    "spot_pct": out["spot_pct"],
                    "realized_pnl_pct": out["realized_pnl_pct"],   # leveraged, net fees
                    "realized_pnl_usd": round(_net_pnl_usd, 4),
                    "gross_pnl_usd": round(_gross_pnl_usd, 4),
                    "fee_usd": round(_fee_usd, 4),
                    "leverage": leverage,
                    "closed_at": _closed_at,
                    # forward-backtest fields:
                    "entry_time": _entry_time,
                    "hold_minutes": _hold_min,
                    "signals_at_entry": _ec.get("signals") or {},
                    "enforcement_at_entry": _ec.get("enforcement") or {},
                    "forced_override": _ec.get("forced_override"),
                    # execution-quality + regime (the audit data items a/c/d):
                    "entry_slip_bps": _ec.get("entry_slip_bps"),
                    "exit_slip_bps": (round((((fill_px - mid_price) / mid_price * 1e4)
                                             * (1 if is_long else -1)) * -1, 1)
                                      if (fill_px and mid_price) else None),
                    "regime_at_entry": _ec.get("regime"),
                    "is_hip3": ":" in coin,
                    # funding carry: rate_hr × hold_hrs × notional × side (long pays
                    # when rate>0). Estimate (entry-rate held constant over the hold).
                    "funding_cost_usd": _funding_cost_usd,
                })
            except Exception as _rc_e:
                logger.error(f"[outcome-store] record_close failed for {coin}: {_rc_e}",
                             exc_info=True)
                # Loud alert — a lost close row means realized PnL is missing
                # from the outcome store (win-rate / payoff / RoR stats all
                # undercount). Previously this was silently swallowed as a
                # warning, which is how PURR's external SL close went
                # unnoticed until manual reconciliation.
                try:
                    from hermes_trader import notify
                    notify.send_text(
                        f"⚠️ record_close 失败: {coin}\n"
                        f"错误: {_rc_e}\n"
                        f"请立即检查 outcome store 完整性",
                        category="risk")
                except Exception:
                    pass
            # Loss cooldown: a losing close arms an extended re-entry block on
            # this coin (config `loss_cooldown_min`, 0 = off). Anti-revenge rule:
            # TON was churned 3x in one day because the standard cooldown expired
            # and the AI re-bought the same falling name each time.
            if out["realized_pnl_pct"] < 0:
                try:
                    lc_min = float(cfg_get("loss_cooldown_min", config=read_agent_config()))
                    if lc_min > 0:
                        until = int(time.time() * 1000 + lc_min * 60_000)
                        memory.set_loss_cooldown(coin, until)
                        logger.info(f"[executor] loss cooldown armed on {coin}: "
                                    f"{lc_min:.0f}min (closed {out['realized_pnl_pct']:.2f}%)")
                except Exception as e:
                    logger.warning(f"[executor] loss-cooldown arm failed for {coin}: {e}")
    return out


def retry_pending_sl(retry_interval: int = 15) -> None:
    """Retry server-side SL placement for positions that failed previously.

    Called every scan cycle; retries at most every `retry_interval` seconds.

    CRITICAL: a position whose backup SL is missing runs with NO exchange-side
    stop during a process crash / watchdog restart window. We therefore NEVER
    give up: the backoff is capped at 5 minutes (rather than the entry being
    dropped) and a loud error is emitted on every subsequent failure so an
    operator can intervene.
    """
    from hermes_trader.client.exchange import place_hl_trigger_order
    now = time.time()
    # Cap the backoff at 5 minutes so a sustained outage retries promptly on
    # recovery while avoiding tight retry loops / rate-limit amplification.
    max_backoff = 300
    for coin in list(_pending_sl_retries.keys()):
        entry = _pending_sl_retries[coin]
        attempt = entry.get("retry_count", 0)
        backoff = min(retry_interval * max(1, attempt), max_backoff)
        if now - entry["last_attempt"] < backoff:
            continue
        entry["retry_count"] = attempt + 1
        entry["last_attempt"] = now
        try:
            res = place_hl_trigger_order(
                entry["is_buy"], entry["size"], entry["sl_px"], "sl", entry["coin"]
            )
            if res.get("ok"):
                logger.info(f"[executor] Pending SL retry SUCCEEDED for {coin} "
                            f"after {entry['retry_count']} attempts")
                # Persist the retried SL's oid/px/size on the tracker.
                set_bracket(coin, entry.get("side", "long" if entry["is_buy"] else "short"),
                            sl_oid=res.get("order_id"),
                            sl_px=entry["sl_px"], sl_size=entry["size"])
                del _pending_sl_retries[coin]
            else:
                # NEVER drop. Loud error each time so the naked position is
                # visible; capped backoff prevents log/rate-limit flooding.
                logger.error(
                    f"[executor] Pending SL STILL MISSING for {coin} "
                    f"(attempt {entry['retry_count']}, next retry in {backoff}s) — "
                    f"position has NO server-side stop, manual intervention required"
                )
        except Exception as e:
            logger.error(
                f"[executor] Pending SL retry error for {coin} "
                f"(attempt {entry['retry_count']}): {e} — will keep retrying"
            )
