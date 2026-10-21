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


def test_record_failure_never_breaks_gate(monkeypatch, tmp_path):
    _isolate(monkeypatch)
    # Point at an unwritable path; append_jsonl swallows the OSError, and the
    # gate must still return its normal decision.
    monkeypatch.setattr(executor, "_RUNNER_GATE_SHADOW_FILE",
                        "/nonexistent-dir-xyz/should/not/write.jsonl")
    a = _analysis(composite_score=20.0, confidence=0.2)
    decision = executor._runner_entry_block_reason(a, _gate())
    assert "runner_gate_blocked" in decision


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

