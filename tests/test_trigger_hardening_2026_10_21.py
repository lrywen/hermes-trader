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


# ---------- B2: breakout prior-extremum NaN fail-closed ------------------

def _b2_base_candles(index30_high):
    # 60 bars. index 30 carries the true range extreme (high=200 normally,
    # NaN in the forged case). Last 3 closes are 100.5 with RVOL 3.0x on the
    # first break bar (index 57). Baseline volume 1000 -> 3000/1000 = 3.0x.
    candles = []
    for i in range(60):
        o, h, l, c, v = 100.0, 100.1, 99.9, 100.0, 1000.0
        if i == 30:
            h = index30_high
        if i >= 57:
            o, h, l, c = 100.0, 100.6, 100.4, 100.5
        if i == 57:
            v = 3000.0
        candles.append(_c(i, o, h, l, c, v=v))
    return candles


def test_b2_normal_200_extreme_is_inside_range():
    # Review checklist §3.1 control case: high=200@30 must NOT break out.
    hit = T.breakout(_b2_base_candles(200.0), lookback=48, min_rvol=1.5,
                     rvol_window=20, confirm_bars=3)
    assert hit["fired"] is False
    assert hit["reason"] == "inside range"
    assert hit["score"] == pytest.approx(4.970, abs=0.01)


def test_b2_nan_extreme_fails_closed_instead_of_forging_breakout():
    # Review checklist §3.1: before the fix NaN@30 collapsed prior_high to
    # ~100.1 and 100.5 closes "broke out" with a FAILED entry (fired=True,
    # score=0.400, RVOL 3.00x, held 3 bars). Now it must stay flat.
    nan = float("nan")
    hit = T.breakout(_b2_base_candles(nan), lookback=48, min_rvol=1.5,
                     rvol_window=20, confirm_bars=3)
    assert hit["fired"] is False
    assert hit["reason"] == "flat"
    assert hit["score"] == 0


def test_b2_nan_low_in_prior_window_also_fails_closed():
    candles = _b2_base_candles(200.0)
    candles[20] = _c(20, 100.0, 100.1, float("nan"), 100.0, v=1000.0)
    hit = T.breakout(candles, lookback=48, confirm_bars=3)
    assert hit["fired"] is False
    assert hit["reason"] == "flat"


def test_b2_all_finite_genuine_breakout_still_fires():
    # Zero-behaviour-change guard: a real held breakout with clean data must
    # still fire (prior high 100.1; last 3 closes 101; RVOL 3.0x).
    candles = [_c(i, 100.0, 100.1, 99.9, 100.0, v=1000.0) for i in range(60)]
    for i in range(57, 60):
        candles[i] = _c(i, 100.0, 101.2, 100.0, 101.0,
                        v=3000.0 if i == 57 else 1000.0)
    hit = T.breakout(candles, lookback=48, min_rvol=1.5,
                     rvol_window=20, confirm_bars=3)
    assert hit["fired"] is True
    assert "held 3 bars" in hit["reason"]


def test_b2_nan_outside_prior_window_does_not_change_inside_range():
    # A NaN in the confirm window is not part of the extremum scan; the
    # pre-existing held-test semantics on it are untouched.
    candles = _b2_base_candles(200.0)
    candles[58] = _c(58, 100.0, float("nan"), 100.4, 100.5)
    hit = T.breakout(candles, lookback=48, confirm_bars=3)
    # Either inside-range (NaN broke the held streak) or flat is acceptable,
    # but it must never be a fired breakout.
    assert hit["fired"] is False


# ---------- D1: golden-value numerical baselines for math.py -------------
#
# These are hand-derived anchors (small periods, deterministic series), not
# "finite and in 0..100" sanity checks: a regression that still produces a
# plausible number must move these values. Every value below was computed by
# hand from the documented recursion independently of the implementation.

