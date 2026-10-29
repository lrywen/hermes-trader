"""M2 entry-selection construction fixes (2026-10-29): record-only shadow arms.

R1 mover_bypass_veto and R3 overextension_veto are SHADOW arms. They must be a
strict no-op when shadow_mode is absent/false; when enabled they only RECORD a
would-block via the risk_tuning_shadow feed while live admission is unchanged
(return ""). Live enforcement is a separate, explicitly-approved promotion.
"""
from __future__ import annotations

import pytest

from hermes_trader.agents import executor


def _gate(**kw):
    base = {
        "enabled": True,
        "min_confidence": 0.70,
        "min_composite": 30.0,
        "min_hip3_composite": 50.0,
        "rsi_overbought": 75.0,
        "rsi_oversold": 25.0,
        "max_extension_atr": 2.5,
        "allow_shorts": True,
        "mover_min_confidence": 0.72,
        "mover_min_composite": 30.0,
        "mover_bypass_veto": {"shadow_mode": False},
        "overextension_veto": {"shadow_mode": False, "max_extension_atr": 3.0},
    }
    base.update(kw)
    return {"runner_entry_gate": base}


def _mover_analysis(**kw):
    base = {"coin": "TST", "side": "long", "confidence": 0.80,
            "ai_confidence_raw": 0.80, "composite_score": 34.0,
            "daily_mover_fired": True, "slow_burn_count": 2,
            "momentum_burst_fired": False, "breakout_fired": False,
            "volume_spike_fired": False, "uptrend_momentum_fired": True,
            "whale_signal": False, "mid_price": 100.0}
    base.update(kw)
    return base


def _fresh_analysis(**kw):
    base = {"coin": "TST", "side": "long", "confidence": 0.80,
            "ai_confidence_raw": 0.80, "composite_score": 48.0,
            "daily_mover_fired": False, "slow_burn_count": 2,
            "momentum_burst_fired": False, "breakout_fired": True,
            "volume_spike_fired": False, "uptrend_momentum_fired": True,
            "whale_signal": False, "mid_price": 100.0,
            "rsi4h": 70.0, "adx4h": 40.0, "ema8_4h": 101.0,
            "ema21_4h": 100.0, "atr4h": 1.0, "close4h": 103.2}
    base.update(kw)
    return base


@pytest.fixture
def isolated(monkeypatch):
    monkeypatch.setattr(executor, "evaluate_risk_overlay",
                        lambda cfg: {"posture": "neutral"})
    monkeypatch.setattr(executor, "resolve_applied_knobs",
                        lambda base, cfg, snap: dict(base))
    import hermes_trader.agents.market_regime as mr
    monkeypatch.setattr(mr, "detect_regime_with_score",
                        lambda coin: ("up", 1.0))
    return monkeypatch


def _spy(monkeypatch):
    calls = []
    monkeypatch.setattr(executor, "_record_risk_tuning_shadow",
                        lambda *a, **k: calls.append(k))
    return calls


# ── R1 daily-mover bypass veto ──────────────────────────────────────────────

def test_r1_disabled_is_strict_noop(isolated, monkeypatch):
    calls = _spy(monkeypatch)
    assert executor._runner_entry_block_reason_impl(
        _mover_analysis(), _gate()) == ""
    assert calls == []


def test_r1_shadow_records_but_still_admits(isolated, monkeypatch):
    calls = _spy(monkeypatch)
    cfg = _gate(mover_bypass_veto={"shadow_mode": True})
    assert executor._runner_entry_block_reason_impl(
        _mover_analysis(), cfg) == ""
    assert len(calls) == 1
    assert calls[0]["rule"] == "mover_bypass_veto"
    assert calls[0]["would"] == "block"
    assert calls[0]["detail"]["daily_mover"] is True
    assert calls[0]["detail"]["fresh_impulse"] is False


def test_r1_skips_fresh_impulse_admit(isolated, monkeypatch):
    calls = _spy(monkeypatch)
    cfg = _gate(mover_bypass_veto={"shadow_mode": True})
    assert executor._runner_entry_block_reason_impl(
        _fresh_analysis(), cfg) == ""
    assert [c for c in calls if c.get("rule") == "mover_bypass_veto"] == []


# ── R3 over-extension chase veto ────────────────────────────────────────────

def test_r3_disabled_is_noop(isolated, monkeypatch):
    calls = _spy(monkeypatch)
    cfg = _gate(overextension_veto={"shadow_mode": False,
                                    "max_extension_atr": 3.0})
    assert executor._runner_entry_block_reason_impl(
        _fresh_analysis(), cfg) == ""
    assert calls == []


def test_r3_shadow_flags_extended_but_still_admits(isolated, monkeypatch):
    calls = _spy(monkeypatch)
    cfg = _gate(overextension_veto={"shadow_mode": True,
                                    "max_extension_atr": 3.0})
    assert executor._runner_entry_block_reason_impl(
        _fresh_analysis(), cfg) == ""
    flagged = [c for c in calls if c.get("rule") == "overextension_veto"]
    assert len(flagged) == 1
    assert flagged[0]["would"] == "block"
    assert flagged[0]["detail"]["extension_4h_atr"] == 3.2


def test_r3_skips_low_extension(isolated, monkeypatch):
    calls = _spy(monkeypatch)
    cfg = _gate(overextension_veto={"shadow_mode": True,
                                    "max_extension_atr": 3.0})
    assert executor._runner_entry_block_reason_impl(
        _fresh_analysis(close4h=101.0), cfg) == ""
    assert [c for c in calls if c.get("rule") == "overextension_veto"] == []
