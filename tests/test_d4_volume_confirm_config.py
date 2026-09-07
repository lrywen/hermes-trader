"""Audit 2026-09-06 (D4): TA pre-filter volume-surge confirmation is driven by
the canonical ``volume_confirm`` block instead of hardcoded literals.

Covers:
  * canonical defaults equal the old hardcoded literals (min_ratio=1.2,
    lookback=20) so unconfigured behaviour is byte-identical;
  * cfg_get resolves the leaves and env overrides land;
  * the block is known to the schema (strict_keys) and exposed in the full
    config view;
  * the runtime reader (_volume_confirm_params) picks up an operator overlay
    and safely falls back to the literals on bad / missing values;
  * the pure gate (_check_volume_confirm) honours parametrised mult/lookback
    and keeps the minimum-bars fail-closed guard.
"""
from __future__ import annotations

from hermes_trader.agents import config_store
from hermes_trader.agents.config_store import (
    CANONICAL_DEFAULTS,
    cfg_get,
    read_agent_config,
)
from hermes_trader.agents import ta_filter


# ── canonical registration: defaults mirror the old hardcoded literals ──────

def test_volume_confirm_canonical_defaults_match_hardcoded_literals():
    vc = CANONICAL_DEFAULTS["volume_confirm"]
    assert vc["min_ratio"] == 1.2
    assert vc["lookback"] == 20
    # Module fallback constants stay in lock-step.
    assert ta_filter._VOLUME_CONFIRM_MIN_RATIO_DEFAULT == 1.2
    assert ta_filter._VOLUME_CONFIRM_LOOKBACK_DEFAULT == 20


def test_volume_confirm_cfg_get_resolves_defaults():
    assert cfg_get("volume_confirm.min_ratio", config={}) == 1.2
    assert cfg_get("volume_confirm.lookback", config={}) == 20


def test_volume_confirm_env_override(monkeypatch):
    monkeypatch.setenv("HERMES_CFG_VOLUME_CONFIRM__MIN_RATIO", "1.5")
    monkeypatch.setenv("HERMES_CFG_VOLUME_CONFIRM__LOOKBACK", "30")
    assert cfg_get("volume_confirm.min_ratio", config={}) == 1.5
    assert cfg_get("volume_confirm.lookback", config={}) == 30


def test_volume_confirm_block_known_to_schema_strict_keys():
    from hermes_trader.agents.config_schema import validate_config_updates
    errors = validate_config_updates(
        {"volume_confirm": {"min_ratio": 1.5, "lookback": 30}},
        strict_keys=True,
    )
    assert errors == [], errors


def test_volume_confirm_exposed_in_full_config_view():
    cfg = read_agent_config()
    assert cfg["volume_confirm"]["min_ratio"] == 1.2
    assert cfg["volume_confirm"]["lookback"] == 20


# ── runtime reader: overlay wins, bad values fall back, never raises ────────

def test_volume_confirm_params_picks_up_overlay():
    # Direct call against an injected config: cfg_get reads the merged config,
    # so pass an overlay via a monkeypatched config_store read.
    cfg = {"volume_confirm": {"min_ratio": 1.8, "lookback": 25}}
    orig = config_store.read_agent_config
    config_store.read_agent_config = lambda *a, **k: {
        **CANONICAL_DEFAULTS, **cfg}
    try:
        r, lb = ta_filter._volume_confirm_params()
    finally:
        config_store.read_agent_config = orig
    assert r == 1.8
    assert lb == 25


def test_volume_confirm_params_bad_values_fall_back(monkeypatch):
    # A non-positive ratio and a sub-1 lookback must be ignored in favour of
    # the historical literals (and never raise).
    monkeypatch.setattr(
        config_store, "read_agent_config",
        lambda *a, **k: {"volume_confirm": {"min_ratio": 0.0, "lookback": 0}},
    )
    r, lb = ta_filter._volume_confirm_params()
    assert r == 1.2
    assert lb == 20


# ── pure gate: parametrised mult / lookback + fail-closed short history ─────

def _candles_with_vols(vols):
    """Build minimal candle dicts carrying only the 'v' field the gate reads."""
    return [{"v": float(v), "T": i} for i, v in enumerate(vols)]


def test_check_volume_confirm_default_threshold():
    # 20 bars at volume 100, last bar at 1.2x average -> exactly at threshold.
    candles = _candles_with_vols([100.0] * 20 + [120.0])
    assert ta_filter._check_volume_confirm(candles) is True
    # Just below threshold -> not confirmed.
    candles[-1]["v"] = 119.0
    assert ta_filter._check_volume_confirm(candles) is False


def test_check_volume_confirm_respects_custom_mult():
    candles = _candles_with_vols([100.0] * 20 + [140.0])
    assert ta_filter._check_volume_confirm(candles, mult=1.5) is False
    assert ta_filter._check_volume_confirm(candles, mult=1.3) is True


def test_check_volume_confirm_respects_custom_lookback():
    # lookback=5: average over prior 5 bars (all 100), last at 1.3x.
    candles = _candles_with_vols([100.0] * 5 + [130.0])
    assert ta_filter._check_volume_confirm(candles, mult=1.2, lookback=5) is True
    # Fewer than lookback+1 bars -> fail-closed False.
    assert ta_filter._check_volume_confirm(candles, mult=1.2, lookback=10) is False


def test_check_volume_confirm_zero_average_passes():
    # A flat-zero average (dead market history) must not divide into a block.
    candles = _candles_with_vols([0.0] * 20 + [1.0])
    assert ta_filter._check_volume_confirm(candles) is True