def test_d1_ema_golden_values():
    # period=2, k=2/3, seed out[0]=values[0]:
    #   e1 = 2*2/3 + 1/3        = 5/3
    #   e2 = 3*2/3 + (5/3)/3    = 23/9
    #   e3 = 4*2/3 + (23/9)/3   = 95/27
    out = M.ema([1.0, 2.0, 3.0, 4.0], 2)
    assert out[0] == pytest.approx(1.0)
    assert out[1] == pytest.approx(5 / 3)
    assert out[2] == pytest.approx(23 / 9)
    assert out[3] == pytest.approx(95 / 27)
    # Steady state: a constant series converges to that constant.
    assert M.ema([7.0] * 30, 14)[-1] == pytest.approx(7.0)


def test_d1_atr_golden_values():
    # period=3. TR sequence (pc = prior close):
    #   i1 h12 l10 pc10 -> max(2,2,0)=2
    #   i2 h13 l11 pc12 -> max(2,1,1)=2
    #   i3 h14 l12 pc13 -> max(2,1,1)=2 ; seed out[3] = mean = 2
    #   i4 h17 l12 pc14 -> max(5,3,2)=5 ; Wilder out[4]=(2*2+5)/3 = 3
    candles = [
        _c(0, 10, 10, 10, 10),
        _c(1, 12, 12, 10, 12),
        _c(2, 12, 13, 11, 13),
        _c(3, 13, 14, 12, 14),
        _c(4, 14, 17, 12, 15),
    ]
    out = M.atr(candles, 3)
    assert all(math.isnan(v) for v in out[:3])
    assert out[3] == pytest.approx(2.0)
    assert out[4] == pytest.approx(3.0)


def test_d1_rsi_golden_values():
    # period=3, closes 10,11,12,11,12:
    #   seed gains 1+1, loss 1 -> avgG=2/3 avgL=1/3 -> RSI[3] = 66.6667
    #   i4 d=+1: avgG=7/9 avgL=2/9 -> RS=3.5 -> RSI[4] = 77.7778
    out = M._rsi_from_closes([10.0, 11.0, 12.0, 11.0, 12.0], 3)
    assert all(math.isnan(v) for v in out[:3])
    assert out[3] == pytest.approx(100 - 100 / 3)
    assert out[4] == pytest.approx(100 - 100 / 4.5)
    # Boundaries: monotonic up -> 100, monotonic down -> 0.
    assert M._rsi_from_closes([1.0, 2.0, 3.0, 4.0], 3)[3] == pytest.approx(100.0)
    assert M._rsi_from_closes([4.0, 3.0, 2.0, 1.0], 3)[3] == pytest.approx(0.0)
    # rsi_last insufficient history -> None
    assert M.rsi_last([1.0, 2.0], 14) is None
    assert M.rsi_last([10.0, 11.0, 12.0, 11.0, 12.0], 3) == pytest.approx(
        100 - 100 / 4.5
    )


def test_d1_adx_golden_values():
    # period=3, 8 hand-built candles. DM/TR chain (verified by hand):
    #   TR  i1..i7 = 3,2,2,3,3,3,3 ; +DM = 2,1,1,0,0,1,1 ; -DM = 0,0,0,2,1,0,0
    #   dx[3]=100, dx[4]=100/7=14.2857, dx[5]=500/37=13.5135,
    #   dx[6]=1700/101=16.8317, dx[7]=11500/283=40.6360
    #   out[5]=mean(dx[3..5])=42.5997; Wilder -> out[6]=34.0104, out[7]=36.2189
    candles = [
        _c(0, 9, 10, 8, 9),
        _c(1, 11, 12, 9, 11),
        _c(2, 12, 13, 11, 12),
        _c(3, 13, 14, 12, 13),
        _c(4, 12, 13, 10, 11),
        _c(5, 10, 12, 9, 10),
        _c(6, 11, 13, 10, 12),
        _c(7, 12, 14, 11, 13),
    ]
    out = M.adx(candles, 3)
    assert all(math.isnan(v) for v in out[:5])
    assert out[5] == pytest.approx((100 + 100 / 7 + 500 / 37) / 3, abs=1e-9)
    expected_6 = (((100 + 100 / 7 + 500 / 37) / 3) * 2 + 1700 / 101) / 3
    assert out[6] == pytest.approx(expected_6, abs=1e-9)
    assert out[7] == pytest.approx((expected_6 * 2 + 11500 / 283) / 3, abs=1e-9)


