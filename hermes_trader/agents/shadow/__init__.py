"""Shadow / gray-release / durable-audit recorders extracted from executor.

P1-1 executor decomposition step ①: these best-effort, trade-path-safe
recorders previously lived inline in ``agents/executor.py``. They only write
JSONL / events / metrics and never place orders or change the live decision,
so they have no back-edge to executor's trading state. ``agents.executor``
re-imports every name below to preserve its public (and private) attribute
surface — including the two confidence-decay singletons — without behavior
change.
"""
from __future__ import annotations

from hermes_trader.agents.shadow.atr_calib import (
    _ATR_CALIB_MODES,
    _atr_calib_apply,
    _atr_calib_config,
    _atr_calib_metric,
    _atr_calib_record_shadow,
    _atr_calib_shadow_path,
)
from hermes_trader.agents.shadow.audit import (
    _FORCE_OVERRIDE_CONFIG_KEYS,
    _record_force_override_armed,
)
from hermes_trader.agents.shadow.confidence_decay import (
    _CONFIDENCE_DECAY_DEFAULT_HALFLIFE_S,
    _CONFIDENCE_DECAY_MODES,
    _CONFIDENCE_DECAY_ONSET_TTL_S,
    _apply_confidence_decay,
    _confidence_decay_age_s,
    _confidence_decay_config,
    _confidence_decay_lock,
    _confidence_decay_metric,
    _confidence_decay_onset,
    _confidence_decay_record_shadow,
    _confidence_decay_shadow_path,
    _reset_confidence_decay,
    _verdict_signature,
)
from hermes_trader.agents.shadow.entry_probes import (
    _EARLY_BREAKOUT_SHADOW_FILE,
    _PULLBACK_SHADOW_FILE,
    _RISK_TUNING_SHADOW_FILE,
    _SHORT_ONLY_SHADOW_FILE,
    _early_breakout_candidate,
    _record_early_breakout_shadow,
    _record_per_coin_regime_probe,
    _record_pullback_shadow,
    _record_risk_tuning_shadow,
    _record_short_only_shadow,
)
from hermes_trader.agents.shadow.sizing_v2 import (
    _SIZING_V2_MODES,
    _sizing_v2_config,
    _sizing_v2_record_shadow,
    _sizing_v2_shadow_path,
)

__all__ = (
    # audit
    "_FORCE_OVERRIDE_CONFIG_KEYS",
    "_record_force_override_armed",
    # entry probes
    "_PULLBACK_SHADOW_FILE",
    "_record_pullback_shadow",
    "_RISK_TUNING_SHADOW_FILE",
    "_record_risk_tuning_shadow",
    "_SHORT_ONLY_SHADOW_FILE",
    "_record_per_coin_regime_probe",
    "_record_short_only_shadow",
    "_EARLY_BREAKOUT_SHADOW_FILE",
    "_early_breakout_candidate",
    "_record_early_breakout_shadow",
    # atr calibration
    "_ATR_CALIB_MODES",
    "_atr_calib_config",
    "_atr_calib_shadow_path",
    "_atr_calib_record_shadow",
    "_atr_calib_metric",
    "_atr_calib_apply",
    # sizing v2
    "_SIZING_V2_MODES",
    "_sizing_v2_config",
    "_sizing_v2_shadow_path",
    "_sizing_v2_record_shadow",
    # confidence decay
    "_CONFIDENCE_DECAY_MODES",
    "_CONFIDENCE_DECAY_DEFAULT_HALFLIFE_S",
    "_CONFIDENCE_DECAY_ONSET_TTL_S",
    "_confidence_decay_lock",
    "_confidence_decay_onset",
    "_reset_confidence_decay",
    "_confidence_decay_config",
    "_verdict_signature",
    "_confidence_decay_age_s",
    "_confidence_decay_shadow_path",
    "_confidence_decay_record_shadow",
    "_confidence_decay_metric",
    "_apply_confidence_decay",
)
