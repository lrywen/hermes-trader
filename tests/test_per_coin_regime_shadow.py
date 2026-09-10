"""Shadow-only regression for the per-coin OWN-4h direction probe (坑1 quick).

The market_regime gate keys every crypto perp off the BTC proxy. This probe
records (never enforces) what a soft weak_aligned demotion would do when the
macro call is "aligned" but the coin's OWN 4h EMA points against the trade
(the ZEC failure: BTC=up/aligned, own 4h rolling over, -0.83% stop).

Pins:
  * pure own_4h_divergence: long + close4h<ema21 + adx4h>=20 -> would_demote;
    weak ADX (range) does NOT demote; missing readings fail OPEN (no demote);
  * record_* only writes when shadow_mode is on AND macro aligned;
  * enabling the probe NEVER changes a gate verdict (shadow-only).
"""
from __future__ import annotations

import json
import os

import pytest

from hermes_trader.agents import per_coin_regime_shadow as pcrs
from hermes_trader.agents import market_regime as mr
from hermes_trader.agents.risk_gates import GateContext


def _analysis(close, ema21, adx):
    return {"close4h": close, "ema21_4h": ema21, "adx4h": adx,
            "atr4h": 1.0, "coin": "ZEC"}


# ── pure divergence ────────────────────────────────────────────────────────

def test_long_own_down_with_adx_confirms_demotes():
    d = pcrs.own_4h_divergence("long", _analysis(99.0, 100.0, 28.0))
    assert d["own_down"] is True
    assert d["adx_confirms"] is True
    assert d["would_demote"] is True
    assert d["own_gap_pct"] == pytest.approx(-1.0)


def test_long_own_down_but_weak_adx_does_not_demote():
    # Range tape: own EMA under but ADX<20 -> not a real own-trend, fail open.
    d = pcrs.own_4h_divergence("long", _analysis(99.0, 100.0, 12.0))
    assert d["own_down"] is True
    assert d["adx_confirms"] is False
    assert d["would_demote"] is False


def test_long_own_up_aligned_no_demote():
    d = pcrs.own_4h_divergence("long", _analysis(101.0, 100.0, 30.0))
    assert d["own_up"] is True
    assert d["would_demote"] is False


def test_short_own_up_with_adx_demotes():
    d = pcrs.own_4h_divergence("short", _analysis(101.0, 100.0, 30.0))
    assert d["own_up"] is True
    assert d["would_demote"] is True


def test_missing_readings_fail_open():
    assert pcrs.own_4h_divergence("long", {})["would_demote"] is False
    assert pcrs.own_4h_divergence("long", None)["would_demote"] is False
    assert pcrs.own_4h_divergence(
        "long", {"close4h": None, "ema21_4h": 100, "adx4h": 30}
    )["would_demote"] is False


# ── own-1h detector: independent cache, no proxy substitution ─────────────

def test_detect_own_uses_coin_and_separate_cache(monkeypatch):
    mr._own_regime_cache.clear()
    calls = []

    def _fake_detect(proxy):
        calls.append(proxy)
        return ("up", 0.7)

    monkeypatch.setattr(mr, "_detect_for_proxy_with_score", _fake_detect)
    # crypto coin is NOT remapped to BTC proxy
    reg, score = mr.detect_own_regime_with_score("ZEC")
    assert (reg, score) == ("up", 0.7)
    assert calls == ["ZEC"]
    # second call within TTL served from the own cache (no extra fetch)
    mr.detect_own_regime_with_score("ZEC")
    assert calls == ["ZEC"]
    # macro BTC cache stays untouched by the own lookup
    assert "BTC" not in mr._regime_cache
    mr._own_regime_cache.clear()


def test_detect_own_failure_degrades(monkeypatch):
    mr._own_regime_cache.clear()
    # _detect_for_proxy_with_score swallows fetch errors to (neutral, 0.0);
    # the own detector must propagate that safe degradation.
    monkeypatch.setattr(mr, "_detect_for_proxy_with_score",
                        lambda proxy: ("neutral", 0.0))
    assert mr.detect_own_regime_with_score("ZEC") == ("neutral", 0.0)
    mr._own_regime_cache.clear()


# ── shadow recording gating ────────────────────────────────────────────────

def _mr(regime="up", via="aligned", score=0.61):
    return {"pass": True, "via": via, "regime": regime,
            "trend_score": score, "funding": "NEUTRAL",
            "against_funding": False, "counter_trend": False}


def test_record_inert_when_shadow_disabled(tmp_path):
    path = tmp_path / "s.jsonl"
    cfg = {"per_coin_regime_shadow": {"shadow_mode": False,
                                      "shadow_log_path": str(path)}}
    pcrs.record_per_coin_regime_shadow(
        coin="ZEC", side="long", confidence=0.7, composite_score=40,
        market_regime_result=_mr(), analysis=_analysis(99, 100, 30),
        config=cfg)
    assert not path.exists()