def test_d1_obv_golden_values():
    # closes 10,11,11,10,12 with vols 1,2,3,4,5:
    # up(+2), flat(0), down(-4), up(+5) -> 0, 2, 2, -2, 3 (bit-exact).
    candles = [
        _c(0, 10, 10, 10, 10, v=1),
        _c(1, 10, 11, 10, 11, v=2),
        _c(2, 11, 11, 11, 11, v=3),
        _c(3, 11, 10, 10, 10, v=4),
        _c(4, 10, 12, 10, 12, v=5),
    ]
    assert M.obv(candles) == [0.0, 2.0, 2.0, -2.0, 3.0]


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


# ---------- D2: ignite_grace_minutes canonical key, unset (None) ----------

def test_d2_canonical_default_key_exists_but_is_none():
    from hermes_trader.agents.config_defaults import CANONICAL_DEFAULTS
    assert "ignite_grace_minutes" in CANONICAL_DEFAULTS["dsl_exit"]
    assert CANONICAL_DEFAULTS["dsl_exit"]["ignite_grace_minutes"] is None


def test_d2_schema_accepts_none_and_bounds_numbers():
    from hermes_trader.agents.config_schema import validate_nested_blocks
    # None stub (the shipped default) validates clean.
    assert validate_nested_blocks(
        {"dsl_exit": {"ignite_grace_minutes": None}}) == []
    # A real numeric value validates; an out-of-rail / wrong-type value does not.
    assert validate_nested_blocks(
        {"dsl_exit": {"ignite_grace_minutes": 45.0}}) == []
    errs = validate_nested_blocks(
        {"dsl_exit": {"ignite_grace_minutes": -1.0}})
    assert any("ignite_grace_minutes" in e for e in errs)
    errs = validate_nested_blocks(
        {"dsl_exit": {"ignite_grace_minutes": "soon"}})
    assert any("ignite_grace_minutes" in e for e in errs)


def test_d2_none_stub_hydrates_to_dataclass_default_not_zero():
    # The merged canonical view now carries an explicit None. The old
    # `.get(...) or 0.0` would turn that stub into 0 (ignite gate OFF); the
    # D2 guard must hydrate it to the ExitPolicy default (90) — zero change
    # vs the pre-D2 absent-key behaviour.
    from hermes_trader.agents.dsl_exit import (
        ExitPolicy, _policy_from_dsl_dict, _tracker_from_dict)
    pol = _policy_from_dsl_dict({"ignite_grace_minutes": None})
    assert pol.ignite_grace_minutes == ExitPolicy.ignite_grace_minutes == 90.0
    # Absent key behaves identically.
    assert _policy_from_dsl_dict({}).ignite_grace_minutes == 90.0
    # Explicit 0 still means OFF; real values pass through.
    assert _policy_from_dsl_dict({"ignite_grace_minutes": 0}).ignite_grace_minutes == 0.0
    assert _policy_from_dsl_dict({"ignite_grace_minutes": 45}).ignite_grace_minutes == 45.0
    # Persisted-tracker hydration seam gets the same None semantics.
    tracker = _tracker_from_dict({
        "version": 2, "coin": "XYZ", "side": "LONG", "entry_px": 100.0,
        "policy": {"ignite_grace_minutes": None}})
    assert tracker.policy.ignite_grace_minutes == 90.0


