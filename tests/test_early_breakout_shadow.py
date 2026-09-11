"""Offline tests for the early-breakout-entry shadow (Audit 2026-09-11).

The NEAR tape: a 5.6x-volume first-leg breakout at 12:30 was rejected on
confidence 0.60/score 12.6; by structure-confirmation at 14:10 the entry was
7.4% higher. The early-breakout shadow records those fresh first-leg impulses
the strict gate rejects, so an early half-size + tight-ATR-stop lane's EV can
be measured. It must NEVER change the live block.

Pins the pure predicate (fresh impulse + not extended/overbought + low score)
and the guarantee that the runner block is identical with the probe on/off.
"""
from __future__ import annotations

import json

from hermes_trader.agents import executor, market_regime


def _gate(path, *, enabled=True):
    return {
        "enabled": enabled,
        "min_confidence": 0.62,
        "min_composite": 45,
        "min_short_confidence": 0.68,
        "min_short_composite": 40,
        "rsi_overbought": 75.0,
        "allow_shorts": False,
        "early_breakout_shadow": {
            "shadow_mode": True, "shadow_log_path": str(path),
            "max_extension_atr": 1.5, "early_stop_atr_mult": 1.2,
            "early_size_fraction": 0.5,
        },
    }


def _analysis(**over):
    a = {
        "coin": "NEAR", "side": "long", "confidence": 0.60,
        "ai_confidence_raw": 0.60, "composite_score": 12.6,
        "mid": 2.45, "rsi4h": 55.0, "adx4h": 30.0, "atr4h": 0.05,
        "volume_spike_fired": True, "breakout_fired": False,
        "momentum_burst_fired": True, "uptrend_momentum_fired": False,
        "downtrend_momentum_fired": False, "slow_burn_count": 0,
        "whale_signal": False, "extension_atr": 0.4, "id": "t1",
    }
    a.update(over)
    return a


def _stub(monkeypatch):
    monkeypatch.setattr(market_regime, "detect_regime_with_score",
                        lambda coin, **k: ("neutral", 0.5))


# ── pure predicate ─────────────────────────────────────────────────────────

def test_candidate_fresh_volume_burst_low_score(tmp_path):
    gate = _gate(tmp_path / "x.jsonl")
    # breakout OR (volume and burst) -> fresh
    ok, why = executor._early_breakout_candidate(
        _analysis(), gate, fresh_impulse=True, score=12.6, rsi4h=55.0)
    assert ok and why == "fresh_first_leg_low_score"


def test_not_candidate_when_shadow_off(tmp_path):
    gate = _gate(tmp_path / "x.jsonl")
    gate["early_breakout_shadow"]["shadow_mode"] = False
    ok, why = executor._early_breakout_candidate(
        _analysis(), gate, fresh_impulse=True, score=12.6, rsi4h=55.0)
    assert not ok and why == "shadow_off"


def test_not_candidate_no_fresh_impulse(tmp_path):
    gate = _gate(tmp_path / "x.jsonl")
    ok, why = executor._early_breakout_candidate(
        _analysis(volume_spike_fired=False, momentum_burst_fired=False),
        gate, fresh_impulse=False, score=12.6, rsi4h=55.0)
    assert not ok and why == "no_fresh_impulse"


def test_not_candidate_when_extended(tmp_path):
    gate = _gate(tmp_path / "x.jsonl")
    ok, why = executor._early_breakout_candidate(
        _analysis(extension_atr=2.2), gate, fresh_impulse=True,
        score=12.6, rsi4h=55.0)
    assert not ok and why.startswith("extended")


def test_not_candidate_when_overbought(tmp_path):
    gate = _gate(tmp_path / "x.jsonl")
    ok, why = executor._early_breakout_candidate(
        _analysis(rsi4h=80.0), gate, fresh_impulse=True,
        score=12.6, rsi4h=80.0)
    assert not ok and why.startswith("rsi")


def test_not_candidate_when_score_already_admittable(tmp_path):
    gate = _gate(tmp_path / "x.jsonl")
    ok, why = executor._early_breakout_candidate(
        _analysis(), gate, fresh_impulse=True, score=50.0, rsi4h=55.0)
    assert not ok and why == "score_already_admittable"


# ── helper writes ──────────────────────────────────────────────────────────

def test_helper_writes_record(tmp_path, monkeypatch):
    _stub(monkeypatch)
    gate = _gate(tmp_path / "x.jsonl")
    executor._record_early_breakout_shadow(
        _analysis(), gate, block="confidence 0.60 < 0.62",
        fresh_impulse=True, score=12.6, gate_conf=0.60)
    rows = [json.loads(l) for l in (tmp_path / "x.jsonl").read_text().splitlines()]
    assert len(rows) == 1
    d = rows[0]["detail"]
    assert rows[0]["rule"] == "early_breakout_entry"
    assert d["early_size_fraction"] == 0.5 and d["early_stop_atr_mult"] == 1.2
    assert d["fresh_impulse"] is True and d["entry_px"] == 2.45


# ── live block identical with the probe on/off ─────────────────────────────

def test_gate_block_identical_shadow_on_off(tmp_path, monkeypatch):
    _stub(monkeypatch)
    on = {"runner_entry_gate": _gate(tmp_path / "on.jsonl"),
          "regime_risk_overlay": {"enabled": False, "shadow_mode": False}}
    off = {"runner_entry_gate": _gate(tmp_path / "off.jsonl"),
           "regime_risk_overlay": {"enabled": False, "shadow_mode": False}}
    off["runner_entry_gate"]["early_breakout_shadow"]["shadow_mode"] = False
    r_on = executor._runner_entry_block_reason(_analysis(), on)
    r_off = executor._runner_entry_block_reason(_analysis(), off)
    assert r_on == r_off == "runner_gate_blocked (confidence 0.60 < 0.62)"
    assert (tmp_path / "on.jsonl").exists()
    assert not (tmp_path / "off.jsonl").exists()
