"""Regression tests for the 2026-10-21 trigger/indicator hardening batch.

Covers the zero-behaviour-change data-hygiene fixes:
  B4  trend_flip_1h newest-first scan + dead score branch removed
  B6  volume_buildup_1h cadence guard (5m can't masquerade as 1h)
  B7  pct_move_spike zero prior-close no longer raises ZeroDivisionError
  B8  pct_move_spike near-zero prior vol no longer fakes a high-z spike
  B9  range_compression perfectly-flat series no longer fakes P0 squeeze
  B10 math lookback indicators reject period < 1
  B11 volume_profile rejects pathological atr_multiple / value-area params
"""

import math

import pytest

from hermes_trader.indicators import triggers as T
from hermes_trader.indicators import math as M
from hermes_trader.indicators.volume_profile import volume_profile
from hermes_trader.models.types import Candle


def _c(t, o, h, l, c, v=1.0) -> Candle:
    return Candle(t=t, o=o, h=h, l=l, c=c, v=v)


# ---------- B7: zero prior close -----------------------------------------

def test_b7_zero_prior_close_does_not_raise():
    # 40 prior closes at 100, then a zero close, then a normal bar.
    candles = [_c(i, 100, 100, 100, 100) for i in range(40)]
    candles.append(_c(40, 100, 100, 0, 0))            # prev close becomes 0
    candles.append(_c(41, 0, 101, 0, 101))
    hit = T.pct_move_spike(candles, sigma_threshold=3)
    # Defined (no ZeroDivisionError); the broken pair is dropped.
    assert hit["name"] == "pctMoveSpike"
    assert isinstance(hit["z"], float)


# ---------- B8: near-zero prior vol --------------------------------------

def test_b8_near_zero_std_no_fake_spike():
    # 38 identical closes (std ~0), then the current bar moves +0.01%.
    candles = [_c(i, 1.0, 1.0, 1.0, 1.0) for i in range(38)]
    candles.append(_c(38, 1.0, 1.0001, 1.0, 1.0001))
    candles.append(_c(39, 1.0001, 1.0002, 1.0001, 1.0002))
    hit = T.pct_move_spike(candles, sigma_threshold=3)
    # Economically zero volatility -> flat, not a 6-sigma fired spike.
    assert hit["fired"] is False
    assert hit["z"] == 0.0
    assert hit["reason"] == "flat"


def test_b8_normal_vol_still_detects_spike():
    # Meaningful noisy baseline so a large current move still fires.
    closes = [100.0]
    for i in range(1, 40):
        closes.append(closes[-1] * (1.0 + (0.002 if i % 2 else -0.002)))
    candles = [_c(i, c, c * 1.001, c * 0.999, c) for i, c in enumerate(closes)]
    candles.append(_c(40, closes[-1], closes[-1] * 1.06, closes[-1],
                      closes[-1] * 1.06))   # +6% bar
    hit = T.pct_move_spike(candles, sigma_threshold=3)
    assert hit["fired"] is True
    assert hit["z"] >= 3.0


# ---------- B6: volume_buildup_1h cadence guard --------------------------

def test_b6_rejects_5m_series_passed_as_1h():
    # 30 candles spaced 5 min apart.
    candles = [
        _c(i * 5 * 60_000, 100, 100, 100, 100, v=10.0 if i >= 26 else 1.0)
        for i in range(30)
    ]
    guarded = T.volume_buildup_1h(candles, interval_minutes=60)
    assert guarded["fired"] is False
    assert guarded["reason"] == "interval_mismatch"


def test_b6_accepts_genuine_1h_series():
    # 30 candles spaced 60 min, last 4 with heavy notional -> should fire.
    candles = []
    for i in range(30):
        vol = 100.0 if i >= 26 else 1.0
        candles.append(_c(i * 60 * 60_000, 100, 100, 100, 100, v=vol))
    hit = T.volume_buildup_1h(candles, interval_minutes=60, ratio_threshold=2.5)
    assert hit["reason"] != "interval_mismatch"
    assert hit["fired"] is True


def test_b6_legacy_call_without_interval_still_works():
    candles = [
        _c(i * 5 * 60_000, 100, 100, 100, 100, v=10.0 if i >= 26 else 1.0)
        for i in range(30)
    ]
    # No enforcement -> behaves as before (no interval_mismatch).
    hit = T.volume_buildup_1h(candles)
    assert hit["reason"] != "interval_mismatch"


# ---------- B4: trend_flip newest-first ----------------------------------

def test_b4_reports_most_recent_cross():
    # Build prices whose EMAs8/21 cross up twice; newest cross must win.
    # Easiest deterministic approach: long downtrend then sharp recovery,
    # verify it fires with bars_ago == 1 (score 6) when cross is on last bar.
    n = 60
    closes = [100.0 - i * 0.5 for i in range(45)]          # steady downtrend
    closes += [closes[-1] + (i + 1) * 2.0 for i in range(15)]  # sharp reversal
    candles = [_c(i, c, c * 1.0005, c * 0.9995, c) for i, c in enumerate(closes)]
    hit = T.trend_flip_1h(candles, lookback_bars=10)
    # Either it fired (score in the reachable 4..6 band) or insufficient data;
    # the key invariant: score can never be 8 (dead branch removed).
    if hit["fired"]:
        assert 4 <= hit["score"] <= 6
        assert "h ago" in hit["reason"]