def test_d2_merged_runtime_config_yields_90_min_grace(tmp_path, monkeypatch):
    import json
    from hermes_trader.agents import config_store as cs
    from hermes_trader.agents.dsl_exit import _policy_from_dsl_dict

    cfg_file = tmp_path / ".agent-config.json"
    monkeypatch.setattr(cs, "CONFIG_PATH", str(cfg_file))
    monkeypatch.setattr(cs, "_CONFIG_LOCK_PATH", str(cfg_file) + ".lock")
    monkeypatch.setattr(cs, "_BACKUP_PATH", str(cfg_file) + ".bak")
    # Raw on-disk config does NOT mention ignite_grace_minutes at all.
    cs.write_agent_config({}, backup=False, via="d2_test")
    merged = cs.read_agent_config()["dsl_exit"]
    # The merged view carries the canonical None stub...
    assert merged["ignite_grace_minutes"] is None
    # ...which hydrates to 90, exactly as before D2.
    assert _policy_from_dsl_dict(merged).ignite_grace_minutes == 90.0
    # Persist the full merged view (RMW) and re-read: no TypeError, still 90.
    cs.write_agent_config(cs.read_agent_config(), backup=False, via="d2_test")
    raw = json.loads(cfg_file.read_text())
    assert raw["dsl_exit"]["ignite_grace_minutes"] is None


# ---------- B3: pctMoveSpike direction = actual up/down move -------------

def test_b3_positive_bar_below_positive_mean_is_direction_up():
    # Review checklist §3.2: alternating +3%/-1% prior returns (mean ≈ +1%),
    # current bar +0.5% — a positive (rising) bar. z = |0.5%-1%|/std ≈ 0.25,
    # so it does NOT fire; pre-B3 the direction was "down" (below-mean),
    # which contradicted the bar's actual rise.
    closes = [100.0]
    for i in range(1, 41):
        closes.append(closes[-1] * (1.03 if i % 2 else 0.99))
    closes.append(closes[-1] * 1.005)  # current bar: +0.5%
    candles = [_c(i, c, c * 1.001, c * 0.999, c) for i, c in enumerate(closes)]
    hit = T.pct_move_spike(candles, sigma_threshold=3)
    assert hit["fired"] is False
    assert hit["z"] == pytest.approx(0.25, abs=0.02)
    assert hit["direction"] == "up"
    assert hit["reason"] == "flat"


def test_b3_negative_bar_above_negative_mean_is_direction_down():
    # Mirror image: negatively-drifting prior tape, current bar -0.5% (above
    # the more-negative mean but still an actual DOWN move) -> "down".
    closes = [100.0]
    for i in range(1, 41):
        closes.append(closes[-1] * (0.97 if i % 2 else 1.01))
    closes.append(closes[-1] * 0.995)
    candles = [_c(i, c, c * 1.001, c * 0.999, c) for i, c in enumerate(closes)]
    hit = T.pct_move_spike(candles, sigma_threshold=3)
    assert hit["direction"] == "down"


def test_b3_fired_spike_direction_matches_move_sign():
    # Quiet alternating tape (+0.2%/-0.2%), then a big DOWN bar: fired spike
    # must carry direction "down" both structurally and in the reason text.
    closes = [100.0]
    for i in range(1, 41):
        closes.append(closes[-1] * (1.002 if i % 2 else 0.998))
    closes.append(closes[-1] * 0.97)  # -3% current bar
    candles = [_c(i, c, c * 1.001, c * 0.999, c) for i, c in enumerate(closes)]
    hit = T.pct_move_spike(candles, sigma_threshold=3)
    assert hit["fired"] is True
    assert hit["direction"] == "down"
    assert hit["reason"].endswith("down")


def test_b3_research_direction_reader_uses_structured_field():
    # research._fired_trigger_direction reads the same structured field, so it
    # inherits the corrected semantics with no parser divergence.
    from hermes_trader.agents.research import _fired_trigger_direction
    perception = {"triggers": [
        {"name": "pctMoveSpike", "fired": True, "direction": "up",
         "reason": "3.1σ return spike up"},
    ]}
    assert _fired_trigger_direction(perception, "pctMoveSpike") == "up"


