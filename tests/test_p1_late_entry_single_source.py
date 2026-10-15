"""P1-1 (audit 2026-10-15): late-entry single-source alignment.

The runner's early fail-open pre-screen and the authoritative fail-closed
ta_late_entry hard gate now share ONE threshold set and ONE strong-trend relax
predicate, expressed from research-snapshot scalars via
``late_entry_limits_from_snapshot``. These tests pin:
  * strict vs trend-relaxed limits mirror late_entry_check (75/2.5 -> 82/3.5),
  * relax requires BOTH ADX>=floor AND EMA8/21 alignment (long + short),
  * missing snapshot data is data_ok=False (caller fails open),
  * late_entry_threshold_warnings flags only divergent *explicit* legacy keys.
"""
from __future__ import annotations

from hermes_trader.agents.ta_filter import (
    late_entry_limits_from_snapshot,
    late_entry_threshold_warnings,
)

# ── snapshot limit resolver ────────────────────────────────────────────────

def test_strict_long_limits_without_trend():
    out = late_entry_limits_from_snapshot(
        "long", rsi4h=70.0, adx4h=20.0, ema8_4h=100.0, ema21_4h=101.0,
        extension_4h=1.0, params={})
    assert out["data_ok"] is True
    assert out["relaxed"] is False
    assert out["rsi_limit"] == 75.0
    assert out["ext_limit"] == 2.5


def test_strict_short_limits_without_trend():
    out = late_entry_limits_from_snapshot(
        "short", rsi4h=30.0, adx4h=20.0, ema8_4h=101.0, ema21_4h=100.0,
        extension_4h=-1.0, params={})
    assert out["relaxed"] is False
    assert out["rsi_limit"] == 25.0
    assert out["ext_limit"] == -2.5


def test_relaxed_long_needs_adx_and_alignment():
    # aligned ema8>ema21 AND adx>=35 -> 82 / 3.5
    out = late_entry_limits_from_snapshot(
        "long", rsi4h=78.0, adx4h=40.0, ema8_4h=101.0, ema21_4h=100.0,
        extension_4h=3.0, params={})
    assert out["relaxed"] is True
    assert out["rsi_limit"] == 82.0
    assert out["ext_limit"] == 3.5


def test_relaxed_short_needs_adx_and_alignment():
    out = late_entry_limits_from_snapshot(
        "short", rsi4h=22.0, adx4h=40.0, ema8_4h=99.0, ema21_4h=100.0,
        extension_4h=-3.0, params={})
    assert out["relaxed"] is True
    assert out["rsi_limit"] == 18.0
    assert out["ext_limit"] == -3.5


def test_high_adx_without_alignment_does_not_relax():
    out = late_entry_limits_from_snapshot(
        "long", rsi4h=78.0, adx4h=40.0, ema8_4h=99.0, ema21_4h=100.0,
        extension_4h=1.0, params={})
    assert out["relaxed"] is False
    assert out["rsi_limit"] == 75.0


def test_alignment_with_low_adx_does_not_relax():
    out = late_entry_limits_from_snapshot(
        "long", rsi4h=78.0, adx4h=30.0, ema8_4h=101.0, ema21_4h=100.0,
        extension_4h=1.0, params={})
    assert out["relaxed"] is False


def test_missing_snapshot_data_is_not_ok():
    out = late_entry_limits_from_snapshot(
        "long", rsi4h=None, adx4h=None, ema8_4h=None, ema21_4h=None,
        extension_4h=None, params={})
    assert out["data_ok"] is False


def test_extension_only_still_data_ok():
    # RSI missing but extension present: fail-open caller can still run the
    # extension veto with strict limits.
    out = late_entry_limits_from_snapshot(
        "long", rsi4h=None, adx4h=None, ema8_4h=None, ema21_4h=100.0,
        extension_4h=3.0, params={})
    assert out["data_ok"] is True
    assert out["relaxed"] is False
    assert out["ext_limit"] == 2.5


def test_unknown_side_is_not_ok():
    out = late_entry_limits_from_snapshot(
        "flat", rsi4h=50.0, adx4h=40.0, ema8_4h=1, ema21_4h=1,
        extension_4h=0.0, params={})
    assert out["data_ok"] is False


def test_custom_params_are_honoured():
    p = {"adx_trend_threshold": 50, "rsi_ob": 70, "rsi_ob_relaxed": 90}
    strict = late_entry_limits_from_snapshot(
        "long", 60.0, 20.0, 100.0, 101.0, 1.0, p)
    assert strict["rsi_limit"] == 70.0
    relaxed = late_entry_limits_from_snapshot(
        "long", 60.0, 55.0, 101.0, 100.0, 1.0, p)
    assert relaxed["relaxed"] is True and relaxed["rsi_limit"] == 90.0


# ── startup drift warning ──────────────────────────────────────────────────

_SSOT = {"rsi_ob": 75, "rsi_os": 25, "ext_ob": 2.5, "rsi_ob_relaxed": 82}


def test_drift_warning_silent_when_blocks_aligned():
    cfg = {"ta_late_entry": dict(_SSOT),
           "runner_entry_gate": {"rsi_overbought": 75, "rsi_oversold": 25,
                                  "max_extension_atr": 2.5,
                                  "trend_rsi_overbought": 82}}
    assert late_entry_threshold_warnings(cfg) == []


def test_drift_warning_flags_divergent_legacy_key():
    cfg = {"ta_late_entry": dict(_SSOT),
           "runner_entry_gate": {"rsi_overbought": 70}}  # legacy drift
    msgs = late_entry_threshold_warnings(cfg)
    assert any("rsi_overbought" in m and "70" in m for m in msgs)


def test_drift_warning_silent_without_ssot_block():
    # legacy runner keys remain the authority when the SSOT block is absent
    cfg = {"runner_entry_gate": {"rsi_overbought": 70}}
    assert late_entry_threshold_warnings(cfg) == []


def test_drift_warning_ignores_unset_legacy_keys():
    cfg = {"ta_late_entry": dict(_SSOT), "runner_entry_gate": {}}
    assert late_entry_threshold_warnings(cfg) == []
