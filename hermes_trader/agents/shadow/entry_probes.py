"""Entry-path shadow/counterfactual audit recorders.

Moved verbatim (behavior-preserving) from ``agents/executor.py`` in the
P1-1 executor decomposition. Each recorder appends a best-effort JSONL line
(via the shared rotating ``hermes_trader.shadow_log`` writer) describing a
counter-factual that the LIVE decision deliberately does not act on. None of
these ever place an order, change the live block decision, or raise into the
trade hot path.

Clusters:
  * pullback-long bypass shadow
  * generic risk-tuning shadow (four proposed rule changes, ``rule`` tag)
  * per-coin regime probe (sampled at the runner entry gate)
  * short-only shadow (allow_shorts=false counter-factual EV)
  * early-breakout first-leg shadow (pure predicate + recorder)
"""
from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)


_PULLBACK_SHADOW_FILE = os.environ.get(
    "HERMES_PULLBACK_SHADOW_FILE",
    os.path.expanduser("~/.hermes-trading/pullback_shadow.jsonl"),
)


def _record_pullback_shadow(*, coin: str, side: str, score: float,
                            conf: float, slow_count: int,
                            rsi4h: Any, extension_atr: Any,
                            entry_px: float, trace_id: str = "",
                            macro_regime: str = "") -> None:
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
        # Audit 2026-09-06 (E2): macro regime (up/chop/neutral/down) recorded so
        # reconciliation can split outcomes by macro regime.
        "macro_regime": macro_regime or "",
        "outcome": None,  # filled by reconciliation script
        "exit_px": None,
        "pnl_usd": None,
    }
    # Audit 2026-09-06 (F2): routed through the shared shadow_log writer
    # (size-based rotation + write-failure metric).
    from hermes_trader.shadow_log import append_jsonl

    if append_jsonl(_PULLBACK_SHADOW_FILE, rec, stream="pullback"):
        logger.info(f"[executor] pullback-long SHADOW recorded for {coin} "
                    f"-> {_PULLBACK_SHADOW_FILE}")


# ── Risk-tuning shadow audit (2026-09-10 ADA/DOT/ZEC follow-ups) ───────────
# Four proposed behaviour changes are run in SHADOW first (the 18-trade
# sample is too small to calibrate on live): (4) a breakout-path minimum
# composite, (5) a per-coin 24h repeat / consecutive-loss cooldown, (6) an
# ATR/score-driven leverage tier, and (3) wider stop / lower breakeven. When
# enabled, every candidate that WOULD be blocked / de-levered / re-armed by
# the proposed rule is appended to ONE JSONL with ``rule`` discriminator,
# while the live decision is unchanged. A reconciliation pass over this feed
# decides which rule is safe to flip to enforce. Mirrors the pullback shadow
# (same shared rotating writer, never raises into the trade path).
_RISK_TUNING_SHADOW_FILE = os.environ.get(
    "HERMES_RISK_TUNING_SHADOW_FILE",
    os.path.expanduser("~/.hermes-trading/risk_tuning_shadow.jsonl"),
)


def _record_risk_tuning_shadow(*, rule: str, coin: str, side: str,
                               would: str, detail: dict[str, Any],
                               trace_id: str = "") -> None:
    """Best-effort append a risk-tuning shadow verdict.

    ``would`` is the counter-factual action ("block" / "deleverage" /
    "rearm_stop"); ``detail`` carries the observed vs threshold values needed
    for offline reconciliation. Never raises into the entry hot path.
    """
    from datetime import datetime, timezone
    rec = {
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "trace_id": trace_id,
        "rule": rule,
        "coin": coin,
        "side": side,
        "would": would,
        "detail": detail,
        "outcome": None,      # filled by reconciliation
        "exit_px": None,
        "pnl_usd": None,
    }
    from hermes_trader.shadow_log import append_jsonl

    if append_jsonl(_RISK_TUNING_SHADOW_FILE, rec, stream="risk_tuning"):
        logger.info(f"[executor] risk-tuning SHADOW[{rule}] for {coin}: "
                    f"would={would} -> {_RISK_TUNING_SHADOW_FILE}")