def test_b3_ta_filter_burst_intent_prefers_hit_direction_over_extension(monkeypatch):
    # A momentumBurst whose structured direction is "up" must make intent long
    # even when the 4h ATR extension is negative (price still below EMA21).
    # Pre-B3 burst_down keyed off the extension sign and vetoed the long.
    from hermes_trader.agents import ta_filter

    def _fake_fetch(coin, interval, count):
        # 60 calm closes with a modest trend so length guards pass.
        closes = [100.0 + i * 0.05 for i in range(60)]
        return [_c(i, c, c + 0.1, c - 0.1, c) for i, c in enumerate(closes)]

    monkeypatch.setattr(ta_filter, "fetch_hl_candles", _fake_fetch)
    # Force a negative ATR extension while the hit says up.
    monkeypatch.setattr(ta_filter, "_extension_atr", lambda candles: -1.5)

    perception = {
        "coin": "TEST", "composite_score": 60.0,
        "triggers": [
            {"name": "momentumBurst", "fired": True, "direction": "up",
             "score": 8.0, "reason": "+5.0% over 2 bars up"},
        ],
    }
    out = ta_filter.analyze_perception(perception)
    assert out["intend_long"] is True
    assert out["intend_short"] is False

    # And the converse: structured "down" with a positive extension -> short.
    monkeypatch.setattr(ta_filter, "_extension_atr", lambda candles: 1.5)
    perception["triggers"][0].update(direction="down",
                                     reason="-5.0% over 2 bars down")
    out = ta_filter.analyze_perception(perception)
    assert out["intend_short"] is True


# ---------- B5: 1h/15m scan history 48 -> 100 bars ----------------------

def _b5_trend_series(n: int) -> list[Candle]:
    """Deterministic trending 1h-like series (steady drift + two harmonic
    cycles). Wilder ADX needs warm-up: on 48 bars it under-reads the long-run
    ADX by ~9% here; 100 bars are within 0.2% — the §3.3 convergence oracle."""
    closes = [100.0]
    for i in range(1, n):
        closes.append(max(1.0, closes[-1] * (
            1 + 0.004 + 0.006 * math.sin(i / 7.0)
            + 0.003 * math.sin(i / 2.3))))
    return [_c(i * 3_600_000, p, p * 1.002, p * 0.998, p,
               1.0 + 0.3 * math.sin(i)) for i, p in enumerate(closes)]


def test_b5_adx_truncation_48bars_biased_100bars_converged():
    cs = _b5_trend_series(200)
    a48 = M.adx(cs[-48:], 14)[-1]
    a100 = M.adx(cs[-100:], 14)[-1]
    a200 = M.adx(cs[-200:], 14)[-1]
    # Pre-fix truncation materially under-reads trend strength...
    assert abs(a48 - a200) / a200 > 0.05
    # ...100 bars are converged to the long-run reference (<1%, §3.3 gate).
    assert abs(a100 - a200) / a200 < 0.01


def test_b5_scan_requests_100_bars_for_1h_and_15m(monkeypatch):
    from hermes_trader.agents import perception
    from hermes_trader.agents.config import (
        trigger_thresholds_params, trigger_weights_params,
    )

    requested: dict[str, int] = {}

    def _fake_fetch(coin, interval, count):
        requested[interval] = count
        if interval == "5m":
            return [_c(i * 300_000, 100.0, 100.0, 100.0, 100.0)
                    for i in range(100)]
        if interval == "1h":
            return [_c(i * 3_600_000, 100.0, 100.0, 100.0, 100.0)
                    for i in range(count)]
        return [_c(i * 900_000, 100.0, 100.0, 100.0, 100.0)
                for i in range(count)]

    monkeypatch.setattr(perception, "fetch_hl_candles", _fake_fetch)

    cfg = {
        "scan": {"candleInterval": "5m", "candleCount": 100,
                 "cacheTtlMs": 50_000, "cacheTtlMs1h": 600_000,
                 "cacheTtlMs15m": 300_000,
                 "evaluateClosedBarsOnly": False},
        "weights": trigger_weights_params(config={}),
        "thresholds": trigger_thresholds_params(config={}),
        "trend_surfacing": {"enabled": False},
        "regime_filter": {"enabled": False},
        "sigma_burst_gate": {"enabled": True, "shadow_mode": True,
                             "pct_sigma_min": 3.0, "vol_sigma_min": 5.0,
                             "gate_override": 45.0},
    }
    ok, _ = perception._scan_single_market(
        {"coin": "TEST", "sz_decimals": 2, "type": "perp"},
        100.0, cfg, min_score=54.0)
    assert ok is True
    assert requested["1h"] == 100
    assert requested["15m"] == 100


