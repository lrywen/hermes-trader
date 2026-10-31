"""Audit 2026-09-12 — majors-missed-surge gray-release arms.

Pins the three observation-first (shadow→enforce) changes:

* #4 sigma_burst_gate (perception.py): a large σ return/volume spike that
  scores in [gate_override, gate) is recorded as a would-surface in shadow
  mode and actually surfaced in enforce mode; coins below gate_override are
  never surfaced.
* #8 research_cooldown_adaptive (trading_loop._coin_is_hot): pure hot-state
  classifier driving the shortened re-research window.
* #7 breakout_exemption (ta_filter._high_quality_breakout): high-quality
  confirmed breakout (fired + strong RVOL) classifier that downgrades the
  late-entry prefilter REJECT.

Also pins the structured `z` carried by pct_move_spike/volume_spike (it must
not change score/fired/reason, which existing composite scoring relies on).
"""

from __future__ import annotations

from hermes_trader.agents.cooldown_adaptive import coin_is_hot
from hermes_trader.indicators.triggers import pct_move_spike, volume_spike
from hermes_trader.models.types import Candle


def _c(close: float, vol: float, t: int = 0) -> Candle:
    return Candle(t=t, T=300_000, o=close, h=close, l=close, c=close, v=vol)


# ────────────────────────────────────────────────────────────────────────────
# structured z on the two spike triggers
# ────────────────────────────────────────────────────────────────────────────────────
def test_pct_move_spike_carries_structured_z_without_changing_score():
    # 80 bars with small noisy returns (non-zero trailing std), then a big up bar.
    import math
    closes = [100.0]
    for i in range(80):
        closes.append(closes[-1] * (1.0 + 0.001 * math.sin(i)))
    closes.append(closes[-1] * 1.08)  # +8% single-bar spike
    candles = [_c(p, 1.0, t=i * 300_000) for i, p in enumerate(closes)]
    hit = pct_move_spike(candles)
    assert "z" in hit and isinstance(hit["z"], float) and hit["z"] > 3.0
    assert hit["direction"] == "up"
    # fired/score/reason semantics unchanged.
    assert hit["fired"] is True and hit["score"] > 0 and "σ" in hit["reason"]


def test_volume_spike_carries_structured_z_without_changing_score():
    # volumes with mild variance, then one large-volume bar.
    vols = [1.0 + (0.1 if i % 2 else 0.0) for i in range(21)]
    vols.append(12.0)
    candles = [_c(100.0, v, t=i * 300_000) for i, v in enumerate(vols)]
    hit = volume_spike(candles)
    assert "z" in hit and hit["z"] > 3.0
    assert hit["fired"] is True and hit["score"] > 0


def test_spike_flat_inputs_have_zero_z():
    assert pct_move_spike([])["z"] == 0.0
    assert volume_spike([_c(1.0, 1.0)])["z"] == 0.0


# ────────────────────────────────────────────────────────────────────────────
# #8 research_cooldown_adaptive — pure hot classifier
# ────────────────────────────────────────────────────────────────────────────
def test_coin_is_hot_requires_enabled():
    perc = {"triggers": [{"name": "pctMoveSpike", "z": 9.0, "fired": True}]}
    hot, _ = coin_is_hot(perc, {"enabled": False, "pct_sigma_min": 3.0})
    assert hot is False


def test_coin_is_hot_on_strong_pct_sigma():
    blk = {"enabled": True, "pct_sigma_min": 3.0, "vol_sigma_min": 5.0}
    perc = {"triggers": [{"name": "pctMoveSpike", "z": 11.2}]}
    hot, dbg = coin_is_hot(perc, blk)
    assert hot is True and dbg["pct_z"] == 11.2


def test_coin_is_hot_on_momentum_burst_without_sigma():
    blk = {"enabled": True, "pct_sigma_min": 3.0, "vol_sigma_min": 5.0}
    perc = {"triggers": [{"name": "momentumBurst", "fired": True}]}
    hot, _ = coin_is_hot(perc, blk)
    assert hot is True