_SHORT_ONLY_SHADOW_FILE = os.environ.get(
    "HERMES_SHORT_ONLY_SHADOW_FILE",
    os.path.expanduser("~/.hermes-trading/short_only_shadow.jsonl"),
)


def _record_per_coin_regime_probe(analysis: dict[str, Any],
                                  config: dict[str, Any],
                                  *, runner_block: str) -> None:
    """探针前移：在 runner entry gate 处采样 per-coin regime 影子记录。

    该探针原先挂在 risk_gates.eval_all_gates 里，但 ~86%（近几日 98.5%）的
    候选在到达 eval_all_gates 之前就被 runner gate 拦掉了，导致影子样本
    积累速率只有 3~29 行/天，凑够可判定的 demote 样本要按年计。挪到这里后
    覆盖 100% 的已评估候选，代价是样本里混入大量不会成交的候选 —— 用
    detail.runner_block 区分（空串 = 通过 runner gate 的真候选）。

    Shadow-only，永不抛异常、永不改变执行决策。
    """
    try:
        from hermes_trader.agents.market_regime import detect_regime_with_score
        from hermes_trader.agents.per_coin_regime_shadow import record_per_coin_regime_shadow
        coin = analysis.get("coin") or ""
        regime, score = detect_regime_with_score(coin)
        record_per_coin_regime_shadow(
            coin=coin,
            side=(analysis.get("side") or "long").lower(),
            confidence=analysis.get("confidence"),
            composite_score=analysis.get("composite_score"),
            market_regime_result={"regime": regime, "trend_score": score,
                                  "via": "probe:runner_gate"},
            analysis=analysis, config=config,
            trace_id=str(analysis.get("id") or ""),
            extra_detail={"runner_block": runner_block})
    except Exception:
        pass