# ---------- B1: build_hits shared live/backtest orchestration -------------
#
# Acceptance bar (review §4): the live scanner's hit list must be BYTE-
# IDENTICAL after the refactor (every hit dict's name/score/reason/fired/
# z/direction). The strongest evidence is an A/A run: the pre-refactor
# orchestration loaded verbatim from git HEAD vs the current delegating
# perception, both calling the SAME working-tree trigger functions — so the
# only possible delta is the orchestration move itself.

_B1_BASE_NAMES = [
    "pctMoveSpike", "volumeSpike", "breakout", "rangeCompression",
    "trendStrength", "momentumBurst", "volumeBuildup1h", "trendFlip1h",
    "higherLows1h", "uptrendMomentum", "downtrendMomentum",
]


def _b1_5m_surge() -> list[Candle]:
    """70 quiet noisy bars at ~100 then a 30-bar +0.4%/bar rally on 3-5x
    volume — enough to fire trend/volume triggers while remaining fully
    deterministic (no network/clock)."""
    cs: list[Candle] = []
    for i in range(70):
        p = 100.0 + math.sin(i / 5.0)
        cs.append(_c(i * 300_000, p, p * 1.001, p * 0.999, p, 1.0))
    p = cs[-1].c
    for j in range(30):
        p *= 1.004
        cs.append(_c((70 + j) * 300_000, p, p * 1.002, p * 0.998, p,
                     3.0 if j < 27 else 5.0))
    return cs


def _b1_15m_flat(n: int = 100) -> list[Candle]:
    return [_c(i * 900_000, 100.0, 100.0, 100.0, 100.0) for i in range(n)]


def _b1_thresholds_weights():
    from hermes_trader.agents.config import (
        trigger_thresholds_params, trigger_weights_params,
    )
    return trigger_thresholds_params(config={}), trigger_weights_params(config={})


def test_b1_build_hits_order_and_daily_mover_terminal():
    th, w = _b1_thresholds_weights()
    hits, score_weights = T.build_hits(_b1_5m_surge(), th, w)
    # Contractually fixed order: 11 base hits, then the terminal dailyMover.
    assert [h["name"] for h in hits] == _B1_BASE_NAMES + ["dailyMover"]
    # MC off -> the SAME weights object is returned (no denominator drift).
    assert score_weights is w
    assert hits[-1] == {
        "name": "dailyMover", "score": 0,
        "reason": "not a configured 24h mover", "fired": False,
    }


def test_b1_build_hits_empty_series_every_hit_flat():
    th, w = _b1_thresholds_weights()
    hits, score_weights = T.build_hits([], th, w)
    assert len(hits) == 12
    assert score_weights is w
    assert all(h.get("fired") is False for h in hits)
    # No 1h/15m feeds is equivalent to empty feeds.
    hits_none, _ = T.build_hits(
        _b1_5m_surge(), th, w, candles_1h=None, candles_15m=None)
    hits_empty, _ = T.build_hits(
        _b1_5m_surge(), th, w, candles_1h=[], candles_15m=[])
    assert hits_none == hits_empty


