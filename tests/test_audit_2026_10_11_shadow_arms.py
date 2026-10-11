"""Audit 2026-10-11 shadow A/B arms.

Two record-only counter-factual arms, wired without changing any live path:

  * ``dsl_exit.stop_confirm_shadow`` — at a LIVE floor_breach exit, would a
    tighter confirmation (1 breach / 1.0s, index still required) have exited
    earlier, and by how much (dwell seconds + spot overshoot).
  * ``weight_shadow`` (perception) — would the canonical reweight
    (trendStrength 0.30 / uptrendMomentum 0.20 / momentumBurst 3bar-3%) flip
    the composite-gate decision vs the live pinned weights.

Plus canonical/schema registration. Best-effort, never raises on the hot path.
"""
import json
import time

import pytest

from hermes_trader.agents import dsl_exit, perception
from hermes_trader.agents.config_defaults import CANONICAL_DEFAULTS
from hermes_trader.agents.config_schema import validate_config_updates


# ── registration ─────────────────────────────────────────────────────────

def test_stop_confirm_shadow_registered_default_off():
    blk = CANONICAL_DEFAULTS["dsl_exit"]["stop_confirm_shadow"]
    assert blk["shadow_mode"] is False
    assert blk["candidate_consecutive_breaches"] == 1
    assert blk["candidate_breach_confirm_sec"] == 1.0


def test_weight_shadow_registered_default_off():
    blk = CANONICAL_DEFAULTS["weight_shadow"]
    assert blk["shadow_mode"] is False
    assert blk["candidate_momentum_lookback"] == 3
    assert blk["candidate_momentum_pct"] == 3.0
    cw = blk["candidate_weights"]
    assert cw["trendStrength"] == 0.30 and cw["uptrendMomentum"] == 0.20
    # candidate weights sum mirrors the live denominator family (~1.80)
    assert sum(cw.values()) == pytest.approx(1.80, abs=1e-9)


def test_schema_accepts_both_blocks():
    errs = validate_config_updates({
        "dsl_exit": {"stop_confirm_shadow": {
            "shadow_mode": True,
            "candidate_consecutive_breaches": 1,
            "candidate_breach_confirm_sec": 1.0}},
        "weight_shadow": {
            "shadow_mode": True,
            "candidate_momentum_lookback": 3,
            "candidate_momentum_pct": 3.0,
            "candidate_weights": {"trendStrength": 0.3, "uptrendMomentum": 0.2}},
    }, strict_keys=True)
    assert errs == [], errs


def test_schema_rejects_bad_stop_confirm_leaf():
    errs = validate_config_updates({"dsl_exit": {"stop_confirm_shadow": {
        "candidate_consecutive_breaches": 0}}}, strict_keys=True)
    assert any("candidate_consecutive_breaches" in e for e in errs)


def test_schema_rejects_non_numeric_weight():
    errs = validate_config_updates({"weight_shadow": {
        "candidate_weights": {"trendStrength": "x"}}}, strict_keys=True)
    assert any("trendStrength" in e for e in errs)


# ── stop-confirm recorder ─────────────────────────────────────────────────

def _tracker(monkeypatch, tmp_path):
    state = tmp_path / "dsl.json"
    monkeypatch.setattr(dsl_exit, "DSL_STATE_FILE", str(state))
    monkeypatch.setattr(dsl_exit, "DSL_STATE_LOCK_FILE", str(state) + ".lock")
    monkeypatch.setattr(dsl_exit, "_LAST_SAVE_TS", 0.0)
    monkeypatch.setattr(dsl_exit, "_SAVE_DIRTY", False)
    dsl_exit._active_positions.clear()
    dsl_exit._loaded_from_disk = False
    pol = dsl_exit.ExitPolicy()
    t = dsl_exit.register_position("ADA", "long", entry_px=100.0, policy=pol)
    return t, pol


def _enable_stop_arm(monkeypatch):
    monkeypatch.setattr(
        "hermes_trader.agents.config_store.read_agent_config",
        lambda: {"dsl_exit": {"stop_confirm_shadow": {
            "shadow_mode": True,
            "candidate_consecutive_breaches": 1,
            "candidate_breach_confirm_sec": 1.0}}})


def test_stop_confirm_records_earlier_exit(monkeypatch, tmp_path):
    f = tmp_path / "sc.jsonl"
    monkeypatch.setenv("HERMES_STOP_CONFIRM_SHADOW_FILE", str(f))
    _enable_stop_arm(monkeypatch)
    t, pol = _tracker(monkeypatch, tmp_path)
    # candidate would have fired 200s ago at 98.0; live exits now at 97.0
    now = time.monotonic()
    monkeypatch.setattr(time, "monotonic", lambda: now)
    t._first_breach_ts = now - 205.0      # live first breach
    t._sc_first_ts = now - 200.0          # candidate gate satisfied 200s ago
    t._sc_first_px = 98.0
    dsl_exit._record_stop_confirm_shadow(
        t, pol, live_reason="2x consec held 205.0s",
        floor=98.5, exit_px=97.0, unrealized_pct=-3.0)
    rec = json.loads(f.read_text().strip())
    assert rec["rule"] == "stop_confirm" and rec["would"] == "earlier_exit"
    d = rec["detail"]
    assert d["candidate_consecutive_breaches"] == 1
    assert d["candidate_breach_confirm_sec"] == 1.0
    assert d["overshoot_delta_spot_pct"] < 0  # long: live exit lower than candidate
    # live excursion -3% vs candidate -2%
    assert d["live_exit_excursion_spot_pct"] == pytest.approx(-3.0, abs=1e-6)
    assert d["candidate_excursion_spot_pct"] == pytest.approx(-2.0, abs=1e-6)


