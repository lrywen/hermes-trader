"""P1-4 Phase 3: register the production EXTRA keys.

The live ``.agent-config.json`` carried keys absent from
``CANONICAL_DEFAULTS`` — they round-tripped but were flagged ``unknown
key`` by every strict whole-view validation:

  * ``leverage_tier_shadow``     — volatility/score de-leverage arm
                                   (executor.py ``_lev_tier`` block);
  * ``own_gap_demote_pct``       — own-4h gap demote threshold, read via
                                   cfg_get in risk_gates (0 disables).

Registering them canonically (defaults preserve the exact current
behaviour for a config without these keys) closes the whole-view
``unknown key`` reports and puts the leaves behind the same strict
patch / range gates as every other tuning key.

2026-09-21 cleanup: the third key ``per_coin_regime_shadow`` (macro x own
4h divergence shadow probe) was retired together with its orphan
recorder/reconcile pipeline and removed here.
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

# ── canonical registration: block + scalar ───────────────────────────

def test_phase3_leverage_tier_block_registered():
    block = CANONICAL_DEFAULTS["leverage_tier_shadow"]
    # shadow_mode=False: an absent block means the arm is inert (executor
    # only evaluates the tier when the block is present).
    assert block["shadow_mode"] is False
    assert set(block) == {
        "shadow_mode", "atr_pct_max", "min_composite", "low_leverage",
    }


def test_phase3_own_gap_demote_scalar_registered():
    # Canonical default aligned to production (15.0); 0.0 remains the explicit
    # "overlay disabled" signal only when an operator sets it.
    assert CANONICAL_DEFAULTS["own_gap_demote_pct"] == 15.0
    assert cfg_get("own_gap_demote_pct", config={}) == 15.0


# ── cfg_get resolution of nested leaves ───────────────────────────────

def test_phase3_nested_leaves_cfg_get_file_overlay():
    cfg = {
        "leverage_tier_shadow": {"atr_pct_max": 2.5},
    }
    assert cfg_get("leverage_tier_shadow.atr_pct_max", config=cfg) == 2.5
    # Untouched leaves still resolve to canonical defaults.
    assert cfg_get("leverage_tier_shadow.low_leverage", config=cfg) == 5


# ── patch gate: production-shaped blocks accepted ─────────────────────

def test_phase3_patch_gate_accepts_production_blocks():
    errors = validate_config_updates({
        "leverage_tier_shadow": {
            "shadow_mode": False, "atr_pct_max": 3.5,
            "min_composite": 40, "low_leverage": 5,
        },
        "own_gap_demote_pct": 15,
    }, strict_keys=True)
    assert errors == [], errors


def test_phase3_patch_gate_accepts_partial_blocks():
    assert validate_config_updates(
        {"leverage_tier_shadow": {"shadow_mode": True}},
        strict_keys=True) == []


# ── patch gate: unknown / mistyped / out-of-range leaves rejected ─────

def test_phase3_patch_gate_rejects_unknown_leaves():
    errors = validate_config_updates({
        "leverage_tier_shadow": {"bogus": 1},
    }, strict_keys=True)
    assert any("leverage_tier_shadow.bogus: unknown key" in e for e in errors)


def test_phase3_patch_gate_rejects_bad_leaf_types_and_ranges():
    cases = [
        ({"leverage_tier_shadow": {"low_leverage": "boom"}},
         "leverage_tier_shadow.low_leverage"),
        ({"leverage_tier_shadow": {"shadow_mode": "yes"}},
         "leverage_tier_shadow.shadow_mode"),
        ({"leverage_tier_shadow": {"atr_pct_max": -1.0}},
         "leverage_tier_shadow.atr_pct_max"),
        ({"own_gap_demote_pct": "15%"}, "own_gap_demote_pct"),
        ({"own_gap_demote_pct": -1.0}, "own_gap_demote_pct"),
    ]
    for patch, needle in cases:
        errors = validate_config_updates(patch, strict_keys=True)
        assert errors, patch
        assert any(needle in e for e in errors), (needle, errors)


# ── whole-view gate: the keys are no longer unknown ──────────────────

def test_phase3_whole_view_accepts_registered_keys():
    cfg = dict(CANONICAL_DEFAULTS)
    cfg["leverage_tier_shadow"] = {
        "shadow_mode": False, "atr_pct_max": 3.5,
        "min_composite": 40, "low_leverage": 5}
    cfg["own_gap_demote_pct"] = 15
    errors = validate_config_dict(cfg, strict_keys=True)
    assert not [e for e in errors if "unknown key" in e and (
        "leverage_tier_shadow" in e
        or "own_gap_demote_pct" in e)], errors


# ── schema drift sentinel: _ConfigPatch fields track canonical ────────

def test_phase3_patch_defaults_match_canonical():
    block_name = "leverage_tier_shadow"
    canonical_keys = set(CANONICAL_DEFAULTS[block_name].keys())
    patch_keys = set(
        _ConfigPatch.model_fields[block_name].default_factory().keys())
    assert canonical_keys == patch_keys, (
        f"{block_name} drift: canonical={canonical_keys}, "
        f"patch={patch_keys}")
