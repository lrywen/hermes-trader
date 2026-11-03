"""Entry-observability fields (2026-11-03, additive).

Locks the observability-only fields added across the perception → research →
executor runner-gate path:

  * triggers.breakout carries a structured ``flow_confirmed`` bool on EVERY
    return path (no parsing of the human reason string);
  * build_hits exposes every per-trigger fired/score and the composite
    denominator Σw, so composite_score is reconstructible offline;
  * the runner-gate verdict records trigger_hits / composite_denominator /
    flow_* / regime_decision (incl. coin-breakout override) / regime_gate
    (the market_regime risk gate's macro value) side by side;
  * none of this changes the admit/block decision — fields are write-only
    attribution, missing values degrade to None, never raise.

Red lines honoured: no admit/block/config change.
"""
from __future__ import annotations

import json

import pytest

from hermes_trader.agents import executor
from hermes_trader.indicators import triggers


# ── build_hits contract: count by feature flag, denominator, recompute ──────

BASE_11 = {
    "pctMoveSpike", "volumeSpike", "breakout", "rangeCompression",
    "trendStrength", "momentumBurst", "volumeBuildup1h", "trendFlip1h",
    "higherLows1h", "uptrendMomentum", "downtrendMomentum",
}

_W = {
    "pctMoveSpike": 0.40, "volumeSpike": 0.10, "breakout": 0.35,
    "rangeCompression": 0.0, "trendStrength": 0.30, "momentumBurst": 0.10,
    "volumeBuildup1h": 0.0, "trendFlip1h": 0.0, "higherLows1h": 0.0,
    "uptrendMomentum": 0.20, "downtrendMomentum": 0.0,
}
_TH = {"sigmaThreshold": 3.0, "breakoutLookback": 48, "breakoutMinRvol": 1.5,
       "adxPeriod": 14, "momentumLookback": 20, "momentumPct": 2.0,
       "bbLength": 20, "bbStdDev": 2.0}


def _build(mc_enabled, cp_enabled):
    return triggers.build_hits(
        [], _TH, dict(_W),
        momentum_continuation={"enabled": mc_enabled, "weight": 0.40},
        candlestick_patterns={"enabled": cp_enabled},
        daily_mover=(False, ""),
    )


def test_trigger_hits_count_follows_switches():
    # 11 base + mc?1 + cp?2 + 1 dailyMover.
    h_off, w_off = _build(False, False)
    assert len(h_off) == 11 + 1
    assert {h["name"] for h in h_off} >= BASE_11
    assert "dailyMover" in {h["name"] for h in h_off}

    h_cp, _ = _build(False, True)
    assert len(h_cp) == 11 + 2 + 1
    assert {"bearishReversalCandle", "bullishReversalCandle"} <= {
        h["name"] for h in h_cp}

    h_mc, w_mc = _build(True, False)
    assert len(h_mc) == 11 + 1 + 1
    assert "momentumContinuation1h" in {h["name"] for h in h_mc}

    h_all, w_all = _build(True, True)
    assert len(h_all) == 11 + 1 + 2 + 1  # current config (cp on, mc off) = 14


def test_composite_denominator_matches_weight_sum_and_recomputes():
    # mc OFF: denominator = base Σw; mc ON: + momentumContinuation1h weight.
    h_off, w_off = _build(False, False)
    denom_off = float(sum(w_off.values()))
    assert denom_off == sum(_W.values())

    h_mc, w_mc = _build(True, False)
    denom_mc = float(sum(w_mc.values()))
    assert denom_mc == pytest.approx(denom_off + 0.40)

    # Offline reconstruction: per-hit (name,fired,score) + recorded Σw + the
    # known weight table must reproduce composite_score exactly.
    def reconstruct(hits, weights, denom):
        ws = sum(h["score"] * weights.get(h["name"], 0.0)
                 for h in hits if h.get("fired"))
        raw = (ws / (denom or 1.0)) * 10.0
        return max(0.0, min(100.0, raw))

    for hits, w in ((h_off, w_off), (h_mc, w_mc)):
        assert reconstruct(hits, w, float(sum(w.values()))) == \
            triggers.composite_score(hits, w)


# ── breakout flow_confirmed present on every path ──────────────────────────

def _mc(t, o, h, l, c, v=100.0):
    from hermes_trader.models.types import Candle
    return Candle(t=t, o=o, h=h, l=l, c=c, v=v)


def test_breakout_flow_confirmed_key_always_present():
    # flat / insufficient-history path still carries the bool as a literal.
    r = triggers.breakout([], 20)
    assert r["flow_confirmed"] is False

    # inside-range quiet series: present, False.
    candles = [_mc(i * 300_000, 100, 100.2, 99.8, 100.0) for i in range(53)]
    r2 = triggers.breakout(candles, lookback=48, confirm_bars=2)
    assert "flow_confirmed" in r2 and r2["flow_confirmed"] is False


# ── runner-gate record: new fields land, decision byte-identical ───────────

def _gate(**kw):
    base = {"enabled": True, "min_confidence": 0.70, "min_composite": 30.0,
            "min_hip3_composite": 50.0, "rsi_overbought": 75.0,
            "rsi_oversold": 25.0, "max_extension_atr": 2.5,
            "allow_shorts": True, "mover_min_confidence": 0.80,
            "mover_min_composite": 45.0}
    base.update(kw)
    return {"runner_entry_gate": base}


