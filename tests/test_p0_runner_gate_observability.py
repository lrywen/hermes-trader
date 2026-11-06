"""P0 observability (2026-10-21 redundancy plan): runner-gate structured JSONL.

Locks that the gate still returns EXACTLY the same decision while now emitting
one machine-readable verdict per evaluation, including the fresh_impulse
provenance (fresh_src). No trading behaviour change.
"""
from __future__ import annotations

import json

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
        "mover_min_confidence": 0.80,
        "mover_min_composite": 45.0,
    }
    base.update(kw)
    return {"runner_entry_gate": base}


def _analysis(side="long", **kw):
    base = {"coin": "TST", "side": side, "confidence": 0.80,
            "composite_score": 50.0}
    base.update(kw)
    return base


def _isolate(monkeypatch):
    monkeypatch.setattr(executor, "evaluate_risk_overlay",
                        lambda cfg: {"posture": "neutral"})
    monkeypatch.setattr(executor, "resolve_applied_knobs",
                        lambda base, cfg, snap: dict(base))
    for name in ("_record_risk_tuning_shadow", "_record_pullback_shadow"):
        monkeypatch.setattr(executor, name, lambda *a, **k: None)
    import hermes_trader.agents.market_regime as mr
    monkeypatch.setattr(mr, "detect_regime_with_score",
                        lambda coin: ("up", 1.0))


def test_fresh_src_precedence_matches_or():
    f = executor._runner_gate_fresh_src
    # breakout beats everything
    assert f(breakout=True, recent_breakout=True, volume=True,
             burst_for_side=True, score=99, min_score=30) == "brk"
    # recent breakout next
    assert f(breakout=False, recent_breakout=True, volume=True,
             burst_for_side=True, score=99, min_score=30) == "recent_brk"
    # volume+burst before score+burst
    assert f(breakout=False, recent_breakout=False, volume=True,
             burst_for_side=True, score=10, min_score=30) == "vol_burst"
    # strong-score burst
    assert f(breakout=False, recent_breakout=False, volume=False,
             burst_for_side=True, score=40, min_score=30) == "burst_score"
    # none
    assert f(breakout=False, recent_breakout=False, volume=True,
             burst_for_side=False, score=99, min_score=30) == ""


def test_structured_verdict_emitted_admit(monkeypatch, tmp_path):
    _isolate(monkeypatch)
    out = tmp_path / "rg.jsonl"
    monkeypatch.setattr(executor, "_RUNNER_GATE_SHADOW_FILE", str(out))
    # breakout alone is self-confirming fresh impulse + score>=min -> admit.
    a = _analysis(breakout_fired=True, volumeSpike=True,
                  composite_score=50.0, confidence=0.9)
    decision = executor._runner_entry_block_reason(a, _gate())
    assert decision == ""  # behaviour unchanged
    lines = out.read_text().strip().splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["coin"] == "TST"
    assert rec["side"] == "long"
    assert rec["admitted"] is True
    assert rec["rule"] == ""
    assert rec["fresh_impulse"] is True
    assert rec["fresh_src"] == "brk"
    assert rec["composite_score"] == 50.0
    assert rec["min_composite"] == 30.0
    assert rec["is_hip3"] is False


def test_structured_verdict_emitted_block_with_rule(monkeypatch, tmp_path):
    _isolate(monkeypatch)
    out = tmp_path / "rg2.jsonl"
    monkeypatch.setattr(executor, "_RUNNER_GATE_SHADOW_FILE", str(out))
    # low confidence, no relax -> blocked at confidence bar.
    a = _analysis(composite_score=20.0, confidence=0.20)
    decision = executor._runner_entry_block_reason(a, _gate())
    assert "runner_gate_blocked" in decision
    rec = json.loads(out.read_text().strip().splitlines()[0])
    assert rec["admitted"] is False
    assert "confidence" in rec["rule"]
    assert rec["fresh_impulse"] is False
    assert rec["fresh_src"] == ""


def test_hip3_flag(monkeypatch, tmp_path):
    _isolate(monkeypatch)
    out = tmp_path / "rg3.jsonl"
    monkeypatch.setattr(executor, "_RUNNER_GATE_SHADOW_FILE", str(out))
    a = _analysis(coin="xyz:TST", composite_score=20.0, confidence=0.2)
    executor._runner_entry_block_reason(a, _gate())
    rec = json.loads(out.read_text().strip().splitlines()[0])
    assert rec["is_hip3"] is True


