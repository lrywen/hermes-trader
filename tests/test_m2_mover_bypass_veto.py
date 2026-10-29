"""M2 / R1 (2026-10-29): daily-mover bypass veto is a record-only shadow arm.

88-day Binance + real-verdict reconciliation proved the daily-mover-only
bypass (structured_daily_mover WITHOUT a fresh impulse) loses gross: it buys a
+1.71% pre-entry run-up and mean-reverts. The arm must:

  * be a strict no-op (never fire) when shadow_mode is absent/false;
  * when shadow_mode=true, RECORD a would-block (rule=mover_bypass_veto) but
    still ADMIT (return "") — live admission never changes;
  * never fire for a fresh-impulse admit (structured_runner carrier).
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
    }
    base.update(kw)
    return {"runner_entry_gate": base}


def _mover_analysis(**kw):
    # daily_mover long with slow-burn backing, NO fresh impulse -> the C path.
    base = {"coin": "TST", "side": "long", "confidence": 0.80,
            "ai_confidence_raw": 0.80, "composite_score": 34.0,
            "daily_mover_fired": True, "slow_burn_count": 2,
            "momentum_burst_fired": False, "breakout_fired": False,
            "volume_spike_fired": False, "uptrend_momentum_fired": True,
            "whale_signal": False, "mid_price": 100.0}
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


def _mover_admits(analysis, config):
    """A C-path daily-mover long clears every other gate (confidence passes,
    mover thresholds pass, no overbought snapshot) and reaches final admit."""
    reason = executor._runner_entry_block_reason_impl(analysis, config)
    return reason


def test_arm_disabled_is_strict_noop(isolated, monkeypatch):
    calls = []
    monkeypatch.setattr(executor, "_record_risk_tuning_shadow",
                        lambda *a, **k: calls.append(k))
    cfg = _gate()  # shadow_mode False
    assert _mover_admits(_mover_analysis(), cfg) == ""   # live admit unchanged
    assert calls == []                                   # arm never recorded


def test_arm_shadow_records_but_still_admits(isolated, monkeypatch):
    calls = []
    monkeypatch.setattr(executor, "_record_risk_tuning_shadow",
                        lambda *a, **k: calls.append(k))
    cfg = _gate(mover_bypass_veto={"shadow_mode": True})
    assert _mover_admits(_mover_analysis(), cfg) == ""   # STILL admitted
    assert len(calls) == 1
    assert calls[0]["rule"] == "mover_bypass_veto"
    assert calls[0]["would"] == "block"
    assert calls[0]["detail"]["daily_mover"] is True
    assert calls[0]["detail"]["fresh_impulse"] is False


def test_arm_skips_fresh_impulse_admit(isolated, monkeypatch):
    calls = []
    monkeypatch.setattr(executor, "_record_risk_tuning_shadow",
                        lambda *a, **k: calls.append(k))
    cfg = _gate(mover_bypass_veto={"shadow_mode": True})
    # fresh impulse (breakout) + slow -> structured_runner carrier, not C
    a = _mover_analysis(daily_mover_fired=False, breakout_fired=True,
                        slow_burn_count=2, composite_score=46.0)
    assert _mover_admits(a, cfg) == ""
    assert calls == []
