"""Tests for ATR regime calibration (roadmap §1).

Covers the pure ratio→regime→factor mapping in agents.sizing
(atr_regime_calibration / ATR_REGIME_DEFAULTS), the self-contained config +
shadow-JSONL wiring in agents.executor (_atr_calib_*), and the suppression
of the legacy binary ATR-spike breaker when the calibration enforces.
Default mode is OFF: with no config, sizing behavior is byte-identical to
before this change.
"""

from __future__ import annotations

import json

from hermes_trader.agents import executor
from hermes_trader.agents.sizing import ATR_REGIME_DEFAULTS, atr_regime_calibration

_ENV_MODE = "HERMES_ATR_REGIME_CALIB_MODE"
_ENV_FILE = "HERMES_ATR_REGIME_CALIB_SHADOW_FILE"


# ── pure mapping math (agents.sizing) ───────────────────────────────────────
def test_defaults_shape():
    # Multipliers must sit on opposite sides of 1.0 and inside the safety clamp.
    assert ATR_REGIME_DEFAULTS["low_mult"] < 1.0 < ATR_REGIME_DEFAULTS["high_mult"]
    assert 0.0 < ATR_REGIME_DEFAULTS["low_ratio"] < 1.0 < ATR_REGIME_DEFAULTS["high_ratio"]
    assert ATR_REGIME_DEFAULTS["min_mult"] <= ATR_REGIME_DEFAULTS["low_mult"]
    assert ATR_REGIME_DEFAULTS["high_mult"] <= ATR_REGIME_DEFAULTS["max_mult"]


def test_no_baseline_is_neutral():
    r = atr_regime_calibration(atr_pct=1.0, atr_hist_mean_pct=0.0)
    assert r["regime"] == "normal"
    assert r["factor"] == 1.0
    assert r["ratio"] == 0.0
    # Zero current ATR likewise → neutral.
    assert atr_regime_calibration(atr_pct=0.0, atr_hist_mean_pct=1.0)["factor"] == 1.0


def test_regime_classification_and_factor_direction():
    mean = 1.0
    # Far below the mean → low vol → factor < 1 (tighter budget).
    low = atr_regime_calibration(atr_pct=0.5, atr_hist_mean_pct=mean)
    assert low["regime"] == "low"
    assert low["ratio"] == 0.5
    assert low["factor"] == ATR_REGIME_DEFAULTS["low_mult"] < 1.0
    # Within the neutral band → factor exactly 1.0.
    assert atr_regime_calibration(atr_pct=1.0, atr_hist_mean_pct=mean)["regime"] == "normal"
    assert atr_regime_calibration(atr_pct=1.0, atr_hist_mean_pct=mean)["factor"] == 1.0
    assert atr_regime_calibration(atr_pct=1.3, atr_hist_mean_pct=mean)["factor"] == 1.0
    # Far above the mean → high vol → factor > 1 (wider budget).
    high = atr_regime_calibration(atr_pct=2.0, atr_hist_mean_pct=mean)
    assert high["regime"] == "high"
    assert high["ratio"] == 2.0
    assert high["factor"] == ATR_REGIME_DEFAULTS["high_mult"] > 1.0


def test_regime_boundaries_inclusive():
    mean = 1.0
    lo = ATR_REGIME_DEFAULTS["low_ratio"]
    hi = ATR_REGIME_DEFAULTS["high_ratio"]
    assert atr_regime_calibration(atr_pct=lo * mean, atr_hist_mean_pct=mean)["regime"] == "low"
    assert atr_regime_calibration(atr_pct=hi * mean, atr_hist_mean_pct=mean)["regime"] == "high"
    # Just inside the band → normal.
    assert atr_regime_calibration(atr_pct=lo * mean + 0.001, atr_hist_mean_pct=mean)[
        "regime"] == "normal"
    assert atr_regime_calibration(atr_pct=hi * mean - 0.001, atr_hist_mean_pct=mean)[
        "regime"] == "normal"


def test_multiplier_safety_clamp():
    # Requested multipliers beyond the safety band are clamped, never exceeded.
    r = atr_regime_calibration(
        atr_pct=0.3, atr_hist_mean_pct=1.0,
        params={"low_mult": 0.1, "high_mult": 5.0},
    )
    assert r["factor"] == ATR_REGIME_DEFAULTS["min_mult"]
    r2 = atr_regime_calibration(
        atr_pct=3.0, atr_hist_mean_pct=1.0,
        params={"low_mult": 0.1, "high_mult": 5.0},
    )
    assert r2["factor"] == ATR_REGIME_DEFAULTS["max_mult"]


