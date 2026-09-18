"""ATR-regime stop-width calibration gray-release (off / shadow / enforce).

Moved verbatim (behavior-preserving) from ``agents/executor.py`` in the
P1-1 executor decomposition. Sizing-only adjustment: it scales the stop
budget used to divide risk into notional, never the byte-aligned core_stop,
so the live DSL stop and the post-fill drift assertion are untouched.

Modes:
  off     — pass-through; the legacy binary spike breaker still applies.
  shadow  — calibrated factor computed + logged, sizing stop stays raw.
  enforce — calibrated factor applied; legacy spike breaker suppressed.
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any

from hermes_trader.agents.config_store import report_legacy_mode_drift

logger = logging.getLogger(__name__)

_ATR_CALIB_MODES = ("off", "shadow", "enforce")


def _atr_calib_config(config: dict[str, Any]) -> dict[str, Any]:
    """Resolve the atr_regime_calibration block: merged agent-config, then an
    env override for the mode (gray-release flip without a file write).
    Invalid modes fall back to off."""
    blk = config.get("atr_regime_calibration") or {}
    file_mode = str(blk.get("mode") or "").strip().lower()
    if file_mode not in _ATR_CALIB_MODES:
        file_mode = "off"
    env_mode = str(os.environ.get("HERMES_ATR_REGIME_CALIB_MODE") or "").strip().lower()
    mode = env_mode if env_mode else file_mode
    if mode not in _ATR_CALIB_MODES:
        mode = "off"
    report_legacy_mode_drift(
        env_name="HERMES_ATR_REGIME_CALIB_MODE",
        env_mode=env_mode,
        file_key="atr_regime_calibration.mode",
        file_mode=file_mode,
        valid_modes=_ATR_CALIB_MODES,
    )
    return {"mode": mode, "block": blk}


def _atr_calib_shadow_path(blk: dict[str, Any]) -> str:
    """Resolve the ATR calibration shadow JSONL path (config → env → default)."""
    return str(blk.get("shadow_log_path") or "").strip() or os.environ.get(
        "HERMES_ATR_REGIME_CALIB_SHADOW_FILE",
        os.path.expanduser("~/.hermes-trading/atr_regime_calib_shadow.jsonl"),
    )


def _atr_calib_record_shadow(rec: dict[str, Any], path: str) -> None:
    """Best-effort append an ATR calibration record to the JSONL."""
    # Audit 2026-09-06 (F2): routed through the shared shadow_log writer
    # (daily + size-based rotation, write-failure metric). The helper never
    # raises, so the trade hot path is unaffected.
    from hermes_trader.shadow_log import append_jsonl

    append_jsonl(path, rec, stream="atr_calib")


def _atr_calib_metric(mode: str, outcome: str) -> None:
    """Best-effort Prometheus counter (roadmap R7 registration)."""
    try:
        from hermes_trader import metrics
        metrics.ATR_REGIME_CALIB_OBSERVATIONS.labels(mode=mode, outcome=outcome).inc()
    except Exception:
        pass


def _atr_calib_apply(
    *,
    effective_stop_pct: float,
    atr_pct: float,
    atr_hist_mean_pct: float,
    config: dict[str, Any],
    coin: str,
    core_stop: float,
    regime_label: str,
) -> dict[str, Any]:
    """Apply (enforce) or observe (shadow) the ATR-regime stop-width factor.

    Sizing-only: ``effective_stop_pct`` already excludes core_stop alignment.
    Returns the adjusted effective stop pct plus a breakdown (regime/ratio/
    factor/raw/calibrated). In off mode this is a pure pass-through and never
    touches the JSONL; in shadow/enforce the calibrated width is logged; only
    enforce actually changes the returned stop pct.
    """
    raw = float(effective_stop_pct)
    cfg = _atr_calib_config(config)
    mode = cfg["mode"]
    if mode == "off":
        return {
            "mode": mode,
            "regime": "normal",
            "ratio": 0.0,
            "factor": 1.0,
            "raw_stop_pct": raw,
            "calibrated_stop_pct": raw,
            "would_change": False,
            "effective_stop_pct": raw,
        }

    from hermes_trader.agents.sizing import atr_regime_calibration

    cal = atr_regime_calibration(
        atr_pct=atr_pct,
        atr_hist_mean_pct=atr_hist_mean_pct,
        params=cfg["block"],
    )
    factor = float(cal["factor"])
    calibrated = raw * factor
    would_change = abs(factor - 1.0) > 1e-9
    try:
        _atr_calib_record_shadow({
            "ts": int(time.time() * 1000),
            "mode": mode,
            "coin": coin,
            "atr_pct": round(float(atr_pct), 4),
            "atr_hist_mean_pct": round(float(atr_hist_mean_pct), 4),
            "ratio": round(float(cal["ratio"]), 4),
            "vol_regime": cal["regime"],
            "factor": round(factor, 4),
            "core_stop_pct": round(float(core_stop), 4),
            "trend_regime": str(regime_label),
            "raw_stop_pct": round(raw, 4),
            "calibrated_stop_pct": round(calibrated, 4),
            "would_change": bool(would_change),
        }, _atr_calib_shadow_path(cfg["block"]))
    except Exception as e:
        logger.debug(f"[atr-calib] shadow record failed for {coin}: {e}")
    _atr_calib_metric(mode, "applied" if would_change else "no_change")

    return {
        "mode": mode,
        "regime": cal["regime"],
        "ratio": float(cal["ratio"]),
        "factor": factor,
        "raw_stop_pct": raw,
        "calibrated_stop_pct": calibrated,
        "would_change": would_change,
        "effective_stop_pct": calibrated if mode == "enforce" else raw,
    }
