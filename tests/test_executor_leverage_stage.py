"""P1-1 step ③ — characterization for the S8 leverage helpers.

Pins two stages extracted from maybe_execute:
- _resolve_coin_max_leverage: exchange max leverage lookup that FAILS CLOSED
  (returns a reason, never raises) when the coin is absent from universe meta.
- _apply_leverage_tier: volatility/score de-leverage tier, which in shadow
  mode records-only and in enforce mode genuinely lowers leverage, and always
  fails OPEN on an internal error.
"""
from __future__ import annotations

import sys
import types

from hermes_trader.agents import executor

resolve = executor._resolve_coin_max_leverage
apply_tier = executor._apply_leverage_tier


# ── _resolve_coin_max_leverage ────────────────────────────────────────────

def test_resolve_max_leverage_ok(monkeypatch):
    monkeypatch.setattr(executor, "get_max_leverage", lambda coin: 20)
    assert resolve("ETH") == (20, None)


def test_resolve_max_leverage_fail_closed_on_missing_coin(monkeypatch):
    def _boom(coin):
        raise ValueError(f"unknown coin {coin}")

    monkeypatch.setattr(executor, "get_max_leverage", _boom)
    max_lev, reason = resolve("DOGE")
    assert max_lev is None
    assert reason == "unknown_max_leverage_DOGE"


# ── _apply_leverage_tier ──────────────────────────────────────────────────

def _tier_cfg(**over):
    base = {"atr_pct_max": 3.5, "min_composite": 40.0, "low_leverage": 5}
    base.update(over)
    return {"leverage_tier_shadow": base}


def _analysis(**over):
    base = {"id": "a1", "coin": "ZEC", "side": "short",
            "atr4h": 4.0, "close4h": 100.0,   # atr% = 4.0 > 3.5
            "composite_score": 30.0}          # 30 < 40
    base.update(over)
    return base


def test_tier_unconfigured_returns_leverage_unchanged():
    assert apply_tier(10, _analysis(), {}) == 10
    assert apply_tier(10, _analysis(), {"leverage_tier_shadow": {}}) == 10


def test_tier_shadow_records_but_keeps_live_leverage(monkeypatch):
    recorded = {}
    monkeypatch.setattr(
        executor, "_record_risk_tuning_shadow",
        lambda **kw: recorded.update(kw))
    cfg = _tier_cfg(shadow_mode=True)
    out = apply_tier(10, _analysis(), cfg)
    assert out == 10                      # live leverage untouched
    assert recorded["rule"] == "leverage_tier"
    assert recorded["detail"]["enforced"] is False
    assert recorded["detail"]["proposed_leverage"] == 5


def test_tier_enforce_lowers_leverage(monkeypatch):
    recorded = {}
    monkeypatch.setattr(
        executor, "_record_risk_tuning_shadow",
        lambda **kw: recorded.update(kw))
    cfg = _tier_cfg(shadow_mode=False)
    out = apply_tier(10, _analysis(), cfg)
    assert out == 5                       # genuinely de-levered
    assert recorded["detail"]["enforced"] is True


def test_tier_no_trigger_leaves_leverage(monkeypatch):
    calls = []
    monkeypatch.setattr(
        executor, "_record_risk_tuning_shadow",
        lambda **kw: calls.append(kw))
    # Low ATR% (1.0) and high score (80) → no de-leverage reason.
    out = apply_tier(10, _analysis(atr4h=1.0, composite_score=80.0),
                     _tier_cfg(shadow_mode=False))
    assert out == 10
    assert calls == []


def test_tier_does_not_increase_leverage(monkeypatch):
    # Trigger conditions met, but live leverage (3) already <= low tier (5):
    # no de-leverage record, leverage unchanged.
    calls = []
    monkeypatch.setattr(
        executor, "_record_risk_tuning_shadow",
        lambda **kw: calls.append(kw))
    out = apply_tier(3, _analysis(), _tier_cfg(shadow_mode=False))
    assert out == 3
    assert calls == []


def test_tier_bad_atr_inputs_ignored_still_uses_score(monkeypatch):
    recorded = {}
    monkeypatch.setattr(
        executor, "_record_risk_tuning_shadow",
        lambda **kw: recorded.update(kw))
    # Non-numeric 4h inputs must not raise; score 30 < 40 still triggers.
    out = apply_tier(10, _analysis(atr4h="junk", close4h=None),
                     _tier_cfg(shadow_mode=True))
    assert out == 10
    assert recorded["detail"]["atr_pct_4h"] is None


def test_tier_fails_open_on_internal_error(monkeypatch):
    # A shadow-trace that raises must not propagate: fail open at original lev.
    def _boom(**kw):
        raise RuntimeError("trace sink broken")

    monkeypatch.setattr(executor, "_record_risk_tuning_shadow", _boom)
    # Also neutralize event_log.append so the best-effort trace can't raise.
    fake_event = types.SimpleNamespace(append=lambda *a, **k: None)
    monkeypatch.setitem(sys.modules, "hermes_trader.event_log", fake_event)
    out = apply_tier(10, _analysis(), _tier_cfg(shadow_mode=False))
    assert out == 10