def test_p1_ai_telemetry_passthrough(monkeypatch, tmp_path):
    # P1: research-exit AI telemetry must land on the runner-gate record.
    _isolate(monkeypatch)
    out = tmp_path / "rg_ai.jsonl"
    monkeypatch.setattr(executor, "_RUNNER_GATE_SHADOW_FILE", str(out))
    a = _analysis(
        breakout_fired=True, volumeSpike=True,
        composite_score=50.0, confidence=0.9,
        ai_decision_elapsed_ms=101234,
        ai_confidence_raw=0.87,
        ai_debate_calls=3,
        ai_debate_state="succeeded",
        ai_debate_fallback_reason="",
    )
    executor._runner_entry_block_reason(a, _gate())
    rec = json.loads(out.read_text().strip().splitlines()[0])
    assert rec["ai_decision_elapsed_ms"] == 101234
    assert rec["ai_confidence_raw"] == 0.87
    assert rec["ai_debate_calls"] == 3
    assert rec["ai_debate_state"] == "succeeded"
    assert rec["ai_debate_fallback_reason"] == ""


def test_p1_ai_telemetry_absent_defaults_safe(monkeypatch, tmp_path):
    # Older analysis objects without the new keys must not break the recorder;
    # fields default to None / "" rather than raising.
    _isolate(monkeypatch)
    out = tmp_path / "rg_ai_old.jsonl"
    monkeypatch.setattr(executor, "_RUNNER_GATE_SHADOW_FILE", str(out))
    a = _analysis(composite_score=20.0, confidence=0.2)
    executor._runner_entry_block_reason(a, _gate())
    rec = json.loads(out.read_text().strip().splitlines()[0])
    assert rec["ai_decision_elapsed_ms"] is None
    assert rec["ai_confidence_raw"] is None
    assert rec["ai_debate_calls"] is None
    assert rec["ai_debate_state"] == ""
    assert rec["ai_debate_fallback_reason"] == ""
    assert rec["rsi4h"] is None
    assert rec["adx4h"] is None
    assert rec["extension_4h"] is None
    assert rec["relaxed_by_trend"] is None


def test_c1_late_entry_snapshot_passthrough_and_decision_unchanged(
        monkeypatch, tmp_path):
    _isolate(monkeypatch)
    out = tmp_path / "rg_c1.jsonl"
    monkeypatch.setattr(executor, "_RUNNER_GATE_SHADOW_FILE", str(out))
    a = _analysis(
        breakout_fired=True, volumeSpike=True,
        rsi4h=82.0, adx4h=40.0, ema8_4h=100.0, ema21_4h=90.0,
        close4h=130.0, atr4h=10.0,
    )
    decision = executor._runner_entry_block_reason(a, _gate())
    assert "extension 4.0x ATR" in decision
    rec = json.loads(out.read_text().strip().splitlines()[0])
    assert rec["rsi4h"] == 82.0
    assert rec["adx4h"] == 40.0
    assert rec["extension_4h"] == 4.0
    assert rec["relaxed_by_trend"] is True
    assert rec["admitted"] is False
    assert rec["rule"] == decision


def test_c1_snapshot_failure_does_not_clear_existing_features(
        monkeypatch, tmp_path):
    _isolate(monkeypatch)
    out = tmp_path / "rg_c1_bad.jsonl"
    monkeypatch.setattr(executor, "_RUNNER_GATE_SHADOW_FILE", str(out))
    monkeypatch.setattr(executor, "_runner_gate_late_entry_observation",
                        lambda *a: (_ for _ in ()).throw(ValueError("bad snapshot")))
    a = _analysis(composite_score=20.0, confidence=0.2)
    decision = executor._runner_entry_block_reason(a, _gate())
    assert "runner_gate_blocked" in decision
    rec = json.loads(out.read_text().strip().splitlines()[0])
    assert rec["composite_score"] == 20.0
    assert rec["confidence"] == 0.2
    assert rec["rsi4h"] is None
    assert rec["relaxed_by_trend"] is None


