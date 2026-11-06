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


def test_daily_mover_admission_writes_one_bypass_record(isolated, monkeypatch):
    _spy(monkeypatch)
    calls = []
    monkeypatch.setattr(
        executor, "_record_regime_bypass",
        lambda analysis, override, **details: calls.append(
            (analysis["id"], override, details)))
    analysis = _mover_analysis(id="a-daily")
    assert executor._runner_entry_block_reason_impl(analysis, _gate()) == ""
    assert len(calls) == 1
    assert calls[0][0:2] == ("a-daily", "daily-mover")
    assert calls[0][2]["daily_mover_fired"] is True


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
    # 2026-11-06: the arm now records at TWO observation points — upstream of
    # the hard late-entry veto (stage=pre_late_entry) and at the end of the
    # gate chain (stage=post_gate_chain). A candidate that survives every gate
    # lands in both; the stage tag is what keeps offline analysis from
    # double-counting. Admission is still unchanged (return "").
    assert len(flagged) == 2
    assert {c["detail"]["stage"] for c in flagged} == {
        "pre_late_entry", "post_gate_chain"}
    assert all(c["would"] == "block" for c in flagged)
    assert all(c["detail"]["extension_4h_atr"] == 3.2 for c in flagged)


def test_r3_pre_late_entry_captures_hard_gate_rejected(isolated, monkeypatch):
    """The whole point of moving the recorder upstream: a long so stretched
    that the HARD late-entry veto rejects it must still be observed.

    Before 2026-11-06 this population was invisible — the end-of-chain
    recorder never ran because the hard veto returned first, which is exactly
    why the arm had logged zero rows since 2026-10-29.
    """
    calls = _spy(monkeypatch)
    cfg = _gate(overextension_veto={"shadow_mode": True,
                                    "max_extension_atr": 2.0})
    # adx4h=20 (< adx_trend_threshold 35) keeps the hard gate on the STRICT
    # limit ext_ob=2.5 rather than the relaxed 3.5, so extension 3.2 is vetoed.
    verdict = executor._runner_entry_block_reason_impl(
        _fresh_analysis(adx4h=20.0), cfg)
    assert "extension" in verdict
    assert "over-extended long" in verdict
    flagged = [c for c in calls if c.get("rule") == "overextension_veto"]
    # Only the upstream recorder ran; the chain returned before the end one.
    assert len(flagged) == 1
    assert flagged[0]["detail"]["stage"] == "pre_late_entry"
    assert flagged[0]["detail"]["extension_4h_atr"] == 3.2
    assert flagged[0]["detail"]["max_extension_atr"] == 2.0
    assert flagged[0]["would"] == "block"


def test_r3_pre_late_entry_band_now_observable(isolated, monkeypatch):
    """The (2.0, 2.5] band: below the hard veto, above the arm threshold.

    This is the band the 2026-11-06 threshold change (3.0 -> 2.0) was meant to
    open up. It must record at both points and still be admitted.
    """
    calls = _spy(monkeypatch)
    cfg = _gate(overextension_veto={"shadow_mode": True,
                                    "max_extension_atr": 2.0})
    # extension = (102.2 - 100) / 1.0 = 2.2 -> in (2.0, 2.5].
    assert executor._runner_entry_block_reason_impl(
        _fresh_analysis(close4h=102.2), cfg) == ""
    flagged = [c for c in calls if c.get("rule") == "overextension_veto"]
    assert len(flagged) == 2
    assert all(c["detail"]["extension_4h_atr"] == 2.2 for c in flagged)


def test_r3_skips_low_extension(isolated, monkeypatch):
    calls = _spy(monkeypatch)
    cfg = _gate(overextension_veto={"shadow_mode": True,
                                    "max_extension_atr": 3.0})
    assert executor._runner_entry_block_reason_impl(
        _fresh_analysis(close4h=101.0), cfg) == ""
    assert [c for c in calls if c.get("rule") == "overextension_veto"] == []


# ── Stage-4 deep slow-burn veto (shipped shadow-only) ──────────────────────

def test_ds_disabled_is_strict_noop(isolated, monkeypatch):
    calls = _spy(monkeypatch)
    assert executor._runner_entry_block_reason_impl(
        _fresh_analysis(), _gate()) == ""
    assert [c for c in calls if c.get("rule") == "deep_slow_burn_veto"] == []


def test_ds_shadow_records_but_still_admits(isolated, monkeypatch):
    calls = _spy(monkeypatch)
    cfg = _gate(deep_slow_burn_veto={"shadow_mode": True,
                                     "enforce": False, "min_slow_burn": 2})
    # slow_burn_count=2, fresh=True -> would-block recorded, still admitted
    assert executor._runner_entry_block_reason_impl(
        _fresh_analysis(), cfg) == ""
    flagged = [c for c in calls if c.get("rule") == "deep_slow_burn_veto"]
    assert len(flagged) == 1
    assert flagged[0]["would"] == "block"
    assert flagged[0]["detail"]["slow_burn_count"] == 2
    assert flagged[0]["detail"]["fresh_impulse"] is True


def test_ds_skips_shallow_slow_burn(isolated, monkeypatch):
    calls = _spy(monkeypatch)
    cfg = _gate(deep_slow_burn_veto={"shadow_mode": True, "min_slow_burn": 2})
    assert executor._runner_entry_block_reason_impl(
        _fresh_analysis(slow_burn_count=1), cfg) == ""
    assert [c for c in calls if c.get("rule") == "deep_slow_burn_veto"] == []


def test_ds_enforce_blocks(isolated, monkeypatch):
    calls = _spy(monkeypatch)
    cfg = _gate(deep_slow_burn_veto={"shadow_mode": False,
                                     "enforce": True, "min_slow_burn": 2})
    reason = executor._runner_entry_block_reason_impl(
        _fresh_analysis(), cfg)
    assert "deep_slow_burn_veto ENFORCE" in reason
    flagged = [c for c in calls if c.get("rule") == "deep_slow_burn_veto"]
    assert len(flagged) == 1
    assert flagged[0]["would"] == "block_enforced"
