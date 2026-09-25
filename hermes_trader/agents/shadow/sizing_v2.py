"""Sizing v2 gray-release config/path/recorder (off / shadow / enforce).

Moved verbatim (behavior-preserving) from ``agents/executor.py`` in the
P1-1 executor decomposition. Only the mode accessor, shadow-path resolver and
JSONL recorder live here; the actual v1-vs-v2 sizing math and enforce path
stays inline in ``executor.maybe_execute`` (it depends on live executor
state/helpers). Defaults off; shadow only logs and never changes size.
"""
from __future__ import annotations

import os
from typing import Any

from hermes_trader.agents.config_store import report_legacy_mode_drift

_SIZING_V2_MODES = ("off", "shadow", "enforce")


def _sizing_v2_config(config: dict[str, Any]) -> dict[str, Any]:
    """Resolve the sizing v2 gray-release mode.

    Sources in priority order: env ``HERMES_SIZING_V2_MODE`` (gray-release
    flip without a file write), then ``atr_risk_sizing.sizing_v2_mode`` in
    the merged agent config; invalid/missing values fall back to the PRODUCTION
    DEFAULT ``enforce`` (PRM-06 收口：v2 按止损宽度 SSOT 真实 sizing，不再退回
    v1 的 1.0% 名义口径）。要显式回退可在配置里写 ``"off"``。
    The legacy boolean ``sizing_v2_enabled`` was retired in P1-4 Phase 1
    step 5 and is ignored."""
    blk = config.get("atr_risk_sizing") or {}
    blk_mode = str(blk.get("sizing_v2_mode") or "").strip().lower()
    file_mode = blk_mode if blk_mode in _SIZING_V2_MODES else "enforce"
    env_mode = str(os.environ.get("HERMES_SIZING_V2_MODE") or "").strip().lower()
    mode = env_mode if env_mode else file_mode
    if mode not in _SIZING_V2_MODES:
        mode = "enforce"
    report_legacy_mode_drift(
        env_name="HERMES_SIZING_V2_MODE",
        env_mode=env_mode,
        file_key="atr_risk_sizing.sizing_v2_mode",
        file_mode=file_mode,
        valid_modes=_SIZING_V2_MODES,
    )
    return {"mode": mode, "block": blk}