def _isolate_p2(monkeypatch, volume_usd):
    _isolate(monkeypatch)
    monkeypatch.setattr(executor, "cfg_get",
                        lambda key, *a, **k: 50_000_000 if key == "min_short_volume_usd" else None)
    monkeypatch.setattr(executor, "_runner_gate_volume_24h", lambda coin: volume_usd)
    monkeypatch.setattr(executor, "_runner_gate_volume_tier", lambda v: "small")


def test_p2_short_floor_would_block_flag(monkeypatch, tmp_path):
    # Zero-exposure observation: a thin-volume SHORT is flagged as blocked by
    # the 50M floor even though the flag never changes admission.
    out = tmp_path / "rg_p2.jsonl"
    monkeypatch.setattr(executor, "_RUNNER_GATE_SHADOW_FILE", str(out))
    _isolate_p2(monkeypatch, 10_000_000)
    a = _analysis(side="short", composite_score=20.0, confidence=0.2)
    executor._runner_entry_block_reason(a, _gate())
    rec = json.loads(out.read_text().strip().splitlines()[0])
    assert rec["short_floor_usd"] == 50_000_000
    assert rec["short_floor_would_block"] is True


def test_p2_short_high_volume_not_flagged(monkeypatch, tmp_path):
    out = tmp_path / "rg_p2b.jsonl"
    monkeypatch.setattr(executor, "_RUNNER_GATE_SHADOW_FILE", str(out))
    _isolate_p2(monkeypatch, 300_000_000)
    a = _analysis(side="short", composite_score=20.0, confidence=0.2)
    executor._runner_entry_block_reason(a, _gate())
    rec = json.loads(out.read_text().strip().splitlines()[0])
    assert rec["short_floor_usd"] == 50_000_000
    assert rec["short_floor_would_block"] is False


def test_p2_floor_fields_none_for_long(monkeypatch, tmp_path):
    # The floor observation applies to shorts only; long records carry None.
    out = tmp_path / "rg_p2c.jsonl"
    monkeypatch.setattr(executor, "_RUNNER_GATE_SHADOW_FILE", str(out))
    _isolate_p2(monkeypatch, 10_000_000)
    a = _analysis(side="long", composite_score=20.0, confidence=0.2)
    executor._runner_entry_block_reason(a, _gate())
    rec = json.loads(out.read_text().strip().splitlines()[0])
    assert rec["short_floor_usd"] is None
    assert rec["short_floor_would_block"] is None


def test_short_circuit_row_still_records_observability(monkeypatch, tmp_path):
    # U1 (2026-11-04): the short-disabled short-circuit returns before the full
    # feature snapshot, so the entry-observability keys used to land null on
    # ~92% of rows. They must now be snapshotted from analysis (+ a same-source
    # regime call) on that early path, without changing the block.
    _isolate(monkeypatch)
    out = tmp_path / "rg_short_obs.jsonl"
    monkeypatch.setattr(executor, "_RUNNER_GATE_SHADOW_FILE", str(out))
    a = _analysis(
        side="short", composite_score=33.3, confidence=0.55,
        trigger_hits=[{"name": "breakout", "fired": True, "score": 12.5,
                       "direction": "short", "flow_confirmed": False}],
        composite_denominator=9.0,
        flow_confirm_raw=0.42, flow_data_available=True,
        rsi4h=28.0, adx4h=40.0, ema8_4h=90.0, ema21_4h=100.0,
        close4h=60.0, atr4h=10.0,
    )
    decision = executor._runner_entry_block_reason(
        a, _gate(allow_shorts=False))
    assert "shorts disabled" in decision  # decision unchanged
    rec = json.loads(out.read_text().strip().splitlines()[0])
    assert rec["admitted"] is False
    # passthrough keys survive the early return
    assert rec["trigger_hits"] == a["trigger_hits"]
    assert rec["composite_denominator"] == 9.0
    assert rec["flow_confirm_raw"] == 0.42
    assert rec["flow_data_available"] is True
    assert rec["flow_confirmed_breakout"] is False
    # both regimes resolved (detect_regime_with_score is mocked in _isolate)
    assert rec["regime_decision"] in ("up", "down", "neutral", "chop")
    assert rec["regime_gate"] == "up"  # mocked detect_regime_with_score
    assert rec["composite_score"] == 33.3
    assert rec["rsi4h"] == 28.0
    assert rec["adx4h"] == 40.0
    assert rec["extension_4h"] == -4.0
    assert rec["relaxed_by_trend"] is True


