"""P1-4 Phase 3 (batch 2): register the live nested shadow sub-blocks.

The production config carries five shadow sub-blocks that had no
canonical leaves and no nested validation spec, plus one sized leaf
that was spec-checked but missing its canonical default:

  * ``runner_entry_gate.breakout_score_floor``   (record-only shadow)
  * ``runner_entry_gate.early_breakout_shadow``  (record-only shadow)
  * ``runner_entry_gate.per_coin_cooldown``      (true=record, false=ENFORCE)
  * ``runner_entry_gate.short_only_shadow``      (record-only shadow)
  * ``dsl_exit.stop_tuning_shadow``              (live stop unchanged)
  * ``atr_risk_sizing.sizing_v2_cap_pct``        (spec existed, default missing)

The four record-only arms and stop_tuning_shadow default to
``shadow_mode=true`` (pure observation, never blocks) so registering a
default block for a previously key-absent config cannot introduce a new
ENFORCE block. ``per_coin_cooldown`` likewise defaults to true
(record-only); the production file explicitly sets it false (ENFORCE)
and the deep merge keeps that value. Per-coin ``coin_overrides`` maps
stay intentionally un-registered (free-form extension channel).
"""

from hermes_trader.agents.config_schema import (
    _ConfigPatch,
    validate_config_updates,
)
from hermes_trader.agents.config_store import (
    CANONICAL_DEFAULTS,
    cfg_get,
)


# ── sizing_v2_cap_pct canonical default mirrors the executor fallback ─

def test_batch2_sizing_v2_cap_pct_canonical_default_is_one():
    # executor.py falls back to 1.0 when the leaf is absent; the canonical
    # default must match so a key-absent config behaves identically.
    block = CANONICAL_DEFAULTS["atr_risk_sizing"]
    assert block["sizing_v2_cap_pct"] == 1.0
    assert cfg_get("atr_risk_sizing.sizing_v2_cap_pct", config={}) == 1.0


# ── canonical registration of the five shadow sub-blocks ─────────────

def test_batch2_runner_shadow_subblocks_registered():
    gate = CANONICAL_DEFAULTS["runner_entry_gate"]
    for name in ("breakout_score_floor", "early_breakout_shadow",
                 "per_coin_cooldown", "short_only_shadow"):
        assert name in gate, name
        # Default to observation only: a synthesised default block must
        # never arm an ENFORCE path for a key-absent config.
        assert gate[name]["shadow_mode"] is True, name


def test_batch2_stop_tuning_shadow_registered():
    tun = CANONICAL_DEFAULTS["dsl_exit"]["stop_tuning_shadow"]
    assert tun["shadow_mode"] is True
    assert set(tun) == {
        "shadow_mode", "candidate_max_loss_pct",
        "candidate_breakeven_trigger_pct",
    }


def test_batch2_production_values_survive_deep_merge():
    """The live file's explicit leaves override the new defaults."""
    from hermes_trader.agents.config_store import _deep_merge
    raw = {
        "runner_entry_gate": {
            "per_coin_cooldown": {"shadow_mode": False, "window_hours": 24},
            "breakout_score_floor": {"shadow_mode": True, "min_composite": 31.5},
        },
        "atr_risk_sizing": {"sizing_v2_cap_pct": 0.1},
        "dsl_exit": {"stop_tuning_shadow": {"shadow_mode": True,
                                            "candidate_max_loss_pct": 1.5}},
    }
    merged = _deep_merge(CANONICAL_DEFAULTS, raw)
    # Explicit production value wins (ENFORCE stays ENFORCE; cap stays 0.1).
    assert merged["runner_entry_gate"]["per_coin_cooldown"][
        "shadow_mode"] is False
    assert merged["atr_risk_sizing"]["sizing_v2_cap_pct"] == 0.1
    # Untouched default leaves are filled in.
    assert merged["runner_entry_gate"]["per_coin_cooldown"][
        "max_consecutive_losses"] == 2
    assert merged["dsl_exit"]["stop_tuning_shadow"][
        "candidate_max_loss_pct"] == 1.5
    assert merged["dsl_exit"]["stop_tuning_shadow"][
        "candidate_breakeven_trigger_pct"] == 0.0


# ── schema drift sentinel: patch factories track canonical sub-blocks ─

def test_batch2_patch_defaults_contain_shadow_subblocks():
    gate = _ConfigPatch.model_fields["runner_entry_gate"].default_factory()
    for name in ("breakout_score_floor", "early_breakout_shadow",
                 "per_coin_cooldown", "short_only_shadow"):
        assert name in gate, name
    dsl = _ConfigPatch.model_fields["dsl_exit"].default_factory()
    assert "stop_tuning_shadow" in dsl
    sizing = _ConfigPatch.model_fields["atr_risk_sizing"].default_factory()
    assert "sizing_v2_cap_pct" in sizing


# ── patch gate: production-shaped sub-blocks accepted ─────────────────

def test_batch2_patch_gate_accepts_production_subblocks():
    errors = validate_config_updates({
        "runner_entry_gate": {
            "breakout_score_floor": {"shadow_mode": True,
                                     "min_composite": 31.5},
            "early_breakout_shadow": {
                "shadow_mode": True, "shadow_log_path": "/data/x.jsonl",
                "max_extension_atr": 1.5, "early_stop_atr_mult": 1.2,
                "early_size_fraction": 0.5},
            "per_coin_cooldown": {
                "shadow_mode": False, "window_hours": 24,
                "repeat_min_composite": 45, "max_consecutive_losses": 2,
                "loss_cooldown_hours": 24},
            "short_only_shadow": {"shadow_mode": True,
                                  "shadow_log_path": "/data/y.jsonl"},
        },
        "dsl_exit": {"stop_tuning_shadow": {
            "shadow_mode": True, "candidate_max_loss_pct": 1.5,
            "candidate_breakeven_trigger_pct": 1.0}},
    }, strict_keys=True)
    assert errors == [], errors


# ── patch gate: unknown / mistyped / out-of-range leaves rejected ─────

def test_batch2_patch_gate_rejects_bad_subblock_leaves():
    cases = [
        ({"runner_entry_gate": {"breakout_score_floor": {"bogus": 1}}},
         "breakout_score_floor.bogus: unknown key"),
        ({"runner_entry_gate": {"early_breakout_shadow": {
            "early_size_fraction": 2.0}}},
         "early_breakout_shadow.early_size_fraction"),
        ({"runner_entry_gate": {"per_coin_cooldown": {
            "max_consecutive_losses": "two"}}},
         "per_coin_cooldown.max_consecutive_losses"),
        ({"runner_entry_gate": {"short_only_shadow": {
            "shadow_mode": "yes"}}},
         "short_only_shadow.shadow_mode"),
        ({"dsl_exit": {"stop_tuning_shadow": {
            "candidate_max_loss_pct": -1}}},
         "stop_tuning_shadow.candidate_max_loss_pct"),
        ({"dsl_exit": {"stop_tuning_shadow": {"bogus": 1}}},
         "stop_tuning_shadow.bogus: unknown key"),
    ]
    for patch, needle in cases:
        errors = validate_config_updates(patch, strict_keys=True)
        assert errors, patch
        assert any(needle in e for e in errors), (needle, errors)
