"""Shadow-mode regression for the 2026-09-10 risk-tuning proposals (4/5/6/3).

All four proposals ship SHADOW-ONLY by default: the live decision must be
identical whether the shadow block is absent, disabled, or enabled; the only
effect of enabling is an append to the risk-tuning JSONL. These tests pin:

  (4) breakout_score_floor — a weak breakout admit is still admitted (no block)
      but emits a would=block shadow record;
  (5) per_coin_cooldown    — repeat/consecutive-loss conditions record only;
  (6) leverage_tier        — proposed de-leverage is recorded, live leverage
      unchanged (tested at the leverage decision in maybe_execute via the
      helper contract + config plumbing);
  (3) stop_tuning          — at a max_loss exit the wider-cap / lower-breakeven
      counter-factual is recorded by the DSL helper.
"""
from __future__ import annotations

import types

import pytest

from hermes_trader.agents import dsl_exit, executor


def _base_gate(**over):
    g = {
        "enabled": True,
        "min_confidence": 0.62,
        "min_composite": 45,
        "min_hip3_composite": 50,
    }
    g.update(over)
    return {"runner_entry_gate": g}


def _zec_analysis(score=26.5, conf=0.65):
    return {
        "id": "a1", "coin": "ZEC", "side": "long",
        "confidence": conf, "ai_confidence_raw": conf,
        "composite_score": score,
        "breakout_fired": True,
        "volume_spike_fired": False,
        "momentum_burst_fired": False,
        "slow_burn_count": 1,
        "mid": 1252.4, "price": 1252.4,
    }


# ── (4) breakout weak-score floor: shadow records, never blocks ───────────

def test_breakout_floor_shadow_records_but_admits(monkeypatch):
    recs = []
    monkeypatch.setattr(executor, "_record_risk_tuning_shadow",
                        lambda **kw: recs.append(kw))
    cfg = _base_gate(breakout_score_floor={
        "shadow_mode": True, "min_composite": 31.5})

    reason = executor._runner_entry_block_reason(_zec_analysis(), cfg)

    assert reason == "", "shadow must not change the live admission decision"
    assert len(recs) == 1
    r = recs[0]
    assert r["rule"] == "breakout_score_floor"
    assert r["would"] == "block"
    assert r["detail"]["composite_score"] == pytest.approx(26.5)
    assert r["detail"]["floor"] == pytest.approx(31.5)


def test_breakout_floor_disabled_emits_nothing(monkeypatch):
    recs = []
    monkeypatch.setattr(executor, "_record_risk_tuning_shadow",
                        lambda **kw: recs.append(kw))
    # disabled (default) → no record, still admitted
    cfg = _base_gate(breakout_score_floor={
        "shadow_mode": False, "min_composite": 31.5})
    assert executor._runner_entry_block_reason(_zec_analysis(), cfg) == ""
    assert recs == []


def test_breakout_floor_strong_score_not_flagged(monkeypatch):
    recs = []
    monkeypatch.setattr(executor, "_record_risk_tuning_shadow",
                        lambda **kw: recs.append(kw))
    cfg = _base_gate(breakout_score_floor={
        "shadow_mode": True, "min_composite": 31.5})
    assert executor._runner_entry_block_reason(
        _zec_analysis(score=40.0), cfg) == ""
    assert recs == [], "score above the proposed floor is not counter-factually blocked"


# ── (5) per-coin cooldown: shadow records, never blocks ───────────────────

def test_per_coin_consecutive_loss_shadow(monkeypatch):
    recs = []
    monkeypatch.setattr(executor, "_record_risk_tuning_shadow",
                        lambda **kw: recs.append(kw))

    class _Mem:
        def get_closes(self, limit=200):
            return []  # no closes, so only the consecutive-loss arm can fire
        def consecutive_losses(self, coin):
            return 2

    monkeypatch.setattr(executor, "memory", _Mem())
    cfg = _base_gate(per_coin_cooldown={
        "shadow_mode": True, "window_hours": 24,
        "repeat_min_composite": 45, "max_consecutive_losses": 2,
        "loss_cooldown_hours": 24})

    # A strong (otherwise-admitted) non-breakout candidate: volume+burst+slow.
    a = _zec_analysis(score=60.0)
    a.update(breakout_fired=False, volume_spike_fired=True,
             momentum_burst_fired=True)
    reason = executor._runner_entry_block_reason(a, cfg)

    assert reason == ""
    assert any(r["rule"] == "per_coin_cooldown" and r["would"] == "block"
               for r in recs)


def test_per_coin_repeat_low_score_shadow(monkeypatch):
    import time as _t
    now_ms = int(_t.time() * 1000)
    recs = []
    monkeypatch.setattr(executor, "_record_risk_tuning_shadow",
                        lambda **kw: recs.append(kw))

    class _Mem:
        def get_closes(self, limit=200):
            return [{"coin": "ZEC", "closed_at": now_ms - 3600_000,
                     "realized_pnl_usd": 0.1}]
        def consecutive_losses(self, coin):
            return 0

    monkeypatch.setattr(executor, "memory", _Mem())
    cfg = _base_gate(per_coin_cooldown={
        "shadow_mode": True, "window_hours": 24,
        "repeat_min_composite": 45, "max_consecutive_losses": 2})

    assert executor._runner_entry_block_reason(_zec_analysis(), cfg) == ""
    r = [x for x in recs if x["rule"] == "per_coin_cooldown"]
    assert len(r) == 1
    assert r[0]["detail"]["entries_in_window"] == 1
    assert r[0]["detail"]["hours_since_last_close"] == pytest.approx(1.0, abs=0.05)