def _capture_entry_chase(monkeypatch, analysis):
    rows = []
    monkeypatch.setattr(executor, "_record_risk_tuning_shadow",
                        lambda **kwargs: rows.append(kwargs))
    side = analysis["side"]
    observation = executor._runner_gate_late_entry_observation(
        analysis, side, _gate())
    executor._record_entry_chase_shadows(analysis, side, observation)
    return observation, {
        row["detail"]["arm"]: row["detail"]
        for row in rows if row["rule"] == "entry_chase"
    }


def test_c1_extension_uses_ema21_then_ema8_fallback():
    analysis = _analysis(
        rsi4h=70.0, adx4h=20.0, ema8_4h=100.0, ema21_4h=90.0,
        close4h=130.0, atr4h=10.0,
    )
    observation = executor._runner_gate_late_entry_observation(
        analysis, "long", _gate())
    assert observation["extension_4h"] == 4.0
    assert observation["ema_basis"] == "ema21_4h"

    analysis["ema21_4h"] = None
    fallback = executor._runner_gate_late_entry_observation(
        analysis, "long", _gate())
    assert fallback["extension_4h"] == 3.0
    assert fallback["ema_basis"] == "ema8_4h"


def test_c2_joint_block_and_c3_release_boundaries(monkeypatch):
    at_boundary = _analysis(
        rsi4h=75.0, adx4h=20.0, ema8_4h=90.0, ema21_4h=90.0,
        close4h=97.5, atr4h=2.5,
    )
    _, arms = _capture_entry_chase(monkeypatch, at_boundary)
    assert arms["c2_joint_block"]["would_block"] is True
    assert arms["c3_conditional_release"]["would_admit"] is False

    release = _analysis(
        rsi4h=65.0, adx4h=20.0, ema8_4h=90.0, ema21_4h=90.0,
        close4h=95.0, atr4h=2.5,
    )
    _, arms = _capture_entry_chase(monkeypatch, release)
    assert arms["c2_joint_block"]["would_block"] is False
    assert arms["c3_conditional_release"]["would_admit"] is True


def test_c5_strong_trend_relaxation_and_short_mirror(monkeypatch):
    long_analysis = _analysis(
        rsi4h=76.0, adx4h=40.0, ema8_4h=102.0, ema21_4h=100.0,
        close4h=106.0, atr4h=2.0,
    )
    _, arms = _capture_entry_chase(monkeypatch, long_analysis)
    c5 = arms["c5_strong_trend_relax"]
    assert c5["relaxed_by_trend"] is True
    assert c5["would_block"] is False
    assert c5["strict_would_block"] is True
    assert c5["relaxation_would_admit"] is True

    short_analysis = _analysis(
        side="short", rsi4h=24.0, adx4h=40.0,
        ema8_4h=98.0, ema21_4h=100.0, close4h=94.0, atr4h=2.0,
    )
    _, arms = _capture_entry_chase(monkeypatch, short_analysis)
    assert arms["c2_joint_block"]["chase_rsi"] == 76.0
    assert arms["c2_joint_block"]["chase_extension"] == 3.0
    assert arms["c2_joint_block"]["would_block"] is True
    c5 = arms["c5_strong_trend_relax"]
    assert c5["would_block"] is False
    assert c5["strict_would_block"] is True
    assert c5["relaxation_would_admit"] is True


def test_chase_shadow_missing_values_are_not_false(monkeypatch):
    analysis = _analysis(
        rsi4h=76.0, adx4h=40.0, ema8_4h=102.0, ema21_4h=100.0,
        close4h=None, atr4h=2.0,
    )
    _, arms = _capture_entry_chase(monkeypatch, analysis)
    assert arms["c2_joint_block"]["would_block"] is None
    assert arms["c3_conditional_release"]["would_admit"] is None
    c5 = arms["c5_strong_trend_relax"]
    assert c5["data_status"] == "missing"
    assert c5["missing_fields"] == ["extension_4h"]
    assert c5["would_block"] is None
    assert c5["extension_would_block"] is None


