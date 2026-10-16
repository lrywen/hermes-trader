"""P3 (audit 2026-10-15) threshold-fallback alignment.

The merged live config always deep-merges CANONICAL_DEFAULTS, so the
``.get(key, <literal>)`` fallbacks in the runner / risk gates are normally
unreachable. They nonetheless used to carry literals that had DRIFTED from the
canonical values (e.g. min_ai_confidence fallback 0.70 vs canonical 0.60,
runner min_composite fallback 30 vs canonical 45, 1h-RSI blowoff 85/15 vs
canonical 88/12). A missing-key event (hand-edited / partial config) would
then silently snap the bar to a different number.

These tests pin that every such fallback is sourced from CANONICAL_DEFAULTS
(or the shared constant), so the two can never diverge again without a test
failure. Behaviour under the merged config is unchanged.
"""
from __future__ import annotations

from pathlib import Path

from hermes_trader.agents import confidence_guard as cg
from hermes_trader.agents.config_defaults import CANONICAL_DEFAULTS

ROOT = Path(__file__).resolve().parent.parent
EXECUTOR_PY = ROOT / "hermes_trader" / "agents" / "executor.py"
RISK_GATES_PY = ROOT / "hermes_trader" / "agents" / "risk_gates.py"
PERCEPTION_PY = ROOT / "hermes_trader" / "agents" / "perception.py"
DECAY_PY = ROOT / "hermes_trader" / "agents" / "shadow" / "confidence_decay.py"
TRADING_LOOP_PY = ROOT / "scripts" / "trading_loop.py"


# ── A1/A2: generic confidence floor fallback ────────────────────────────────

def test_confidence_default_constant_matches_canonical():
    assert cg.DEFAULT_MIN_AI_CONFIDENCE == CANONICAL_DEFAULTS["min_ai_confidence"] == 0.60


def test_resolver_missing_keys_falls_back_to_canonical_not_070():
    # neither nested runner key nor canonical key present -> built-in default
    assert cg.resolve_runner_generic_confidence({}) == 0.60
    # canonical present wins
    assert cg.resolve_runner_generic_confidence({"min_ai_confidence": 0.65}) == 0.65
    # explicit nested emergency override still honoured
    cfg = {"min_ai_confidence": 0.60,
           "runner_entry_gate": {"min_confidence": 0.7}}
    assert cg.resolve_runner_generic_confidence(cfg) == 0.7


def test_drifted_070_confidence_literals_removed_from_call_sites():
    # The override floor and the shadow counterfactual must resolve through the
    # SSOT, not a hardcoded 0.70.
    executor_src = EXECUTOR_PY.read_text(encoding="utf-8")
    decay_src = DECAY_PY.read_text(encoding="utf-8")
    assert 'config.get("min_ai_confidence", 0.70)' not in executor_src
    assert 'config.get("min_ai_confidence", 0.70)' not in decay_src
    assert "canonical_generic_confidence" in executor_src
    assert "canonical_generic_confidence" in decay_src


# ── A3-A8: runner / mover / pullback / rescore composite fallbacks ───────────

def test_runner_gate_fallbacks_reference_canonical():
    src = EXECUTOR_PY.read_text(encoding="utf-8")
    # generic min_composite, mover pair, pullback block all read canonical
    assert '_rg_defaults = CANONICAL_DEFAULTS["runner_entry_gate"]' in src
    assert '_rg_defaults["min_composite"]' in src
    assert 'CANONICAL_DEFAULTS["runner_entry_gate"]["mover_min_confidence"]' in src
    assert 'CANONICAL_DEFAULTS["runner_entry_gate"]["mover_min_composite"]' in src
    assert '_pb_defaults = CANONICAL_DEFAULTS["runner_entry_gate"]["pullback_long"]' in src
    # the drifted literals must be gone from those get() fallbacks
    assert 'gate.get("mover_min_confidence", 0.80)' not in src
    assert 'gate.get("mover_min_composite", 45.0)' not in src
    assert 'gate.get("min_composite", 30.0)' not in src
    assert 'pb_cfg.get("min_composite", 20.0)' not in src
    assert 'pb_cfg.get("max_rsi", 70.0)' not in src


def test_score_invariant_uses_same_canonical_as_runner_gate():
    src = RISK_GATES_PY.read_text(encoding="utf-8")
    assert 'cfg_get("runner_entry_gate.min_composite"' in src
    assert '_runner_gate_cfg.get("min_composite", 30.0)' not in src


def test_perception_rescore_fallback_is_canonical_54():
    src = PERCEPTION_PY.read_text(encoding="utf-8")
    assert 'CANONICAL_DEFAULTS["scan"]["minCompositeScore"]' in src
    assert '.get("minCompositeScore", 30)' not in src
    assert CANONICAL_DEFAULTS["scan"]["minCompositeScore"] == 54


# ── A9/A10: late_chase RSI / move-state band read+write alignment ────────────

def test_late_chase_gate_fallbacks_reference_canonical():
    src = RISK_GATES_PY.read_text(encoding="utf-8")
    assert '_lc_defaults = CANONICAL_DEFAULTS["late_chase"]' in src
    assert '_lc_defaults["fresh_move_band_pct"]' in src
    assert '_lc_defaults["rsi1h_overbought"]' in src
    assert '_lc_defaults["rsi1h_oversold"]' in src
    # drifted legacy literals removed
    assert 'cfg.get("fresh_move_band_pct", 8.0)' not in src
    assert 'cfg.get("rsi1h_overbought", 85.0)' not in src
    assert 'cfg.get("rsi1h_oversold", 15.0)' not in src


def test_move_state_anchor_write_band_matches_gate_read_band():
    """The anchor WRITE side (trading_loop.observe) and the gate READ side
    (risk_gates.late_chase_gate) must resolve the same canonical band so the
    state machine can't anchor at 8% while gating at 12% under a missing key."""
    loop_src = TRADING_LOOP_PY.read_text(encoding="utf-8")
    assert 'CANONICAL_DEFAULTS' in loop_src
    assert '.get("fresh_move_band_pct", 8.0)' not in loop_src
    # both sides ultimately anchor to the same canonical leaf
    canonical_band = CANONICAL_DEFAULTS["late_chase"]["fresh_move_band_pct"]
    assert canonical_band == 12.0
    assert RISK_GATES_PY.read_text(encoding="utf-8").count(
        "fresh_move_band_pct") >= 2
