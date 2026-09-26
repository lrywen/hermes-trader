"""Tests for the single-source decision regime and its OBSERVATION-ONLY probe.

The hard direction gate (LONG-in-DOWN / SHORT-in-UP) was reverted to a
counterfactual shadow probe after a 106-trade replay showed it blocked a
net-positive half. These tests cover:
  * resolve_decision_regime resolves once, is cached, applies the coin breakout
    override;
  * regime_direction_observe sets would_block correctly but NEVER blocks;
  * the block shim regime_direction_block always returns "" (allows entry).

Macro detection and the coin breakout override are stubbed at their source
modules; shadow writes go to a tmp path so no /data side effects.
"""

import pytest

from hermes_trader.agents import executor


@pytest.fixture
def patched(monkeypatch):
    import hermes_trader.agents.market_regime as mr
    state = {"macro": "neutral", "coin": ""}
    monkeypatch.setattr(mr, "detect_regime", lambda coin: state["macro"])
    monkeypatch.setattr(executor, "coin_breakout_regime",
                        lambda analysis: state["coin"])
    # neutralize the shadow file write (no /data side effects)
    import hermes_trader.shadow_log as sl
    monkeypatch.setattr(sl, "append_jsonl", lambda path, rec, stream="shadow": True)
    return state


def _resolve(analysis, patched):
    analysis.pop("_decision_regime", None)
    return executor.resolve_decision_regime(analysis, {})


def test_resolve_uses_macro(patched):
    patched["macro"] = "up"
    assert _resolve({"coin": "T"}, patched) == "up"


def test_coin_breakout_overrides_macro(patched):
    patched["macro"] = "neutral"
    patched["coin"] = "up"
    assert _resolve({"coin": "T"}, patched) == "up"


def test_resolve_cached_and_stable(patched):
    patched["macro"] = "down"
    analysis = {"coin": "T"}
    first = executor.resolve_decision_regime(analysis, {})
    patched["macro"] = "up"
    second = executor.resolve_decision_regime(analysis, {})
    assert first == second == "down"


def _observe(analysis, patched):
    _resolve(analysis, patched)
    return executor.regime_direction_observe(analysis, {})


def test_long_in_down_would_block_but_not_blocked(patched):
    patched["macro"] = "down"
    analysis = {"coin": "T", "side": "long"}
    rec = _observe(analysis, patched)
    assert rec["would_block"] is True
    # observation only: the entry is still allowed
    assert executor.regime_direction_block(analysis, {}) == ""


def test_short_in_up_would_block(patched):
    patched["macro"] = "up"
    rec = _observe({"coin": "T", "side": "short"}, patched)
    assert rec["would_block"] is True


def test_aligned_long_in_up_not_would_block(patched):
    patched["macro"] = "up"
    rec = _observe({"coin": "T", "side": "long"}, patched)
    assert rec["would_block"] is False


def test_neutral_regime_never_would_block(patched):
    patched["macro"] = "neutral"
    rec = _observe({"coin": "T", "side": "long"}, patched)
    assert rec["would_block"] is False


def test_counter_regime_with_reversal_recorded_not_logged(patched):
    patched["macro"] = "down"
    analysis = {"coin": "T", "side": "long",
                "counter_reversal_confirmed": True}
    rec = _observe(analysis, patched)
    # it is counter-regime, but the explicit reversal escape means a real gate
    # would not fire — flagged on the record for later analysis.
    assert rec["would_block"] is False
    assert rec["reversal_confirmed"] is True


def test_unknown_side_not_would_block(patched):
    patched["macro"] = "down"
    rec = _observe({"coin": "T"}, patched)
    assert rec["would_block"] is False
