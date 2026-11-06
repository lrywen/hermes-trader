"""Tests for the single-source decision regime and its OBSERVATION-ONLY probe.

The hard direction gate (LONG-in-DOWN / SHORT-in-UP) was reverted to a
counterfactual shadow probe after a 106-trade replay showed it blocked a
net-positive half. These tests cover:
  * resolve_decision_regime resolves once, is cached, applies the coin breakout
    override;
  * regime_direction_observe sets would_block correctly but NEVER blocks;
  * the block shim regime_direction_block always returns "" (allows entry).

Macro detection and the coin breakout override are stubbed at their source
modules; shadow writes are neutralized so there are no /data side effects.
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


def test_coin_breakout_writes_joinable_unified_audit(patched, monkeypatch):
    import hermes_trader.shadow_log as sl

    rows = []
    monkeypatch.setattr(sl, "append_jsonl",
                        lambda path, rec, stream="shadow": rows.append((stream, rec)))
    patched["macro"] = "neutral"
    patched["coin"] = "up"
    analysis = {"id": "a-brk", "coin": "T", "side": "long",
                "composite_score": 55.0, "breakout_fired": True}
    assert executor.resolve_decision_regime(analysis, {}) == "up"
    assert len(rows) == 1
    stream, row = rows[0]
    assert stream == row["stream"] == "regime_bypass"
    assert row["override"] == "coin_breakout"
    assert row["analysis_id"] == "a-brk"
    assert row["trace_id"] == "a-brk"
    assert row["macro_regime"] == "neutral"
    assert row["coin_regime"] == "up"


def test_scan_probe_can_suppress_duplicate_coin_breakout_audit(patched, monkeypatch):
    import hermes_trader.shadow_log as sl

    monkeypatch.setattr(
        sl, "append_jsonl",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("duplicate audit")))
    patched["macro"] = "neutral"
    patched["coin"] = "down"
    analysis = {"coin": "T", "side": "short"}
    assert executor.resolve_decision_regime(
        analysis, {}, record_bypass=False) == "down"


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


def test_long_in_down_would_block_but_entry_allowed(patched):
    patched["macro"] = "down"
    analysis = {"coin": "T", "side": "long"}
    rec = _observe(analysis, patched)
    assert rec["would_block"] is True
    # observation only: the block shim still admits the entry
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


def test_counter_regime_with_reversal_not_would_block(patched):
    patched["macro"] = "down"
    analysis = {"coin": "T", "side": "long",
                "counter_reversal_confirmed": True}
    rec = _observe(analysis, patched)
    assert rec["would_block"] is False
    assert rec["reversal_confirmed"] is True


def test_unknown_side_not_would_block(patched):
    patched["macro"] = "down"
    rec = _observe({"coin": "T"}, patched)
    assert rec["would_block"] is False


def _capture_c4(monkeypatch):
    rows = []
    monkeypatch.setattr(executor, "_record_risk_tuning_shadow",
                        lambda **kwargs: rows.append(kwargs))
    return rows


def _plunge_analysis(pct=3.0):
    return {
        "id": "c4-test",
        "coin": "T",
        "side": "long",
        "recent_4h_plunge_data_ok": True,
        "recent_4h_plunge_pct": pct,
        "recent_4h_plunge_bar_ts": 123000,
        "recent_4h_plunge_body_pct": pct,
        "recent_4h_plunge_close_pct": pct,
    }


def test_c4_plunge_neutralizes_up_without_mutating_live_regime(patched, monkeypatch):
    rows = _capture_c4(monkeypatch)
    patched["macro"] = "up"
    patched["coin"] = "up"
    analysis = _plunge_analysis()
    assert _resolve(analysis, patched) == "up"
    monkeypatch.setattr(executor, "coin_breakout_regime",
                        lambda _: (_ for _ in ()).throw(AssertionError("recalculated")))

    executor._record_c4_regime_shadow(analysis, "up", "up", 75.0)

    assert analysis["_decision_regime"] == "up"
    detail = rows[0]["detail"]
    assert detail["arm"] == "c4_post_plunge"
    assert detail["enforced"] is False
    assert detail["regime_candidate"] == "neutral"
    assert detail["regime_would_change"] is True
    assert detail["coin_override_would_be_disabled"] is True
    assert detail["rsi_ob_eff"] == 70.0
    assert detail["would_block"] is False


def test_c4_plunge_preserves_down_candidate_and_counterfactual_block(patched, monkeypatch):
    rows = _capture_c4(monkeypatch)
    analysis = _plunge_analysis(2.84)
    executor._record_c4_regime_shadow(analysis, "down", "down", 80.0)
    detail = rows[0]["detail"]
    assert detail["regime_candidate"] == "down"
    assert detail["would_block"] is True
    assert detail["rsi_ob_eff"] == 75.0


def test_c4_non_plunge_keeps_regime_override_and_rsi(patched, monkeypatch):
    rows = _capture_c4(monkeypatch)
    analysis = _plunge_analysis(2.83)
    analysis["_decision_coin_regime"] = "up"
    executor._record_c4_regime_shadow(analysis, "up", "neutral", 75.0)
    detail = rows[0]["detail"]
    assert detail["regime_candidate"] == "up"
    assert detail["regime_would_change"] is False
    assert detail["coin_override_would_be_disabled"] is False
    assert detail["rsi_ob_eff"] == 75.0


def test_c4_skips_missing_or_short_snapshots(patched, monkeypatch):
    rows = _capture_c4(monkeypatch)
    executor._record_c4_regime_shadow({"coin": "T", "side": "long"}, "up", "up", 75.0)
    analysis = _plunge_analysis()
    analysis["side"] = "short"
    executor._record_c4_regime_shadow(analysis, "up", "up", 75.0)
    assert rows == []