def test_c2_c3_c5_record_before_live_early_returns(monkeypatch, tmp_path):
    out = tmp_path / "rg_chase_preblock.jsonl"
    _isolate(monkeypatch)
    monkeypatch.setattr(executor, "_RUNNER_GATE_SHADOW_FILE", str(out))
    rows = []
    monkeypatch.setattr(executor, "_record_risk_tuning_shadow",
                        lambda **kwargs: rows.append(kwargs))
    analysis = _analysis(
        id="chase-low-conf", composite_score=20.0, confidence=0.2,
        rsi4h=76.0, adx4h=40.0, ema8_4h=102.0, ema21_4h=100.0,
        close4h=106.0, atr4h=2.0,
    )
    decision = executor._runner_entry_block_reason(analysis, _gate())
    assert decision == "runner_gate_blocked (confidence 0.20 < 0.70)"
    assert {row["detail"]["arm"] for row in rows} == {
        "c2_joint_block", "c3_conditional_release", "c5_strong_trend_relax"}

    rows.clear()
    short = _analysis(side="short", rsi4h=24.0, adx4h=40.0,
                      ema8_4h=98.0, ema21_4h=100.0,
                      close4h=94.0, atr4h=2.0)
    decision = executor._runner_entry_block_reason(
        short, _gate(allow_shorts=False))
    assert "shorts disabled" in decision
    assert len([row for row in rows if row["rule"] == "entry_chase"]) == 3


def test_chase_shadow_failure_cannot_change_live_gate(monkeypatch, tmp_path):
    out = tmp_path / "rg_chase_failure.jsonl"
    _isolate(monkeypatch)
    monkeypatch.setattr(executor, "_RUNNER_GATE_SHADOW_FILE", str(out))
    monkeypatch.setattr(
        executor, "_record_entry_chase_shadows",
        lambda *args: (_ for _ in ()).throw(RuntimeError("shadow write failed")))
    analysis = _analysis(composite_score=20.0, confidence=0.2)
    decision = executor._runner_entry_block_reason(analysis, _gate())
    assert decision == "runner_gate_blocked (confidence 0.20 < 0.70)"


def test_record_failure_never_breaks_gate(monkeypatch, tmp_path):
    _isolate(monkeypatch)
    # Point at an unwritable path; append_jsonl swallows the OSError, and the
    # gate must still return its normal decision.
    monkeypatch.setattr(executor, "_RUNNER_GATE_SHADOW_FILE",
                        "/nonexistent-dir-xyz/should/not/write.jsonl")
    a = _analysis(composite_score=20.0, confidence=0.2)
    decision = executor._runner_entry_block_reason(a, _gate())
    assert "runner_gate_blocked" in decision


# ── C4: post-plunge regime is observation-only ─────────────────────────────

def test_c4_shadow_records_before_confidence_block_without_changing_decision(monkeypatch, tmp_path):
    out = tmp_path / "rg_c4.jsonl"
    _isolate(monkeypatch)
    import hermes_trader.agents.market_regime as mr
    monkeypatch.setattr(mr, "detect_regime", lambda coin: "up")
    monkeypatch.setattr(executor, "coin_breakout_regime", lambda analysis: "")
    monkeypatch.setattr(executor, "_RUNNER_GATE_SHADOW_FILE", str(out))
    rows = []
    monkeypatch.setattr(executor, "_record_risk_tuning_shadow",
                        lambda **kwargs: rows.append(kwargs))
    a = _analysis(id="c4-runner", composite_score=20.0, confidence=0.2,
                  recent_4h_plunge_data_ok=True, recent_4h_plunge_pct=3.0,
                  recent_4h_plunge_bar_ts=123000)

    decision = executor._runner_entry_block_reason(a, _gate())

    assert decision == "runner_gate_blocked (confidence 0.20 < 0.70)"
    row = next(row for row in rows if row["rule"] == "entry_regime")
    assert row["trace_id"] == "c4-runner"
    assert row["detail"]["regime_old"] == "up"
    assert row["detail"]["regime_candidate"] == "neutral"
    assert row["detail"]["enforced"] is False
    rec = json.loads(out.read_text().strip().splitlines()[0])
    assert rec["admitted"] is False


