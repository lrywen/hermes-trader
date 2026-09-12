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


# ── (5) per-coin cooldown: ENFORCE really blocks ─────────────────────────

def test_per_coin_consecutive_loss_enforce_blocks(monkeypatch):
    """shadow_mode=false + arm configured: a candidate that trips the
    consecutive-loss condition is really blocked at the runner gate."""
    recs = []
    monkeypatch.setattr(executor, "_record_risk_tuning_shadow",
                        lambda **kw: recs.append(kw))

    class _Mem:
        def get_closes(self, limit=200):
            return []
        def consecutive_losses(self, coin):
            return 2

    monkeypatch.setattr(executor, "memory", _Mem())
    cfg = _base_gate(per_coin_cooldown={
        "shadow_mode": False, "window_hours": 24,
        "repeat_min_composite": 45, "max_consecutive_losses": 2,
        "loss_cooldown_hours": 24})

    a = _zec_analysis(score=60.0)
    a.update(breakout_fired=False, volume_spike_fired=True,
             momentum_burst_fired=True)
    reason = executor._runner_entry_block_reason(a, cfg)

    assert reason.startswith("runner_gate_blocked (per-coin cooldown:")
    assert "consecutive_losses 2" in reason
    # shadow record is still written for gray-release reconciliation.
    assert any(r["rule"] == "per_coin_cooldown" and r["would"] == "block"
               for r in recs)


def test_per_coin_repeat_low_score_enforce_blocks(monkeypatch):
    import time as _t
    now_ms = int(_t.time() * 1000)
    monkeypatch.setattr(executor, "_record_risk_tuning_shadow",
                        lambda **kw: None)

    class _Mem:
        def get_closes(self, limit=200):
            return [{"coin": "ZEC", "closed_at": now_ms - 3600_000,
                     "realized_pnl_usd": 0.1}]
        def consecutive_losses(self, coin):
            return 0

    monkeypatch.setattr(executor, "memory", _Mem())
    cfg = _base_gate(per_coin_cooldown={
        "shadow_mode": False, "window_hours": 24,
        "repeat_min_composite": 45, "max_consecutive_losses": 2})

    reason = executor._runner_entry_block_reason(_zec_analysis(), cfg)
    assert reason.startswith("runner_gate_blocked (per-coin cooldown:")
    assert "repeat entry within 24h" in reason


def test_per_coin_enforce_clean_candidate_admits(monkeypatch):
    """ENFORCE must not block a first entry / clean coin: no recent closes,
    zero consecutive losses."""
    monkeypatch.setattr(executor, "_record_risk_tuning_shadow",
                        lambda **kw: None)

    class _Mem:
        def get_closes(self, limit=200):
            return []
        def consecutive_losses(self, coin):
            return 0

    monkeypatch.setattr(executor, "memory", _Mem())
    cfg = _base_gate(per_coin_cooldown={
        "shadow_mode": False, "window_hours": 24,
        "repeat_min_composite": 45, "max_consecutive_losses": 2})

    a = _zec_analysis(score=60.0)
    a.update(breakout_fired=False, volume_spike_fired=True,
             momentum_burst_fired=True)
    assert executor._runner_entry_block_reason(a, cfg) == ""


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


# ── (6) leverage tier: ENFORCE really de-levers (full maybe_execute) ─────

class _NeutralMemory:
    """Neutral memory: no disk, no cooldowns, zero pnl (mirrors sizing test)."""

    def __getattr__(self, name):
        def _stub(*_a, **_k):
            return None
        return _stub

    def avg_exit_slip_bps(self, coin, days=None):
        return 0.0

    def avg_exit_slip_bps_side(self, coin, side, days=None, min_samples=None,
                               default_bps=2.0):
        return default_bps, "default"

    def avg_hold_hours_side(self, coin, side, days=None, min_samples=None,
                            default_hours=8.0):
        return default_hours, "default"

    def avg_round_trip_fee_bps(self, coin, days=None, min_samples=None):
        return 0.0

    def loss_cooldown_remaining_min(self, coin):
        return 0

    def get_daily_pnl(self):
        return 0.0

    def peak_daily_pnl(self):
        return 0.0

    def daily_realized_pnl(self):
        return 0.0

    def peak_daily_realized_pnl(self):
        return 0.0

    def get_recent_trades(self, n=10):
        return []

    def track_daily_pnl(self, equity, net_contributions=0.0):
        return None


