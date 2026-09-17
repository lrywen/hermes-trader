"""P1-4 Phase 3: register the three production EXTRA keys.

The live ``.agent-config.json`` carried three keys absent from
``CANONICAL_DEFAULTS`` — they round-tripped but were flagged ``unknown
key`` by every strict whole-view validation:

  * ``leverage_tier_shadow``     — volatility/score de-leverage arm
                                   (executor.py ``_lev_tier`` block);
  * ``per_coin_regime_shadow``   — macro x own 4h divergence shadow probe
                                   (per_coin_regime_shadow.py);
  * ``own_gap_demote_pct``       — own-4h gap demote threshold, read via
                                   cfg_get in risk_gates (0 disables).

Registering them canonically (defaults preserve the exact current
behaviour for a config without these keys) closes the last three
whole-view ``unknown key`` reports and puts the leaves behind the same
strict patch / range gates as every other tuning key.
"""

from hermes_trader.agents.config_schema import (
    _ConfigPatch,
    validate_config_updates,
)
from hermes_trader.agents.config_store import (
    CANONICAL_DEFAULTS,
    cfg_get,
    validate_config_dict,
)


# ── canonical registration: blocks + scalar ──────────────────────────

def test_phase3_leverage_tier_block_registered():
    block = CANONICAL_DEFAULTS["leverage_tier_shadow"]
    # shadow_mode=False: an absent block means the arm is inert (executor
    # only evaluates the tier when the block is present).
    assert block["shadow_mode"] is False
    assert set(block) == {
        "shadow_mode", "atr_pct_max", "min_composite", "low_leverage",
    }


def test_phase3_per_coin_regime_block_registered():
    block = CANONICAL_DEFAULTS["per_coin_regime_shadow"]
    assert block["shadow_mode"] is False
    assert set(block) == {
        "shadow_mode", "record_all_quadrants", "require_own_adx",
        "strong_own_score", "mid_own_score", "shadow_log_path",
    }


def test_phase3_own_gap_demote_scalar_registered():
    # 0.0 preserves current behaviour for a key-absent config (the risk
    # gate treats 0/missing as "overlay disabled").
    assert CANONICAL_DEFAULTS["own_gap_demote_pct"] == 0.0
    assert cfg_get("own_gap_demote_pct", config={}) == 0.0


# ── cfg_get resolution of nested leaves ───────────────────────────────

def test_phase3_nested_leaves_cfg_get_file_overlay():
    cfg = {
        "leverage_tier_shadow": {"atr_pct_max": 2.5},
        "per_coin_regime_shadow": {"require_own_adx": 25.0},
    }
    assert cfg_get("leverage_tier_shadow.atr_pct_max", config=cfg) == 2.5
    assert cfg_get("per_coin_regime_shadow.require_own_adx", config=cfg) == 25.0
    # Untouched leaves still resolve to canonical defaults.
    assert cfg_get("leverage_tier_shadow.low_leverage", config=cfg) == 5
    assert cfg_get(
        "per_coin_regime_shadow.record_all_quadrants", config=cfg) is True


# ── patch gate: production-shaped blocks accepted ─────────────────────

def test_phase3_patch_gate_accepts_production_blocks():
    errors = validate_config_updates({
        "leverage_tier_shadow": {
            "shadow_mode": False, "atr_pct_max": 3.5,
            "min_composite": 40, "low_leverage": 5,
        },
        "per_coin_regime_shadow": {
            "shadow_mode": True, "record_all_quadrants": True,
            "require_own_adx": 20, "strong_own_score": 0.65,
            "mid_own_score": 0.55,
            "shadow_log_path": "/data/per_coin_regime_shadow.jsonl",
        },
        "own_gap_demote_pct": 15,
    }, strict_keys=True)
    assert errors == [], errors


def test_phase3_patch_gate_accepts_partial_blocks():
    assert validate_config_updates(
        {"leverage_tier_shadow": {"shadow_mode": True}},
        strict_keys=True) == []
    assert validate_config_updates(
        {"per_coin_regime_shadow": {"shadow_log_path": "/tmp/x.jsonl"}},
        strict_keys=True) == []


# ── patch gate: unknown / mistyped / out-of-range leaves rejected ─────

def test_phase3_patch_gate_rejects_unknown_leaves():
    errors = validate_config_updates({
        "leverage_tier_shadow": {"bogus": 1},
        "per_coin_regime_shadow": {"bogus": 1},
    }, strict_keys=True)
    assert any("leverage_tier_shadow.bogus: unknown key" in e for e in errors)
    assert any("per_coin_regime_shadow.bogus: unknown key" in e for e in errors)


def test_phase3_patch_gate_rejects_bad_leaf_types_and_ranges():
    cases = [
        ({"leverage_tier_shadow": {"low_leverage": "boom"}},
         "leverage_tier_shadow.low_leverage"),
        ({"leverage_tier_shadow": {"shadow_mode": "yes"}},
         "leverage_tier_shadow.shadow_mode"),
        ({"leverage_tier_shadow": {"atr_pct_max": -1.0}},
         "leverage_tier_shadow.atr_pct_max"),
        ({"per_coin_regime_shadow": {"strong_own_score": 1.5}},
         "per_coin_regime_shadow.strong_own_score"),
        ({"per_coin_regime_shadow": {"require_own_adx": True}},
         "per_coin_regime_shadow.require_own_adx"),
        ({"own_gap_demote_pct": "15%"}, "own_gap_demote_pct"),
        ({"own_gap_demote_pct": -1.0}, "own_gap_demote_pct"),
    ]
    for patch, needle in cases:
        errors = validate_config_updates(patch, strict_keys=True)
        assert errors, patch
        assert any(needle in e for e in errors), (needle, errors)


# ── whole-view gate: the three keys are no longer unknown ─────────────

def test_phase3_whole_view_accepts_registered_keys():
    cfg = dict(CANONICAL_DEFAULTS)
    cfg["leverage_tier_shadow"] = {
        "shadow_mode": False, "atr_pct_max": 3.5,
        "min_composite": 40, "low_leverage": 5}
    cfg["per_coin_regime_shadow"] = {
        "shadow_mode": True, "record_all_quadrants": True,
        "require_own_adx": 20, "strong_own_score": 0.65,
        "mid_own_score": 0.55,
        "shadow_log_path": "/data/per_coin_regime_shadow.jsonl"}
    cfg["own_gap_demote_pct"] = 15
    errors = validate_config_dict(cfg, strict_keys=True)
    assert not [e for e in errors if "unknown key" in e and (
        "leverage_tier_shadow" in e or "per_coin_regime_shadow" in e
        or "own_gap_demote_pct" in e)], errors


# ── schema drift sentinel: _ConfigPatch fields track canonical ────────

def test_phase3_patch_defaults_match_canonical():
    for block_name in ("leverage_tier_shadow", "per_coin_regime_shadow"):
        canonical_keys = set(CANONICAL_DEFAULTS[block_name].keys())
        patch_keys = set(
            _ConfigPatch.model_fields[block_name].default_factory().keys())
        assert canonical_keys == patch_keys, (
            f"{block_name} drift: canonical={canonical_keys}, "
            f"patch={patch_keys}")