def test_custom_params_override():
    r = atr_regime_calibration(
        atr_pct=0.5, atr_hist_mean_pct=1.0,
        params={"low_ratio": 0.8, "low_mult": 0.9},
    )
    assert r["regime"] == "low"
    assert r["factor"] == 0.9


def test_bad_params_fall_back_to_defaults():
    # Non-numeric multipliers/thresholds are ignored (default used), not raised.
    r = atr_regime_calibration(
        atr_pct=0.5, atr_hist_mean_pct=1.0,
        params={"low_mult": "junk", "low_ratio": None},
    )
    assert r["factor"] == ATR_REGIME_DEFAULTS["low_mult"]
    # Inverted / non-positive threshold band → neutral.
    assert atr_regime_calibration(
        atr_pct=0.5, atr_hist_mean_pct=1.0,
        params={"low_ratio": 1.5, "high_ratio": 0.5},
    )["factor"] == 1.0


# ── config resolution (executor, self-contained) ────────────────────────────
def test_config_defaults_off(monkeypatch):
    monkeypatch.delenv(_ENV_MODE, raising=False)
    assert executor._atr_calib_config({})["mode"] == "off"
    assert executor._atr_calib_config({"atr_regime_calibration": {}})["mode"] == "off"


def test_config_mode_from_block(monkeypatch):
    monkeypatch.delenv(_ENV_MODE, raising=False)
    assert executor._atr_calib_config(
        {"atr_regime_calibration": {"mode": "shadow"}})["mode"] == "shadow"
    assert executor._atr_calib_config(
        {"atr_regime_calibration": {"mode": "ENFORCE"}})["mode"] == "enforce"


def test_config_env_overrides_block(monkeypatch):
    monkeypatch.setenv(_ENV_MODE, "enforce")
    assert executor._atr_calib_config(
        {"atr_regime_calibration": {"mode": "shadow"}})["mode"] == "enforce"


def test_config_invalid_mode_falls_back_off(monkeypatch):
    monkeypatch.setenv(_ENV_MODE, "bogus")
    assert executor._atr_calib_config({})["mode"] == "off"


def test_shadow_path_resolution(monkeypatch, tmp_path):
    monkeypatch.delenv(_ENV_FILE, raising=False)
    # Config block wins.
    blk = {"shadow_log_path": str(tmp_path / "from_config.jsonl")}
    assert executor._atr_calib_shadow_path(blk).endswith("from_config.jsonl")
    # Env overrides an empty block.
    monkeypatch.setenv(_ENV_FILE, str(tmp_path / "from_env.jsonl"))
    assert executor._atr_calib_shadow_path({}).endswith("from_env.jsonl")
    # Default fallback when neither set.
    monkeypatch.delenv(_ENV_FILE, raising=False)
    assert executor._atr_calib_shadow_path({}).endswith("atr_regime_calib_shadow.jsonl")


# ── _atr_calib_apply: off / shadow / enforce semantics ──────────────────────
def _apply(mode_cfg, tmp_path, **kw):
    cfg = {"atr_regime_calibration": dict(mode_cfg)}
    cfg["atr_regime_calibration"].setdefault(
        "shadow_log_path", str(tmp_path / "calib.jsonl"))
    kw.setdefault("effective_stop_pct", 2.0)
    kw.setdefault("atr_pct", 2.0)
    kw.setdefault("atr_hist_mean_pct", 1.0)  # ratio 2.0 → high regime
    kw.setdefault("config", cfg)
    kw.setdefault("coin", "TEST")
    kw.setdefault("core_stop", 1.5)
    kw.setdefault("regime_label", "scalp")
    return executor._atr_calib_apply(**kw)


def _read_jsonl(path):
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def test_apply_off_is_passthrough_and_no_file(monkeypatch, tmp_path):
    monkeypatch.delenv(_ENV_MODE, raising=False)
    out = _apply({"mode": "off"}, tmp_path)
    assert out["mode"] == "off"
    assert out["factor"] == 1.0
    assert out["effective_stop_pct"] == 2.0
    assert out["would_change"] is False
    # OFF must never touch the JSONL.
    assert not (tmp_path / "calib.jsonl").exists()