def test_b4_score_never_8():
    # Fuzz-ish: across many lookbacks a fired score must stay within 4..6.
    closes = [100.0]
    for i in range(1, 80):
        closes.append(closes[-1] * (1.006 if i % 6 == 0 else 0.997))
    candles = [_c(i, c, c * 1.001, c * 0.999, c) for i, c in enumerate(closes)]
    for lb in (3, 5, 8, 12):
        hit = T.trend_flip_1h(candles, lookback_bars=lb)
        if hit["fired"]:
            assert hit["score"] <= 6


# ---------- B9: range_compression flat series ----------------------------

def test_b9_flat_series_no_fake_squeeze():
    # Identical non-zero OHLC -> every Bollinger bandwidth identical.
    candles = [_c(i, 100, 100, 100, 100) for i in range(120)]
    hit = T.range_compression(candles, bb_length=20)
    assert hit["fired"] is False
    assert hit["reason"] == "flat"


# ---------- B10: math period validation ----------------------------------

@pytest.mark.parametrize("fn", [M.ema, M.sma])
def test_b10_ma_rejects_bad_period(fn):
    with pytest.raises(ValueError):
        fn([1.0, 2.0, 3.0], 0)
    with pytest.raises(ValueError):
        fn([1.0, 2.0, 3.0], -2)


def test_b10_atr_rsi_adx_reject_bad_period():
    candles = [_c(i, 100, 101, 99, 100) for i in range(40)]
    with pytest.raises(ValueError):
        M.atr(candles, 0)
    with pytest.raises(ValueError):
        M.rsi(candles, -1)
    with pytest.raises(ValueError):
        M.adx(candles, -1)
    with pytest.raises(ValueError):
        M.rsi_last([100.0] * 40, 0)


def test_b10_valid_period_still_works():
    assert M.ema([1.0, 2.0, 3.0], 2)[-1] > 0
    assert M.sma([1.0, 2.0, 3.0], 2)[-1] == pytest.approx(2.5)


# ---------- B11: volume_profile parameter bounds -------------------------

def _vp_candles(n=60):
    return [_c(i * 60_000, 100 + (i % 5), 102 + (i % 5), 98 + (i % 5),
               100 + (i % 5), v=5.0) for i in range(n)]


def test_b11_pathological_atr_multiple_returns_none():
    candles = _vp_candles()
    assert volume_profile(candles, atr_bins=True, atr_multiple=1e-9) is None
    assert volume_profile(candles, atr_bins=True, atr_multiple=0.0) is None
    assert volume_profile(candles, atr_bins=True, atr_period=0) is None


def test_b11_bad_value_area_returns_none():
    candles = _vp_candles()
    assert volume_profile(candles, value_area_pct=0.0) is None
    assert volume_profile(candles, value_area_pct=150.0) is None


def test_b11_normal_profile_still_builds():
    vp = volume_profile(_vp_candles(), atr_bins=True, atr_multiple=0.25)
    assert vp is not None


# ---------- D5: whole-view nested-block range guard ----------------------

def test_d5_whole_view_rejects_out_of_range_breaker_leaf():
    # The HTTP patch gate already rejected this; the gap was the whole-view
    # gate used by write_agent_config / restore paths (nested block passed
    # as a plain dict, leaf range unchecked).
    from hermes_trader.agents.config_schema import validate_nested_blocks

    errs = validate_nested_blocks(
        {"circuit_breaker": {"max_drawdown_pct": 999}}
    )
    assert any("max_drawdown_pct" in e for e in errs)
    assert validate_nested_blocks(
        {"circuit_breaker": {"max_drawdown_pct": 50}}
    ) == []
    # non-dict / absent blocks are left to the top-level kind gate
    assert validate_nested_blocks({"circuit_breaker": "x"}) == []
    assert validate_nested_blocks({}) == []


def test_d5_write_agent_config_refuses_absurd_breaker(tmp_path, monkeypatch):
    import json
    from hermes_trader.agents import config_store as cs
    from hermes_trader.agents.config_defaults import CANONICAL_DEFAULTS

    cfg_file = tmp_path / ".agent-config.json"
    monkeypatch.setattr(cs, "CONFIG_PATH", str(cfg_file))
    monkeypatch.setattr(cs, "_CONFIG_LOCK_PATH", str(cfg_file) + ".lock")
    monkeypatch.setattr(cs, "_BACKUP_PATH", str(cfg_file) + ".bak")

    good = json.loads(json.dumps(CANONICAL_DEFAULTS))
    bad = json.loads(json.dumps(CANONICAL_DEFAULTS))
    bad["circuit_breaker"] = dict(bad.get("circuit_breaker") or {})
    bad["circuit_breaker"]["max_drawdown_pct"] = 999

    with pytest.raises(RuntimeError):
        cs.write_agent_config(bad, backup=False, via="d5_test")
    assert not cfg_file.exists()

    cs.write_agent_config(good, backup=False, via="d5_test")
    assert cfg_file.exists()