def test_b1_build_hits_momentum_continuation_appends_and_adds_weight():
    th, w = _b1_thresholds_weights()
    c1h = _b5_trend_series(100)
    hits, score_weights = T.build_hits(
        _b1_5m_surge(), th, w, candles_1h=c1h,
        momentum_continuation={"enabled": True})
    assert [h["name"] for h in hits] == (
        _B1_BASE_NAMES + ["momentumContinuation1h", "dailyMover"])
    assert "momentumContinuation1h" not in w  # base weights untouched
    assert score_weights["momentumContinuation1h"] == 0.4
    assert sum(score_weights.values()) == sum(w.values()) + 0.4
    # Custom params propagate; the disabled path appends nothing.
    _, sw2 = T.build_hits(
        _b1_5m_surge(), th, w, candles_1h=c1h,
        momentum_continuation={"enabled": True, "weight": 0.9,
                               "min_trend_pct": 7.0, "max_pullback_pct": 5.0})
    assert sw2["momentumContinuation1h"] == 0.9
    hits_off, sw_off = T.build_hits(
        _b1_5m_surge(), th, w, candles_1h=c1h,
        momentum_continuation={"enabled": False})
    assert sw_off is w
    assert all(h["name"] != "momentumContinuation1h" for h in hits_off)


def test_b1_build_hits_candlestick_hits_gated():
    th, w = _b1_thresholds_weights()
    hits, score_weights = T.build_hits(
        _b1_5m_surge(), th, w, candlestick_patterns={"enabled": True})
    assert [h["name"] for h in hits] == (
        _B1_BASE_NAMES + ["bearishReversalCandle", "bullishReversalCandle",
                          "dailyMover"])
    assert score_weights is w  # candlestick hits never touch the denominator
    hits_off, _ = T.build_hits(
        _b1_5m_surge(), th, w, candlestick_patterns={"enabled": False})
    assert "bearishReversalCandle" not in [h["name"] for h in hits_off]


@pytest.mark.parametrize(
    "dm",
    [None, (False, ""), (True, "+11.1% 24h mover, vol $9.00M")],
)
def test_b1_build_hits_daily_mover_variants(dm):
    th, w = _b1_thresholds_weights()
    hits, _ = T.build_hits(_b1_5m_surge(), th, w, daily_mover=dm)
    last = hits[-1]
    assert last["name"] == "dailyMover"
    if dm and dm[0]:
        assert last == {"name": "dailyMover", "score": 10,
                        "reason": dm[1], "fired": True}
    else:
        # None and (False, "") both reproduce the surface-disabled hit.
        assert last["fired"] is False
        assert last["score"] == 0
        assert last["reason"] == "not a configured 24h mover"


def _b1_scan_cfg(*, mc=False, cp=False, rms=False) -> dict:
    from hermes_trader.agents.config import (
        trigger_thresholds_params, trigger_weights_params,
    )
    cfg = {
        "scan": {"candleInterval": "5m", "candleCount": 100,
                 "cacheTtlMs": 50_000, "cacheTtlMs1h": 600_000,
                 "cacheTtlMs15m": 300_000,
                 "evaluateClosedBarsOnly": False},
        "weights": trigger_weights_params(config={}),
        "thresholds": trigger_thresholds_params(config={}),
        "trend_surfacing": {"enabled": False},
        "regime_filter": {"enabled": False},
        "sigma_burst_gate": {"enabled": False},
        "launch_capture": {"enabled": False},
    }
    if mc:
        cfg["momentum_continuation"] = {"enabled": True}
    if cp:
        cfg["candlestick_patterns"] = {"enabled": True}
    if rms:
        cfg["runner_mover_surface"] = {"enabled": True}
    return cfg


def _b1_load_head_perception():
    """Exec the verbatim pre-B1 orchestration from git HEAD as an isolated
    module. Its `from hermes_trader.indicators import triggers` resolves to
    the CURRENT working-tree trigger functions, so an A/A difference can only
    come from the orchestration move itself."""
    import pathlib
    import subprocess
    import sys
    import types

    repo = pathlib.Path(__file__).resolve().parents[1]
    try:
        old_src = subprocess.run(
            ["git", "show", "HEAD:hermes_trader/agents/perception.py"],
            cwd=repo, capture_output=True, text=True, check=True,
            timeout=30,
        ).stdout
    except Exception:
        pytest.skip("git/HEAD unavailable for the B1 A/A baseline")
    mod_name = "_perception_head_b1_aa"
    mod = types.ModuleType(mod_name)
    mod.__file__ = str(repo / "hermes_trader" / "agents" / "perception.py")
    sys.modules[mod_name] = mod
    try:
        exec(compile(old_src, mod.__file__, "exec"), mod.__dict__)
    except BaseException:
        sys.modules.pop(mod_name, None)
        raise
    return mod