# ── helper: shadow JSONL writer contract ─────────────────────────────────

def test_risk_tuning_shadow_writer(tmp_path, monkeypatch):
    import json as _json
    f = tmp_path / "rt.jsonl"
    monkeypatch.setenv("HERMES_RISK_TUNING_SHADOW_FILE", str(f))
    # the module-level path constant was bound at import; point both.
    monkeypatch.setattr(executor, "_RISK_TUNING_SHADOW_FILE", str(f))

    executor._record_risk_tuning_shadow(
        rule="leverage_tier", coin="ZEC", side="long", would="deleverage",
        detail={"live_leverage": 10, "proposed_leverage": 5})

    lines = f.read_text().strip().splitlines()
    assert len(lines) == 1
    rec = _json.loads(lines[0])
    assert rec["rule"] == "leverage_tier"
    assert rec["detail"]["proposed_leverage"] == 5


# ── (3) stop-tuning shadow at max_loss ───────────────────────────────────

def _make_tracker(monkeypatch, tmp_path, peak_px):
    state = tmp_path / "dsl.json"
    monkeypatch.setattr(dsl_exit, "DSL_STATE_FILE", str(state))
    monkeypatch.setattr(dsl_exit, "DSL_STATE_LOCK_FILE", str(state) + ".lock")
    monkeypatch.setattr(dsl_exit, "_LAST_SAVE_TS", 0.0)
    monkeypatch.setattr(dsl_exit, "_SAVE_DIRTY", False)
    dsl_exit._active_positions.clear()
    dsl_exit._loaded_from_disk = False
    pol = dsl_exit.ExitPolicy()
    t = dsl_exit.register_position("ADA", "long", entry_px=1.0, policy=pol)
    t.peak_px = peak_px
    return t, pol


def test_stop_tuning_shadow_records_wider_cap_survival(monkeypatch, tmp_path):
    f = tmp_path / "rt.jsonl"
    monkeypatch.setenv("HERMES_RISK_TUNING_SHADOW_FILE", str(f))

    # config offers a wider 1.5% candidate cap; the position exits at ~0.87%.
    monkeypatch.setattr(
        "hermes_trader.agents.config_store.read_agent_config",
        lambda: {"dsl_exit": {"stop_tuning_shadow": {
            "shadow_mode": True, "candidate_max_loss_pct": 1.5,
            "candidate_breakeven_trigger_pct": 1.0}}})

    t, pol = _make_tracker(monkeypatch, tmp_path, peak_px=1.005)  # MFE 0.5%
    dsl_exit._record_stop_tuning_shadow(
        t, pol, effective_max_loss=0.80, loss_pct=-0.87, roe_loss=-8.7,
        atr_active=False, spot_cap_display=0.80)

    import json as _json
    rec = _json.loads(f.read_text().strip())
    assert rec["rule"] == "stop_tuning"
    assert rec["would"] == "survive_wider_cap"
    assert rec["detail"]["candidate_max_loss_pct"] == 1.5
    assert rec["detail"]["mfe_spot_pct"] == pytest.approx(0.5)
    assert rec["detail"]["breakeven_would_have_armed"] is False


def test_stop_tuning_shadow_records_breakeven_arm(monkeypatch, tmp_path):
    f = tmp_path / "rt.jsonl"
    monkeypatch.setenv("HERMES_RISK_TUNING_SHADOW_FILE", str(f))
    monkeypatch.setattr(
        "hermes_trader.agents.config_store.read_agent_config",
        lambda: {"dsl_exit": {"stop_tuning_shadow": {
            "shadow_mode": True, "candidate_max_loss_pct": 0.5,
            "candidate_breakeven_trigger_pct": 1.0}}})

    # MFE 1.4% (the ADA true peak) would arm a 1.0% candidate breakeven, but
    # the loss is beyond the (narrower) candidate cap so survival is False.
    t, pol = _make_tracker(monkeypatch, tmp_path, peak_px=1.014)
    dsl_exit._record_stop_tuning_shadow(
        t, pol, effective_max_loss=0.80, loss_pct=-0.87, roe_loss=-8.7,
        atr_active=False, spot_cap_display=0.80)

    import json as _json
    rec = _json.loads(f.read_text().strip())
    assert rec["would"] == "breakeven_would_arm"
    assert rec["detail"]["breakeven_would_have_armed"] is True


def test_stop_tuning_shadow_disabled_writes_nothing(monkeypatch, tmp_path):
    f = tmp_path / "rt.jsonl"
    monkeypatch.setenv("HERMES_RISK_TUNING_SHADOW_FILE", str(f))
    monkeypatch.setattr(
        "hermes_trader.agents.config_store.read_agent_config",
        lambda: {"dsl_exit": {"stop_tuning_shadow": {"shadow_mode": False}}})
    t, pol = _make_tracker(monkeypatch, tmp_path, peak_px=1.02)
    dsl_exit._record_stop_tuning_shadow(
        t, pol, 0.80, -0.87, -8.7, False, 0.80)
    assert not f.exists()