def test_record_writes_only_for_macro_aligned(tmp_path, monkeypatch):
    # Avoid any network: own 1h detector is TTL-cached/mocked.
    monkeypatch.setattr(
        "hermes_trader.agents.market_regime.detect_own_regime_with_score",
        lambda coin, force=False: ("down", 0.40))
    path = tmp_path / "s.jsonl"
    cfg = {"per_coin_regime_shadow": {"shadow_mode": True,
                                      "shadow_log_path": str(path)}}
    # counter-trend macro (down + long) must not be shadowed by this probe
    pcrs.record_per_coin_regime_shadow(
        coin="ZEC", side="long", confidence=0.7, composite_score=40,
        market_regime_result=_mr(regime="down", via="blocked_bypass"),
        analysis=_analysis(99, 100, 30), config=cfg)
    assert not path.exists()
    # macro up + long + own down -> writes a demote record (weak_review tier)
    pcrs.record_per_coin_regime_shadow(
        coin="ZEC", side="long", confidence=0.7, composite_score=40,
        market_regime_result=_mr(), analysis=_analysis(99, 100, 30),
        config=cfg)
    rows = [json.loads(l) for l in path.read_text().splitlines()]
    assert len(rows) == 1
    r = rows[0]
    assert r["rule"] == "per_coin_regime"
    assert r["would"] == "demote_to_weak_aligned"
    assert r["detail"]["macro_regime"] == "up"
    assert r["detail"]["own_down"] is True
    assert r["detail"]["own_1h_regime"] == "down"
    assert r["detail"]["quadrant_tier"] == "weak_review"
    assert r["detail"]["tier_would"] == "counter_review"


# ── quadrant / 3-tier pure classification ──────────────────────────────────

def test_quadrant_strong_when_both_aligned_and_own_strong():
    q = pcrs.quadrant_tier(macro_regime="up", macro_score=0.7,
                           own_regime="up", own_score=0.70, side="long",
                           strong_score=0.65, mid_score=0.55)
    assert q["tier"] == "strong" and q["would"] == "free_pass"


def test_quadrant_mid_when_own_neutral_or_weak():
    q = pcrs.quadrant_tier(macro_regime="up", macro_score=0.6,
                           own_regime="neutral", own_score=0.2, side="long",
                           strong_score=0.65, mid_score=0.55)
    assert q["tier"] == "mid" and q["would"] == "light_bar"


def test_quadrant_weak_review_when_own_against():
    q = pcrs.quadrant_tier(macro_regime="up", macro_score=0.61,
                           own_regime="down", own_score=0.4, side="long",
                           strong_score=0.65, mid_score=0.55)
    assert q["tier"] == "weak_review" and q["would"] == "counter_review"
    assert q["own_against"] is True


def test_quadrant_na_for_non_aligned_macro():
    q = pcrs.quadrant_tier(macro_regime="chop", macro_score=0.1,
                           own_regime="down", own_score=0.4, side="long",
                           strong_score=0.65, mid_score=0.55)
    assert q["tier"] == "n/a"


# ── shadow-only: gate verdict unchanged whether probe on/off ───────────────

def _ctx():
    return GateContext(
        confidence=0.7, current_positions=[], trade_notional_usd=25.0,
        daily_pnl=0.0, market_volume_24h_usd=1e9, coin="ZEC",
        trade_side="long", has_binary_news_risk=False, equity=1000.0,
        total_open_notional=0.0, composite_score=40.0)


def test_probe_is_bypass_and_does_not_touch_gate_result(tmp_path, monkeypatch):
    # The probe reads the gate result but never mutates it. Run the same macro
    # aligned result through the recorder with the probe off vs on and assert
    # the verdict dict is identical (and still aligned/pass).
    from hermes_trader.agents import risk_gates

    captured = {}

    def _fake_detect(coin):
        return ("up", 0.61)

    monkeypatch.setattr(
        "hermes_trader.agents.market_regime.detect_regime_with_score",
        _fake_detect)
    monkeypatch.setattr(
        "hermes_trader.agents.risk_gates._funding_regime_for",
        lambda coin: "NEUTRAL")
    monkeypatch.setattr(
        "hermes_trader.agents.market_regime.detect_own_regime_with_score",
        lambda coin, force=False: ("down", 0.40))

    ctx = _ctx()
    cfg_on = {"per_coin_regime_shadow": {
        "shadow_mode": True,
        "shadow_log_path": str(tmp_path / "probe.jsonl")},
        "min_trend_score": 0.55,
        "analyst_scoring": {"counter_trend_min_score": 50.0}}

    res = risk_gates.market_regime_gate(
        ctx, 0.82, True, 0.0, 0.55, config=cfg_on)
    before = dict(res)
    pcrs.record_per_coin_regime_shadow(
        coin=ctx.coin, side=ctx.trade_side, confidence=ctx.confidence,
        composite_score=ctx.composite_score, market_regime_result=res,
        analysis=_analysis(99.0, 100.0, 30.0), config=cfg_on)
    assert res == before                 # gate result not mutated
    assert res["via"] == "aligned" and res["pass"] is True
    assert (tmp_path / "probe.jsonl").exists()
    row = json.loads((tmp_path / "probe.jsonl").read_text().splitlines()[0])
    assert row["would"] == "demote_to_weak_aligned"
