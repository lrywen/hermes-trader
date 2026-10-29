"""M1 (plan G1): decision-shadow recorder enrichment.

Hermetic unit tests for the additive observational fields appended to
runner_gate_shadow rows: fired_names mapping, volume tiering, safe float.
These fields are never read by the gate, so the tests assert pure transforms.
"""
from __future__ import annotations

from hermes_trader.agents import executor as e


def test_fired_names_maps_booleans_and_slowburn():
    feats = {"vol": True, "brk": False, "burst": True, "dMover": False,
             "up": True, "down": False, "slow_count": 2}
    assert e._runner_gate_fired_names(feats) == [
        "volumeSpike", "momentumBurst", "uptrendMomentum", "slowBurn"]


def test_fired_names_empty_and_no_slow_when_zero():
    assert e._runner_gate_fired_names({}) == []
    assert e._runner_gate_fired_names({"slow_count": 0}) == []
    assert e._runner_gate_fired_names({"brk": True}) == ["breakout"]
    assert e._runner_gate_fired_names({"down": True, "slow_count": 1}) == [
        "downtrendMomentum", "slowBurn"]
    # defensive: non-dict / garbage must not raise
    assert e._runner_gate_fired_names(None) == []  # type: ignore[arg-type]


def test_volume_tier_boundaries():
    assert e._runner_gate_volume_tier(None) is None
    assert e._runner_gate_volume_tier(0) == "micro"
    assert e._runner_gate_volume_tier(9_999_999) == "micro"
    assert e._runner_gate_volume_tier(10_000_000) == "mid"
    assert e._runner_gate_volume_tier(99_999_999) == "mid"
    assert e._runner_gate_volume_tier(100_000_000) == "mega"


def test_safe_float():
    assert e._safe_float("3.5") == 3.5
    assert e._safe_float(2) == 2.0
    assert e._safe_float(None) is None
    assert e._safe_float("x") is None
    assert e._safe_float(float("nan")) is None
    assert e._safe_float(float("inf")) is None


def test_legacy_fired_names_reconstructs_old_schema():
    import importlib.util, os
    spec = importlib.util.spec_from_file_location(
        "rds", os.path.join(os.path.dirname(__file__), "..",
                            "scripts", "reconcile_decision_shadow.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    old = {"vol": True, "brk": False, "burst": True, "dMover": False,
           "up": False, "down": True, "slow_burn_count": 1}
    assert mod.legacy_fired_names(old) == [
        "volumeSpike", "momentumBurst", "downtrendMomentum", "slowBurn"]
    assert mod.legacy_fired_names({"fired_names": ["breakout"]}) == ["breakout"]
    assert mod.legacy_fired_names({}) == []


def test_classify_path_abc():
    import importlib.util, os
    spec = importlib.util.spec_from_file_location(
        "rds2", os.path.join(os.path.dirname(__file__), "..",
                             "scripts", "reconcile_decision_shadow.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.classify_path({"fresh_impulse": True, "slow_burn_count": 1}) == "A_fresh_slow"
    assert mod.classify_path({"fresh_impulse": True, "slow_burn_count": 0,
                              "composite_score": 50}) == "B_fresh_score"
    assert mod.classify_path({"fresh_impulse": False, "dMover": True,
                              "admitted": True}) == "C_daily_mover"
    assert mod.classify_path({"fresh_impulse": False, "dMover": False,
                              "admitted": False}) == "Z_blocked"