def test_apply_shadow_logs_but_keeps_raw(tmp_path):
    out = _apply({"mode": "shadow"}, tmp_path)
    # High regime (ratio 2.0) → factor 1.20, calibrated width recorded...
    assert out["regime"] == "high"
    assert out["factor"] == ATR_REGIME_DEFAULTS["high_mult"]
    assert out["calibrated_stop_pct"] == 2.0 * ATR_REGIME_DEFAULTS["high_mult"]
    assert out["would_change"] is True
    # ...but the applied effective stop stays RAW in shadow.
    assert out["effective_stop_pct"] == 2.0
    recs = _read_jsonl(tmp_path / "calib.jsonl")
    assert len(recs) == 1
    rec = recs[0]
    assert rec["mode"] == "shadow"
    assert rec["coin"] == "TEST"
    assert rec["vol_regime"] == "high"
    assert rec["would_change"] is True
    assert rec["raw_stop_pct"] == 2.0
    assert rec["calibrated_stop_pct"] == round(2.0 * ATR_REGIME_DEFAULTS["high_mult"], 4)


def test_apply_enforce_uses_calibrated_and_logs(tmp_path):
    out = _apply({"mode": "enforce"}, tmp_path)
    assert out["effective_stop_pct"] == 2.0 * ATR_REGIME_DEFAULTS["high_mult"]
    recs = _read_jsonl(tmp_path / "calib.jsonl")
    assert len(recs) == 1
    assert recs[0]["mode"] == "enforce"


def test_apply_normal_regime_records_no_change(tmp_path):
    out = _apply({"mode": "shadow"}, tmp_path, atr_pct=1.0, atr_hist_mean_pct=1.0)
    assert out["regime"] == "normal"
    assert out["factor"] == 1.0
    assert out["would_change"] is False
    assert out["effective_stop_pct"] == 2.0
    # Normal regime is still logged (every v2 sizing records an observation).
    recs = _read_jsonl(tmp_path / "calib.jsonl")
    assert len(recs) == 1
    assert recs[0]["vol_regime"] == "normal"


def test_apply_no_baseline_stays_neutral_even_in_enforce(tmp_path):
    out = _apply({"mode": "enforce"}, tmp_path, atr_pct=1.0, atr_hist_mean_pct=0.0)
    assert out["regime"] == "normal"
    assert out["factor"] == 1.0
    assert out["effective_stop_pct"] == 2.0


# ── legacy spike breaker suppression (compute_effective_stop_pct) ───────────
def _dsl_cfg():
    # atr_stop enabled so the ATR clamp path runs; no ROE cap binding here.
    return {
        "atr_stop": {"enabled": True, "atr_mult": 1.5, "floor_pct": 1.0,
                     "ceiling_pct": 4.0},
        "risk_management": {},
    }


def test_spike_breaker_fires_by_default():
    # ratio 3.0 (>2x) → legacy breaker tightens effective by 30%.
    eff = executor.compute_effective_stop_pct(
        _dsl_cfg(), "neutral", leverage=1.0, atr_pct=3.0,
        avg_exit_slip_pct=0.0, atr_hist_mean_pct=1.0)
    assert eff["atr_spike"] is True
    # core_stop is the un-tightened mirror; effective is 0.70x it (slip 0).
    assert eff["effective_stop_pct"] < eff["core_stop"]


def test_spike_breaker_suppressed_when_disabled():
    eff = executor.compute_effective_stop_pct(
        _dsl_cfg(), "neutral", leverage=1.0, atr_pct=3.0,
        avg_exit_slip_pct=0.0, atr_hist_mean_pct=1.0,
        atr_spike_enabled=False)
    assert eff["atr_spike"] is False
    # With the breaker off, effective equals core (slip 0).
    assert eff["effective_stop_pct"] == eff["core_stop"]


def test_core_stop_unaffected_by_spike_or_calibration_layer():
    # The byte-aligned core_stop must be identical whether the breaker fires
    # or not (it is sizing-only and excluded from the drift assertion).
    a = executor.compute_effective_stop_pct(
        _dsl_cfg(), "neutral", 1.0, 3.0, 0.0, 1.0, atr_spike_enabled=True)
    b = executor.compute_effective_stop_pct(
        _dsl_cfg(), "neutral", 1.0, 3.0, 0.0, 1.0, atr_spike_enabled=False)
    assert a["core_stop"] == b["core_stop"]