def test_c4_shadow_failure_cannot_change_live_gate(monkeypatch, tmp_path):
    out = tmp_path / "rg_c4_failure.jsonl"
    _isolate(monkeypatch)
    import hermes_trader.agents.market_regime as mr
    monkeypatch.setattr(mr, "detect_regime", lambda coin: "up")
    monkeypatch.setattr(executor, "coin_breakout_regime", lambda analysis: "")
    monkeypatch.setattr(executor, "_RUNNER_GATE_SHADOW_FILE", str(out))
    monkeypatch.setattr(
        executor, "_record_c4_regime_shadow",
        lambda *args: (_ for _ in ()).throw(RuntimeError("shadow write failed")))
    a = _analysis(composite_score=20.0, confidence=0.2,
                  recent_4h_plunge_data_ok=True, recent_4h_plunge_pct=3.0)

    decision = executor._runner_entry_block_reason(a, _gate())

    assert decision == "runner_gate_blocked (confidence 0.20 < 0.70)"
    rec = json.loads(out.read_text().strip().splitlines()[0])
    assert rec["admitted"] is False


# ── L5-A1: effective exit-policy snapshot on close ──────────────────────────

def test_tracker_policy_snapshot_reads_effective_clocks():
    from types import SimpleNamespace
    from hermes_trader.agents.dsl_exit import ExitPolicy
    from hermes_trader.agents.shadow_book import _tracker_policy_snapshot

    pol = ExitPolicy(protect_pct=1.5, max_loss_pct=1.5, max_loss_roe_pct=15.0,
                     hard_timeout_minutes=120.0, stale_flat_timeout_minutes=60.0,
                     ignite_grace_minutes=90.0)
    trk = SimpleNamespace(policy=pol, leverage=10, entry_atr_pct=2.5)
    snap = _tracker_policy_snapshot(trk)
    assert snap["leverage"] == 10
    assert snap["protect_pct"] == 1.5
    assert snap["max_loss_pct"] == 1.5
    assert snap["max_loss_roe_pct"] == 15.0
    assert snap["hard_timeout_minutes"] == 120.0
    assert snap["stale_flat_timeout_minutes"] == 60.0
    assert snap["ignite_grace_minutes"] == 90.0
    assert snap["entry_atr_pct"] == 2.5


def test_tracker_policy_snapshot_is_best_effort():
    from hermes_trader.agents.shadow_book import _tracker_policy_snapshot
    assert _tracker_policy_snapshot(None) == {}
    # object with no policy attribute -> empty, never raises
    assert _tracker_policy_snapshot(object()) == {}


def test_close_fill_carries_policy_snapshot_without_pnl_change(tmp_path):
    """Passing policy_snapshot into _close_position adds attribution only;
    the realized ROE/PnL fields are identical to a no-snapshot close."""
    import os
    from hermes_trader.agents.shadow_book import ShadowBook

    path = str(tmp_path / "book.json")
    book = ShadowBook(path)
    book.reload_if_changed()
    with book._lock:
        acc = book._account("taker")
        acc["positions"].append({
            "id": "p1", "coin": "TST", "side": "long",
            "entry_px": 100.0, "size_usd": 1000.0, "size_coin": 10.0,
            "leverage": 3, "opened_at": __import__("time").time() * 1000 - 60_000,
            "entry_regime": "trend", "analysis_id": "",
        })
        pos = acc["positions"][0]
        snap = {"leverage": 3, "max_loss_pct": 4.0, "max_loss_roe_pct": 20.0,
                "stale_flat_timeout_minutes": 120, "hard_timeout_minutes": 240,
                "ignite_grace_minutes": 90, "protect_pct": 1.5,
                "entry_atr_pct": 2.0}
        fill = book._close_position("taker", pos, 105.0, "floor_breach",
                                    hold_min=1.0, mfe_pct=6.0,
                                    policy_snapshot=snap)
    assert fill["exit_policy"]["stale_flat_timeout_minutes"] == 120
    assert fill["entry_regime"] == "trend"
    assert fill["reason"] == "floor_breach"
    assert fill["exit_policy"]["max_loss_roe_pct"] == 20.0
    # Attribution is additive: realized ROE derives from the fill's own
    # spot move and recorded fee, independent of exit_policy being present.
    assert fill["spot_pct"] == round((fill["price"] - 100.0) / 100.0 * 100.0, 4)
    assert fill["realized_pnl_pct"] == round(
        fill["spot_pct"] * 3 - (fill["fee_usd"] / 1000.0 * 100.0) * 3, 4)

