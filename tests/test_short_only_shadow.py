"""Offline tests for the short-only shadow (Audit 2026-09-10).

When runner_entry_gate.allow_shorts=false every short is blocked at the gate.
The short-only shadow records those suppressed candidates (conviction,
composite, structure, price, macro + own-1h regime) so the counter-factual EV
of re-enabling shorts can be measured. It must NEVER change the live block.

Pins:
  * helper writes a well-formed record to the configured path (offline, regime
    detectors stubbed);
  * a short candidate is still blocked with shadow ON as with it OFF (the
    returned reason is identical);
  * shadow OFF / a long candidate / a disabled gate writes nothing.
"""
from __future__ import annotations

import json

from hermes_trader.agents import executor, market_regime


def _analysis(**over):
    a = {
        "coin": "AAA", "side": "short", "confidence": 0.71,
        "ai_confidence_raw": 0.71, "composite_score": 48.0,
        "mid": 100.0, "rsi4h": 30.0, "adx4h": 30.0, "atr4h": 1.5,
        "volume_spike_fired": False, "breakout_fired": False,
        "momentum_burst_fired": True, "downtrend_momentum_fired": True,
        "slow_burn_count": 1, "whale_signal": False, "id": "t1",
    }
    a.update(over)
    return a


def _config(path, *, enabled=True, allow_shorts=False):
    return {
        "runner_entry_gate": {
            "enabled": enabled,
            "allow_shorts": allow_shorts,
            "min_confidence": 0.62,
            "min_composite": 45,
            "min_short_confidence": 0.68,
            "min_short_composite": 40,
            "short_only_shadow": {"shadow_mode": True,
                                  "shadow_log_path": str(path)},
        },
        # overlay disabled -> base allow_shorts stands, no posture flip
        "regime_risk_overlay": {"enabled": False, "shadow_mode": False},
    }


def _stub_regimes(monkeypatch, macro=("down", 0.7), own=("down", 0.66)):
    monkeypatch.setattr(market_regime, "detect_regime_with_score",
                        lambda coin, **kw: macro)
    monkeypatch.setattr(market_regime, "detect_own_regime_with_score",
                        lambda coin, **kw: own)


# ── helper ─────────────────────────────────────────────────────────────────

def test_helper_writes_record(tmp_path, monkeypatch):
    _stub_regimes(monkeypatch)
    path = tmp_path / "short.jsonl"
    gate = _config(path)["runner_entry_gate"]
    executor._record_short_only_shadow(_analysis(), gate,
                                       reason="shorts_disabled")
    rows = [json.loads(l) for l in path.read_text().splitlines()]
    assert len(rows) == 1
    r = rows[0]
    assert r["rule"] == "short_only" and r["side"] == "short"
    assert r["would"] == "admit_if_shorts_enabled"
    d = r["detail"]
    assert d["confidence"] == 0.71 and d["composite_score"] == 48.0
    assert d["entry_px"] == 100.0 and d["downtrend"] is True
    assert d["macro_regime"] == "down" and d["own_1h_regime"] == "down"
    assert d["min_short_confidence"] == 0.68


def test_helper_never_raises_on_regime_error(tmp_path, monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("net")
    monkeypatch.setattr(market_regime, "detect_regime_with_score", _boom)
    path = tmp_path / "short.jsonl"
    gate = _config(path)["runner_entry_gate"]
    executor._record_short_only_shadow(_analysis(), gate,
                                       reason="shorts_disabled")
    rows = [json.loads(l) for l in path.read_text().splitlines()]
    assert len(rows) == 1          # record still written, regime fields None
    assert rows[0]["detail"]["macro_regime"] is None


# ── gate integration: shadow never changes the live block ─────────────────

def test_short_blocked_identical_shadow_on_vs_off(tmp_path, monkeypatch):
    _stub_regimes(monkeypatch)
    cfg_on = _config(tmp_path / "on.jsonl")
    cfg_off = _config(tmp_path / "off.jsonl")
    cfg_off["runner_entry_gate"]["short_only_shadow"]["shadow_mode"] = False

    reason_on = executor._runner_entry_block_reason(_analysis(), cfg_on)
    reason_off = executor._runner_entry_block_reason(_analysis(), cfg_off)
    assert reason_on == reason_off == "runner_gate_blocked (shorts disabled)"
    assert (tmp_path / "on.jsonl").exists()
    assert not (tmp_path / "off.jsonl").exists()


def test_long_candidate_writes_nothing(tmp_path, monkeypatch):
    _stub_regimes(monkeypatch)
    cfg = _config(tmp_path / "x.jsonl")
    reason = executor._runner_entry_block_reason(
        _analysis(side="long", confidence=0.8, composite_score=55,
                  downtrend_momentum_fired=False), cfg)
    # a strong fresh long is admitted (empty reason) and no short shadow row
    assert reason == ""
    assert not (tmp_path / "x.jsonl").exists()


def test_disabled_gate_writes_nothing(tmp_path, monkeypatch):
    _stub_regimes(monkeypatch)
    cfg = _config(tmp_path / "x.jsonl", enabled=False)
    assert executor._runner_entry_block_reason(_analysis(), cfg) == ""
    assert not (tmp_path / "x.jsonl").exists()