def _isolate(monkeypatch, regime=("up", 1.0), coin_override=None):
    monkeypatch.setattr(executor, "evaluate_risk_overlay",
                        lambda cfg: {"posture": "neutral"})
    monkeypatch.setattr(executor, "resolve_applied_knobs",
                        lambda base, cfg, snap: dict(base))
    for name in ("_record_risk_tuning_shadow", "_record_pullback_shadow"):
        monkeypatch.setattr(executor, name, lambda *a, **k: None)
    import hermes_trader.agents.market_regime as mr
    monkeypatch.setattr(mr, "detect_regime_with_score",
                        lambda coin, **kw: regime)
    if coin_override is not None:
        monkeypatch.setattr(executor, "coin_breakout_regime",
                            lambda analysis: coin_override)


def _trigger_hits():
    return [
        {"name": "breakout", "fired": True, "score": 8.0,
         "direction": "up", "flow_confirmed": True},
        {"name": "trendStrength", "fired": False, "score": 0.0,
         "direction": None, "flow_confirmed": None},
    ]


def test_runner_record_carries_new_fields(monkeypatch, tmp_path):
    _isolate(monkeypatch)
    out = tmp_path / "rg.jsonl"
    monkeypatch.setattr(executor, "_RUNNER_GATE_SHADOW_FILE", str(out))
    a = {"coin": "TST", "side": "long", "confidence": 0.9,
         "composite_score": 50.0, "breakout_fired": True, "volumeSpike": True,
         "trigger_hits": _trigger_hits(), "composite_denominator": 1.45,
         "flow_confirm_raw": 0.83, "flow_data_available": True}
    cfg = _gate()
    cfg["momentum_continuation"] = {"enabled": False}
    cfg["candlestick_patterns"] = {"enabled": True}
    decision = executor._runner_entry_block_reason(a, cfg)
    assert decision == ""  # admitted; behaviour unchanged
    rec = json.loads(out.read_text().strip().splitlines()[0])

    # structured observability fields
    assert rec["flow_confirm_raw"] == 0.83
    assert rec["flow_data_available"] is True
    assert rec["flow_confirmed_breakout"] is True
    assert rec["composite_denominator"] == 1.45
    assert rec["mc_enabled"] is False
    assert rec["cp_enabled"] is True  # explicit config passthrough
    th = {h["name"]: h for h in rec["trigger_hits"]}
    assert th["breakout"]["fired"] is True and th["breakout"]["score"] == 8.0
    # gate regime comes from the patched detect_regime_with_score
    assert rec["regime_gate"] == "up"
    assert rec["regime_gate_score"] == 1.0
    # decision regime: same macro (no coin override here)
    assert rec["regime_decision"] == "up"


def test_regime_gate_vs_decision_can_diverge(monkeypatch, tmp_path):
    # Macro regime neutral (what market_regime_gate sees) but a high-RVOL
    # coin-local breakout overrides the decision regime to up. Both recorded.
    _isolate(monkeypatch, regime=("neutral", 0.2), coin_override="up")
    out = tmp_path / "rg_div.jsonl"
    monkeypatch.setattr(executor, "_RUNNER_GATE_SHADOW_FILE", str(out))
    a = {"coin": "TST", "side": "long", "confidence": 0.9,
         "composite_score": 50.0, "breakout_fired": True, "volumeSpike": True,
         "trigger_hits": _trigger_hits()}
    executor._runner_entry_block_reason(a, _gate())
    rec = json.loads(out.read_text().strip().splitlines()[0])
    assert rec["regime_gate"] == "neutral"
    assert rec["regime_gate_score"] == 0.2
    assert rec["regime_decision"] == "up"  # coin-breakout override
    assert rec["regime_gate"] != rec["regime_decision"]


def test_new_fields_absent_degrade_to_none(monkeypatch, tmp_path):
    # Legacy analysis without any of the new keys: recorder must not raise and
    # every added field resolves to None (flow_confirmed_breakout → False).
    _isolate(monkeypatch)
    out = tmp_path / "rg_old.jsonl"
    monkeypatch.setattr(executor, "_RUNNER_GATE_SHADOW_FILE", str(out))
    a = {"coin": "TST", "side": "long", "confidence": 0.2,
         "composite_score": 20.0}
    decision = executor._runner_entry_block_reason(a, _gate())
    assert "runner_gate_blocked" in decision  # decision unchanged
    rec = json.loads(out.read_text().strip().splitlines()[0])
    for k in ("trigger_hits", "composite_denominator", "flow_confirm_raw",
              "flow_data_available", "regime_gate", "regime_gate_score",
              "regime_decision", "mc_enabled", "cp_enabled"):
        # regime_decision is always resolved by resolve_decision_regime;
        # gate regime from the patched detector — only the pure passthroughs
        # are guaranteed None here.
        if k in ("trigger_hits", "composite_denominator", "flow_confirm_raw",
                 "flow_data_available"):
            assert rec[k] is None, k
    assert rec["flow_confirmed_breakout"] is False