def _record_short_only_shadow(analysis: dict[str, Any], gate: dict[str, Any],
                              *, reason: str) -> None:
    """Record a short candidate that the operator's allow_shorts=false switch
    is suppressing, so the counter-factual EV of re-enabling shorts can be
    measured before flipping the live switch.

    Shadow-only: writes a JSONL line, never places an order and never changes
    the live block decision. Captures everything the offline reconciliation
    needs — the model conviction, composite, structure flags, signal price and
    the short thresholds the candidate would otherwise face — plus the macro
    (BTC proxy) regime and the coin's OWN 1h regime/score so the BTC-proxy
    mismatch on the short side is auditable. Best-effort: never raises.
    """
    try:
        from datetime import datetime, timezone
        coin = analysis.get("coin") or ""

        def _f(v):
            try:
                x = float(v)
                return x if x == x else None
            except (TypeError, ValueError):
                return None

        entry_px = (_f(analysis.get("mid")) or _f(analysis.get("price"))
                    or _f(analysis.get("entry_px")))
        macro_regime = macro_score = own_regime = own_score = None
        try:
            from hermes_trader.agents.market_regime import detect_own_regime_with_score, detect_regime_with_score
            macro_regime, macro_score = detect_regime_with_score(coin)
            own_regime, own_score = detect_own_regime_with_score(coin)
        except Exception:
            try:
                from hermes_trader.metrics import SWALLOWED_ERRORS
                SWALLOWED_ERRORS.labels(func="regime_detection").inc()
            except Exception:
                pass
            logger.warning("[executor] regime detection failed for %s", coin, exc_info=True)
        rec = {
            "timestamp": datetime.now(timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ"),
            "trace_id": str(analysis.get("id") or ""),
            "rule": "short_only",
            "coin": coin,
            "side": "short",
            "would": "admit_if_shorts_enabled",
            "block_reason": reason,
            "detail": {
                "confidence": _f(analysis.get("ai_confidence_raw")
                                 or analysis.get("confidence")),
                "composite_score": _f(analysis.get("composite_score")),
                "entry_px": entry_px,
                "rsi4h": _f(analysis.get("rsi4h")),
                "adx4h": _f(analysis.get("adx4h")),
                "atr4h": _f(analysis.get("atr4h")),
                "volume_spike": bool(analysis.get("volume_spike_fired")),
                "breakout": bool(analysis.get("breakout_fired")),
                "burst": bool(analysis.get("momentum_burst_fired")),
                "downtrend": bool(analysis.get("downtrend_momentum_fired")),
                "slow_burn_count": int(analysis.get("slow_burn_count", 0) or 0),
                "whale": bool(analysis.get("whale_signal")),
                "min_short_confidence": _f(gate.get("min_short_confidence")),
                "min_short_composite": _f(gate.get("min_short_composite")),
                "macro_regime": macro_regime,
                "macro_trend_score": (round(macro_score, 3)
                                      if macro_score is not None else None),
                "own_1h_regime": own_regime,
                "own_1h_score": (round(own_score, 3)
                                 if own_score is not None else None),
            },
            "outcome": None,
            "exit_px": None,
            "pnl_usd": None,
        }
        from hermes_trader.shadow_log import append_jsonl
        path = str(
            (gate.get("short_only_shadow") or {}).get("shadow_log_path")
            or "").strip() or _SHORT_ONLY_SHADOW_FILE
        if append_jsonl(path, rec, stream="short_only"):
            logger.info(
                f"[runner_gate] short-only SHADOW for {coin}: "
                f"conf={rec['detail']['confidence']} score={rec['detail']['composite_score']} "
                f"down={int(rec['detail']['downtrend'])} macro={macro_regime} "
                f"own1h={own_regime} -> {path}")
    except Exception as e:  # never break the entry hot path
        logger.debug(f"[runner_gate] short-only shadow record failed: {e}")


_EARLY_BREAKOUT_SHADOW_FILE = os.environ.get(
    "HERMES_EARLY_BREAKOUT_SHADOW_FILE",
    os.path.expanduser("~/.hermes-trading/early_breakout_shadow.jsonl"),
)


def _early_breakout_candidate(analysis: dict[str, Any], gate: dict[str, Any],
                              *, fresh_impulse: bool, score: float,
                              rsi4h: Any) -> tuple[bool, str]:
    """Pure predicate: is this LONG a *fresh volume breakout in its first leg*
    that the strict confidence/structure gate is about to reject?

    The NEAR 2026-09-11 case: at 12:30 the coin printed a 5.6x-volume breakout
    but composite was only 12.6 / confidence 0.60, so it was rejected; by the
    time structure confirmed at ~14:10 the entry was 7.4% higher and then it
    had to clear the anti-chase veto. This predicate identifies exactly those
    first-leg impulses so their counter-factual EV (early half-size + tight
    ATR stop) can be measured BEFORE relaxing the live gate.

    Conditions (all required, deliberately conservative):
      * a real fresh impulse (breakout, or volume+burst),
      * NOT already extended (4h extension under a cap, RSI not overbought),
      * composite below the live admission score (otherwise it's admitted
        anyway and needs no early lane).
    Returns (is_candidate, reason). No side effects.
    """
    cfg = gate.get("early_breakout_shadow") or {}
    if not bool(cfg.get("shadow_mode", False)):
        return False, "shadow_off"
    if not fresh_impulse:
        return False, "no_fresh_impulse"
    ext_cap = float(cfg.get("max_extension_atr", 1.5))
    extension = analysis.get("extension_atr")
    if extension is None:
        # fall back to a carried 4h extension if present, else treat unknown
        extension = analysis.get("atr_extension_4h")
    try:
        if extension is not None and float(extension) > ext_cap:
            return False, f"extended {float(extension):.2f}>{ext_cap}"
    except (TypeError, ValueError):
        pass
    ob = float(gate.get("rsi_overbought", 75.0))
    try:
        if rsi4h is not None and float(rsi4h) > ob:
            return False, f"rsi {float(rsi4h):.0f}>{ob:.0f}"
    except (TypeError, ValueError):
        pass
    min_score = float(gate.get("min_composite", 30.0))
    if score >= min_score:
        return False, "score_already_admittable"
    return True, "fresh_first_leg_low_score"


def _record_early_breakout_shadow(analysis: dict[str, Any],
                                  gate: dict[str, Any], *, block: str,
                                  fresh_impulse: bool, score: float,
                                  gate_conf: float) -> None:
    """Record one early-breakout counter-factual. Shadow-only; never places an
    order and never changes the live block. Carries the early-lane sizing/stop
    parameters the offline grader replays (fractional notional + tight ATR
    stop). Best-effort: never raises."""
    try:
        from datetime import datetime, timezone

        def _f(v):
            try:
                x = float(v)
                return x if x == x else None
            except (TypeError, ValueError):
                return None

        coin = analysis.get("coin") or ""
        cfg = gate.get("early_breakout_shadow") or {}
        entry_px = (_f(analysis.get("mid")) or _f(analysis.get("price"))
                    or _f(analysis.get("entry_px")))
        atr4h = _f(analysis.get("atr4h"))
        atr_pct = (_f(analysis.get("atr4h_pct"))
                   or ((atr4h / entry_px * 100.0)
                       if atr4h and entry_px else None))
        stop_mult = float(cfg.get("early_stop_atr_mult", 1.2))
        size_frac = float(cfg.get("early_size_fraction", 0.5))
        macro_regime = macro_score = None
        try:
            from hermes_trader.agents.market_regime import detect_regime_with_score
            macro_regime, macro_score = detect_regime_with_score(coin)
        except Exception:
            pass
        rec = {
            "timestamp": datetime.now(timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ"),
            "trace_id": str(analysis.get("trace_id") or analysis.get("id") or ""),
            "rule": "early_breakout_entry",
            "coin": coin,
            "side": "long",
            "would": "early_half_size_if_lane_enabled",
            "live_block": block,
            "detail": {
                "confidence": _f(analysis.get("ai_confidence_raw")
                                 or analysis.get("confidence"))
                              or gate_conf,
                "composite_score": score,
                "entry_px": entry_px,
                "rsi4h": _f(analysis.get("rsi4h")),
                "adx4h": _f(analysis.get("adx4h")),
                "atr4h_pct": round(atr_pct, 4) if atr_pct is not None else None,
                "volume_spike": bool(analysis.get("volume_spike_fired")),
                "breakout": bool(analysis.get("breakout_fired")),
                "burst": bool(analysis.get("momentum_burst_fired")),
                "uptrend": bool(analysis.get("uptrend_momentum_fired")),
                "slow_burn_count": int(analysis.get("slow_burn_count", 0) or 0),
                "fresh_impulse": bool(fresh_impulse),
                "early_size_fraction": size_frac,
                "early_stop_atr_mult": stop_mult,
                "min_composite_live": float(gate.get("min_composite", 30.0)),
                "min_confidence_live": float(gate.get("min_confidence", 0.70)),
                "macro_regime": macro_regime,
                "macro_trend_score": (round(macro_score, 3)
                                      if macro_score is not None else None),
            },
            "outcome": None,
            "exit_px": None,
            "pnl_usd": None,
        }
        from hermes_trader.shadow_log import append_jsonl
        path = str(cfg.get("shadow_log_path") or "").strip() \
            or _EARLY_BREAKOUT_SHADOW_FILE
        if append_jsonl(path, rec, stream="early_breakout"):
            logger.info(
                f"[runner_gate] early-breakout SHADOW for {coin}: "
                f"conf={rec['detail']['confidence']} score={score:.0f} "
                f"fresh={int(fresh_impulse)} block='{block[:40]}' -> {path}")
    except Exception as e:
        logger.debug(f"[runner_gate] early-breakout shadow record failed: {e}")