def test_coin_is_hot_false_for_calm_coin():
    blk = {"enabled": True, "pct_sigma_min": 3.0, "vol_sigma_min": 5.0}
    perc = {"triggers": [{"name": "pctMoveSpike", "z": 1.1},
                         {"name": "volumeSpike", "z": 2.0}]}
    hot, _ = coin_is_hot(perc, blk)
    assert hot is False


def test_coin_is_hot_false_on_garbled_input():
    hot, _ = coin_is_hot({"triggers": "garbage"}, {"enabled": True})
    assert hot is False
    hot, _ = coin_is_hot(None, {"enabled": True})  # type: ignore[arg-type]
    assert hot is False


# ────────────────────────────────────────────────────────────────────────────
# #7 breakout_exemption — pure high-quality-breakout classifier
# ────────────────────────────────────────────────────────────────────────────
def test_high_quality_breakout_from_reason_rvol():
    from hermes_trader.agents.ta_filter import _high_quality_breakout

    perc = {"triggers": [{"name": "breakout", "fired": True,
                          "reason": "breakout held 3 bars, +4.21%, RVOL 4.18x, +2.8 ATR"}]}
    ok, info = _high_quality_breakout(perc, {"enabled": True, "require_rvol": 4.0})
    assert ok is True and info["rvol"] == 4.18


def test_high_quality_breakout_structured_rvol_field():
    from hermes_trader.agents.ta_filter import _high_quality_breakout

    perc = {"triggers": [{"name": "breakout", "fired": True, "rvol": 5.5}]}
    ok, info = _high_quality_breakout(perc, {"enabled": True, "require_rvol": 4.0})
    assert ok is True and info["rvol"] == 5.5


def test_high_quality_breakout_rejects_weak_rvol_and_unfired():
    from hermes_trader.agents.ta_filter import _high_quality_breakout

    blk = {"enabled": True, "require_rvol": 4.0}
    # fired but RVOL under threshold
    perc = {"triggers": [{"name": "breakout", "fired": True,
                          "reason": "breakout held 2 bars, RVOL 2.10x"}]}
    ok, _ = _high_quality_breakout(perc, blk)
    assert ok is False
    # high RVOL but not fired
    perc2 = {"triggers": [{"name": "breakout", "fired": False, "rvol": 9.0}]}
    ok2, _ = _high_quality_breakout(perc2, blk)
    assert ok2 is False
    # disabled
    ok3, _ = _high_quality_breakout(perc, {"enabled": False})
    assert ok3 is False


# ────────────────────────────────────────────────────────────────────────────
# #4 sigma_burst_gate — pure decision + full _scan_single_market wiring
# ────────────────────────────────────────────────────────────────────────────
_BLK = {"enabled": True, "shadow_mode": True,
        "pct_sigma_min": 3.0, "vol_sigma_min": 5.0, "gate_override": 45.0}


def _hits(pct_z=0.0, vol_z=0.0, pct_fired=False, vol_fired=False):
    return [
        {"name": "pctMoveSpike", "z": pct_z, "fired": pct_fired, "score": 10},
        {"name": "volumeSpike", "z": vol_z, "fired": vol_fired, "score": 7},
    ]


def test_sigma_decision_qualifies_on_strong_pct_sigma_in_band():
    from hermes_trader.agents.perception import _sigma_burst_decision
    # 11.2σ, composite 49.57 → in [45,54) (the ETH segment-1 shape).
    ok, info = _sigma_burst_decision(49.57, 54.0, _hits(pct_z=11.2), _BLK)
    assert ok is True
    assert info["score"] == 49.57 and info["eff_gate"] == 45.0
    assert info["pct_z"] == 11.2 and info["gate"] == 54.0


def test_sigma_decision_qualifies_on_strong_vol_sigma_in_band():
    from hermes_trader.agents.perception import _sigma_burst_decision
    ok, info = _sigma_burst_decision(46.0, 54.0, _hits(vol_z=6.7), _BLK)
    assert ok is True and info["vol_z"] == 6.7