def _wire_leverage_case(monkeypatch, tier_cfg):
    """Drive maybe_execute in bot SHADOW (paper) mode; capture the leverage
    the would-be order is paper-booked with."""
    from hermes_trader.agents import market_regime, shadow_book

    cfg = {
        "mode": "SHADOW", "enable_crypto": True,
        "leverage": 10,
        "max_trade_notional_usd": 0,
        "max_concurrent": 9999,
        "min_market_volume_usd": 0,
        "min_hip3_volume_usd": 0,
        "min_short_volume_usd": 0,
        "max_total_notional_pct": 50.0,
        "max_daily_loss_usd": -1_000_000_000,
        "min_ai_confidence": 0.0,
        "aligned_min_conf": None,
        "min_trend_score": 0.0,
        "coin_allowlist": [],
        "coin_blocklist": [],
        "max_crypto_long_correlated": 9999,
        "cooldown_min": 0,
        "counter_regime_min_conf": 0.0,
        "block_counter_trend_bypass": False,
        "crowded_with_min_conf": 0.0,
        "debate_gate": {"enabled": False},
        "news_blackout": {"enabled": False},
        "circuit_breaker": {"consecutive_loss_limit": 0,
                            "coin_daily_loss_pct": 0.0,
                            "max_drawdown_pct": 0.0},
        "liquidation_maint_margin_pct": 1.0,
        "sl_buffer_bps": 10.0,
        "dsl_exit": {
            "max_loss_pct": 2.5, "max_loss_roe_pct": 25.0,
            "atr_stop": {"enabled": True, "atr_mult": 0.5,
                         "floor_pct": 1.0, "ceiling_pct": 4.0},
        },
        "atr_risk_sizing": {"enabled": False},
        "leverage_tier_shadow": tier_cfg,
    }
    monkeypatch.setattr(executor, "read_agent_config", lambda: dict(cfg))
    monkeypatch.setattr(executor, "memory", _NeutralMemory())
    monkeypatch.setattr(executor, "get_max_leverage", lambda _c: 10)
    monkeypatch.setattr(executor, "resolve_user_address", lambda: "0xUSER")
    monkeypatch.setattr(executor, "fetch_account_state",
                        lambda *_a, **_k: {"equity": 1000.0, "available": 900.0,
                                           "total_ntl": 0.0, "asset_positions": []})
    monkeypatch.setattr(executor, "get_hl_price", lambda _c: 100.0)
    monkeypatch.setattr(executor, "get_hl_atr", lambda *_a, **_k: 2.0)
    monkeypatch.setattr(executor, "min_entry_notional_usd", lambda _c, _m: 0.0)
    monkeypatch.setattr(executor, "entry_size_for_notional",
                        lambda _c, n, m: n / m)
    monkeypatch.setattr(market_regime, "detect_regime",
                        lambda *_a, **_k: "neutral")
    monkeypatch.setattr(executor, "get_atr_hist_mean_pct",
                        lambda *_a, **_k: 2.0)
    captured = {}
    monkeypatch.setattr(shadow_book, "shadow_open",
                        lambda **kw: captured.update(kw))
    monkeypatch.setattr(executor, "place_hl_order",
                        lambda *_a, **_k: {"status": "ok", "oid": "x"})
    return captured


def _lev_analysis():
    # 4h ATR% = 4.0 (> atr_pct_max 3.5) at close 100; score 26.5 (< 40).
    return {
        "id": "levtest", "coin": "TEST", "action": "LONG", "side": "long",
        "confidence": 0.9, "composite_score": 80,
        "atr4h": 4.0, "close4h": 100.0,
        "entry_px": 100.0, "stop_px": 99.0, "tp_px": 110.0,
        "reasoning": "leverage tier enforce test",
    }


def test_leverage_tier_enforce_de_levers_paper_order(monkeypatch):
    """shadow_mode=false: a high-ATR candidate is really de-levered to the
    low tier; the would-be paper order carries 5x, and the reconciliation
    record is flagged enforced."""
    recs = []
    monkeypatch.setattr(executor, "_record_risk_tuning_shadow",
                        lambda **kw: recs.append(kw))
    captured = _wire_leverage_case(
        monkeypatch,
        {"shadow_mode": False, "atr_pct_max": 3.5,
         "min_composite": 40, "low_leverage": 5})

    res = executor.maybe_execute(_lev_analysis())
    assert res.get("reason") == "shadow_mode_would_execute"
    assert captured["leverage"] == 5
    r = [x for x in recs if x["rule"] == "leverage_tier"]
    assert len(r) == 1
    assert r[0]["detail"]["live_leverage"] == 10
    assert r[0]["detail"]["proposed_leverage"] == 5
    assert r[0]["detail"]["enforced"] is True


def test_leverage_tier_shadow_keeps_live_leverage(monkeypatch):
    """shadow_mode=true: live leverage stays 10x; record is not enforced."""
    recs = []
    monkeypatch.setattr(executor, "_record_risk_tuning_shadow",
                        lambda **kw: recs.append(kw))
    captured = _wire_leverage_case(
        monkeypatch,
        {"shadow_mode": True, "atr_pct_max": 3.5,
         "min_composite": 40, "low_leverage": 5})

    res = executor.maybe_execute(_lev_analysis())
    assert res.get("reason") == "shadow_mode_would_execute"
    assert captured["leverage"] == 10
    r = [x for x in recs if x["rule"] == "leverage_tier"]
    assert len(r) == 1
    assert r[0]["detail"]["enforced"] is False