def test_stop_confirm_skips_without_candidate_state(monkeypatch, tmp_path):
    f = tmp_path / "sc.jsonl"
    monkeypatch.setenv("HERMES_STOP_CONFIRM_SHADOW_FILE", str(f))
    _enable_stop_arm(monkeypatch)
    t, pol = _tracker(monkeypatch, tmp_path)
    now = time.monotonic()
    t._first_breach_ts = now - 100.0
    # _sc_first_ts left None (e.g. restart mid-trade) -> no record
    dsl_exit._record_stop_confirm_shadow(
        t, pol, "x", floor=99.0, exit_px=98.0, unrealized_pct=-2.0)
    assert not f.exists()


def test_stop_confirm_disabled_writes_nothing(monkeypatch, tmp_path):
    f = tmp_path / "sc.jsonl"
    monkeypatch.setenv("HERMES_STOP_CONFIRM_SHADOW_FILE", str(f))
    monkeypatch.setattr(
        "hermes_trader.agents.config_store.read_agent_config",
        lambda: {"dsl_exit": {"stop_confirm_shadow": {"shadow_mode": False}}})
    t, pol = _tracker(monkeypatch, tmp_path)
    t._sc_first_ts = time.monotonic()
    t._sc_first_px = 98.0
    t._first_breach_ts = time.monotonic()
    dsl_exit._record_stop_confirm_shadow(
        t, pol, "x", floor=99.0, exit_px=98.0, unrealized_pct=-2.0)
    assert not f.exists()


# ── weight composite recorder ─────────────────────────────────────────────

def _hit(name, score, fired):
    return {"name": name, "score": score, "reason": "t", "fired": fired}


def _enable_weight_arm(monkeypatch):
    monkeypatch.setattr(
        "hermes_trader.agents.config_store.read_agent_config",
        lambda: {"weight_shadow": {
            "shadow_mode": True,
            "candidate_momentum_lookback": 3,
            "candidate_momentum_pct": 3.0,
            "candidate_weights": CANONICAL_DEFAULTS["weight_shadow"][
                "candidate_weights"]}})


def test_weight_shadow_emits_on_decision_flip(monkeypatch, tmp_path):
    f = tmp_path / "w.jsonl"
    monkeypatch.setenv("HERMES_WEIGHT_SHADOW_FILE", str(f))
    _enable_weight_arm(monkeypatch)
    # Force the candidate momentumBurst to fire strongly (alt score clears gate).
    def fake_burst(candles, lb, pct):
        return _hit("momentumBurst", 100 if (lb, pct) == (3, 3.0) else 0, True)
    monkeypatch.setattr(perception.trigger_mod, "momentum_burst", fake_burst)
    # candidate composite with a 0.20-weight burst at score100 clears 54.
    live_hits = [_hit("trendStrength", 40, True)]  # live weighted below gate
    live_weights = {"trendStrength": 0.55}
    perception._record_weight_shadow(
        coin="XYZ", hits=live_hits, candles=[{}, {}, {}],
        live_weights=live_weights, live_score=20.0, gate=54.0,
        fired_count=1, now_ms=int(time.time() * 1000))
    rec = json.loads(f.read_text().strip())
    assert rec["rule"] == "weight_shadow" and rec["would"] == "would_surface"
    assert rec["detail"]["candidate_passes_gate"] is True
    assert rec["detail"]["live_passes_gate"] is False


def test_weight_shadow_silent_without_flip(monkeypatch, tmp_path):
    f = tmp_path / "w.jsonl"
    monkeypatch.setenv("HERMES_WEIGHT_SHADOW_FILE", str(f))
    _enable_weight_arm(monkeypatch)
    monkeypatch.setattr(
        perception.trigger_mod, "momentum_burst",
        lambda c, lb, pct: _hit("momentumBurst", 0, False))
    # low-score single hit: candidate-normalized composite (score30 * 0.30 /
    # 1.80 * 10 = 50) stays below the 54 gate under both schemes -> no record.
    hits = [_hit("trendStrength", 30, True)]
    perception._record_weight_shadow(
        coin="XYZ", hits=hits, candles=[{}, {}, {}],
        live_weights={"trendStrength": 0.55}, live_score=20.0, gate=54.0,
        fired_count=1, now_ms=int(time.time() * 1000))
    assert not f.exists()


def test_weight_shadow_disabled_writes_nothing(monkeypatch, tmp_path):
    f = tmp_path / "w.jsonl"
    monkeypatch.setenv("HERMES_WEIGHT_SHADOW_FILE", str(f))
    monkeypatch.setattr(
        "hermes_trader.agents.config_store.read_agent_config",
        lambda: {"weight_shadow": {"shadow_mode": False,
                                   "candidate_weights": {"x": 1.0}}})
    perception._record_weight_shadow(
        coin="X", hits=[_hit("x", 10, True)], candles=[{}, {}, {}],
        live_weights={"x": 1.0}, live_score=10.0, gate=54.0, fired_count=1,
        now_ms=int(time.time() * 1000))
    assert not f.exists()