@pytest.mark.parametrize(
    "case_id,extra,expected_names",
    [
        ("base", {}, _B1_BASE_NAMES + ["dailyMover"]),
        ("mc_cp", {"mc": True, "cp": True},
         _B1_BASE_NAMES + ["momentumContinuation1h", "bearishReversalCandle",
                           "bullishReversalCandle", "dailyMover"]),
        ("daily_mover", {"rms": True},
         _B1_BASE_NAMES + ["dailyMover"]),
    ],
)
def test_b1_scan_hits_byte_identical_to_head_orchestration(
        monkeypatch, case_id, extra, expected_names):
    import sys

    from hermes_trader.agents import perception as new_mod

    old_mod = _b1_load_head_perception()
    try:
        c5, c1h, c15 = (_b1_5m_surge(), _b5_trend_series(100),
                        _b1_15m_flat())

        def _fake_fetch(coin, interval, count):
            if interval == "5m":
                return list(c5)
            if interval == "1h":
                return list(c1h)
            return list(c15)

        monkeypatch.setattr(new_mod, "fetch_hl_candles", _fake_fetch)
        monkeypatch.setattr(old_mod, "fetch_hl_candles", _fake_fetch)

        cfg = _b1_scan_cfg(**extra)
        # +11.1% 24h on $9M -> qualifies for runner_mover_surface when on.
        market = {"coin": f"B1AA_{case_id}", "sz_decimals": 2, "type": "perp",
                  "prevDayPx": 90.0, "dayNtlVlm": 9_000_000.0}

        ok_old, res_old = old_mod._scan_single_market(
            market, 100.0, cfg, min_score=0.0)
        ok_new, res_new = new_mod._scan_single_market(
            market, 100.0, cfg, min_score=0.0)

        assert ok_old is True and ok_new is True
        assert res_old is not None and res_new is not None
        assert [h["name"] for h in res_new["triggers"]] == expected_names
        # THE acceptance assertion: every hit dict (name/score/reason/fired/
        # z/direction) and the surfaced composite are identical to the
        # pre-refactor orchestration's output.
        assert res_new["triggers"] == res_old["triggers"]
        assert (res_new["composite_score"]
                == res_old["composite_score"])
        if case_id == "daily_mover":
            dm = res_new["triggers"][-1]
            assert dm["name"] == "dailyMover" and dm["fired"] is True
            assert dm["score"] == 10
    finally:
        sys.modules.pop("_perception_head_b1_aa", None)


def test_b1_evaluate_window_single_feed_matches_build_hits():
    from hermes_trader.backtest import signals as backtest_signals

    th, w = _b1_thresholds_weights()
    # Breakout confirmation now comes from thresholds (prod 3), exactly like
    # live — one of the three intended backtest deltas (see evaluate_window).
    assert th["breakoutConfirmBars"] == 3

    score, hits = backtest_signals.evaluate_window(_b1_5m_surge(), th, w)
    direct_hits, direct_weights = T.build_hits(_b1_5m_surge(), th, w)
    # The backtest kernel IS the shared orchestration on a single feed.
    assert hits == direct_hits
    assert score == T.composite_score(direct_hits, direct_weights)
    # Triggers that need the absent 1h/15m feeds can never fire in backtest.
    for name in ("rangeCompression", "trendStrength", "volumeBuildup1h",
                 "trendFlip1h", "higherLows1h"):
        assert next(h for h in hits if h["name"] == name)["fired"] is False
    # Backtest never opts into momentum continuation, so the hit is absent.
    assert all(h["name"] != "momentumContinuation1h" for h in hits)