def test_sigma_decision_rejects_below_override_and_weak_sigma():
    from hermes_trader.agents.perception import _sigma_burst_decision
    # strong σ but composite under the 45 override → never surface.
    assert _sigma_burst_decision(40.0, 54.0, _hits(pct_z=11.2), _BLK)[0] is False
    # in band but σ too weak.
    assert _sigma_burst_decision(50.0, 54.0, _hits(pct_z=2.0, vol_z=2.0), _BLK)[0] is False
    # already above gate → bypass not needed.
    assert _sigma_burst_decision(60.1, 54.0, _hits(pct_z=11.2), _BLK)[0] is False


def test_sigma_decision_disabled_and_garbled_fail_open():
    from hermes_trader.agents.perception import _sigma_burst_decision
    blk = dict(_BLK, enabled=False)
    assert _sigma_burst_decision(49.0, 54.0, _hits(pct_z=11.2), blk)[0] is False
    assert _sigma_burst_decision(49.0, 54.0, "garbage", _BLK)[0] is False


def _base_config() -> dict:
    from hermes_trader.agents.config import trigger_thresholds_params, trigger_weights_params
    return {
        "scan": {"candleInterval": "5m", "candleCount": 100,
                 "cacheTtlMs": 50_000, "cacheTtlMs1h": 600_000,
                 "evaluateClosedBarsOnly": False},
        "weights": trigger_weights_params(config={}),
        "thresholds": trigger_thresholds_params(config={}),
        "trend_surfacing": {"enabled": True, "min_adx": 1000},  # effectively off
        "regime_filter": {"enabled": False},
        "sigma_burst_gate": dict(_BLK),
    }


def _quiet_candles():
    """80 flat 5m bars (composite ~0, no trend bypass)."""
    return [_c(100.0, 1.0, t=i * 300_000) for i in range(81)]


def _wire_scan(monkeypatch, candles):
    from hermes_trader.agents import perception

    def _fake_fetch(coin, interval, count, *a, **k):
        return list(candles)

    monkeypatch.setattr(perception, "fetch_hl_candles", _fake_fetch)


def test_sigma_burst_shadow_does_not_surface(monkeypatch):
    from hermes_trader.agents import perception

    _wire_scan(monkeypatch, _quiet_candles())
    # Force the gate-band qualification; in shadow the coin must still be
    # dropped (2026-09-21 cleanup: the observation-only recorder is gone).
    monkeypatch.setattr(perception, "_sigma_burst_decision",
                        lambda score, gate, hits, blk: (True, {"score": 49.0,
                            "gate": 54.0, "eff_gate": 45.0, "pct_z": 11.2,
                            "vol_z": 0.0, "fired_triggers": ["pctMoveSpike"]}))

    cfg = _base_config()  # shadow_mode True
    ok, out = perception._scan_single_market(
        {"coin": "ETH", "sz_decimals": 2, "type": "perp"}, 100.0, cfg, min_score=54.0)
    assert ok is True and out is None  # shadow → still dropped


def test_sigma_burst_enforce_surfaces(monkeypatch):
    from hermes_trader.agents import perception

    _wire_scan(monkeypatch, _quiet_candles())
    monkeypatch.setattr(perception, "_sigma_burst_decision",
                        lambda score, gate, hits, blk: (True, {"score": 49.0,
                            "gate": 54.0, "eff_gate": 45.0, "pct_z": 11.2,
                            "vol_z": 0.0, "fired_triggers": ["pctMoveSpike"]}))

    cfg = _base_config()
    cfg["sigma_burst_gate"]["shadow_mode"] = False
    ok, out = perception._scan_single_market(
        {"coin": "ETH", "sz_decimals": 2, "type": "perp"}, 100.0, cfg, min_score=54.0)
    assert ok is True
    assert isinstance(out, dict) and out["coin"] == "ETH"


def test_sigma_burst_disabled_leaves_original_gate(monkeypatch):
    from hermes_trader.agents import perception

    _wire_scan(monkeypatch, _quiet_candles())
    cfg = _base_config()
    cfg["sigma_burst_gate"]["enabled"] = False
    ok, out = perception._scan_single_market(
        {"coin": "ETH", "sz_decimals": 2, "type": "perp"}, 100.0, cfg, min_score=54.0)
    assert ok is True and out is None  # flat candles + arm disabled → dropped
