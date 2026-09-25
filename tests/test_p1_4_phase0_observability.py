"""P1-4 Phase 0 — configuration-plane observability hardening (2026-09-17).

Read-only behavioral surface: no trading semantics change, no env removed.

P0-1  the 4th gray-release mode (HERMES_SIGNAL_AGE_DECAY_MODE, read by
      perception._age_decay_config) is registered in config_store's legacy env
      list, so it appears in the startup snapshot like the other three modes.
P0-2  the sizing_v2 one-time env-vs-config drift alarm is generalized to all
      four gray-release modes: when a valid env mode actively overrides the
      persisted file value (file absent/off counts too), a warning fires once
      per process and a config_env_drift session event is appended. Aligned or
      absent env stays silent.
P0-3  the snapshot gains an ``accessor_effective`` view resolving loop_runtime
      knobs and the four gray modes through their REAL module accessors — the
      canonical provenance walk cannot see legacy env overrides there.
P0-4  three dedicated kill/observability switches that bypass HERMES_CFG_
      (HERMES_HL_RATE_STATS, HERMES_PRICE_CROSSCHECK_ENABLED,
      HERMES_IP_DRIFT_WATCH) are snapshotted even when unset, because two of
      them default ON.
"""

from __future__ import annotations

import logging

import pytest

from hermes_trader import loop_runtime, session_log
from hermes_trader.agents import config_store, executor, perception

# ── P0-1: 4th gray-release mode registered ─────────────────────────────────

def test_p01_signal_age_mode_visible_in_legacy_env_overrides(monkeypatch):
    monkeypatch.setenv("HERMES_SIGNAL_AGE_DECAY_MODE", "shadow")
    snap = config_store.build_effective_config_snapshot()
    assert (snap["legacy_env_overrides"]["HERMES_SIGNAL_AGE_DECAY_MODE"]
            == "shadow")


def test_p01_all_four_gray_modes_registered():
    assert set(config_store._GRAY_MODE_ENV_KEYS) == {
        "HERMES_CONFIDENCE_DECAY_MODE",
        "HERMES_ATR_REGIME_CALIB_MODE",
        "HERMES_SIZING_V2_MODE",
        "HERMES_SIGNAL_AGE_DECAY_MODE",
    }


# ── P0-2: drift alarm generalized to all four modes ────────────────────────

@pytest.fixture
def drift_events(monkeypatch):
    """Capture session_log.append records instead of writing disk."""
    config_store.reset_legacy_mode_drift_warnings()
    events: list[dict] = []
    monkeypatch.setattr(session_log, "append", lambda rec: events.append(rec))
    yield events
    config_store.reset_legacy_mode_drift_warnings()


_DRIFT_CASES = (
    "env_name,cfg,key,resolve",
    [
        ("HERMES_ATR_REGIME_CALIB_MODE",
         {"atr_regime_calibration": {"mode": "off"}},
         "atr_regime_calibration.mode",
         lambda c: executor._atr_calib_config(c)["mode"]),
        ("HERMES_CONFIDENCE_DECAY_MODE",
         {"confidence_decay": {"mode": "off"}},
         "confidence_decay.mode",
         lambda c: executor._confidence_decay_config(c)["mode"]),
        ("HERMES_SIZING_V2_MODE",
         {"atr_risk_sizing": {"sizing_v2_enabled": False}},
         "atr_risk_sizing.sizing_v2_mode",
         lambda c: executor._sizing_v2_config(c)["mode"]),
        ("HERMES_SIGNAL_AGE_DECAY_MODE",
         {"signal_age_decay": {"mode": "off"}},
         "signal_age_decay.mode",
         lambda c: perception._age_decay_config(c)["mode"]),
    ],
)


@pytest.mark.parametrize(*_DRIFT_CASES)
def test_p02_drift_alarm_fires_when_env_overrides_file(
        monkeypatch, caplog, drift_events, env_name, cfg, key, resolve):
    monkeypatch.setenv(env_name, "shadow")
    with caplog.at_level(logging.WARNING):
        effective = resolve(cfg)
    assert effective == "shadow"
    drift = [e for e in drift_events if e.get("event") == "config_env_drift"]
    assert len(drift) == 1
    assert drift[0]["key"] == key
    assert drift[0]["env_name"] == env_name
    assert drift[0]["env_value"] == "shadow"
    assert drift[0]["config_value"] == "off"
    assert drift[0]["effective"] == "shadow"
    assert any("drift" in rec.message for rec in caplog.records)


@pytest.mark.parametrize(*_DRIFT_CASES)
def test_p02_drift_alarm_silent_when_env_absent(
        monkeypatch, caplog, drift_events, env_name, cfg, key, resolve):
    monkeypatch.delenv(env_name, raising=False)
    with caplog.at_level(logging.WARNING):
        resolve(cfg)
    assert not [e for e in drift_events if e.get("event") == "config_env_drift"]
    assert not any("drift" in rec.message for rec in caplog.records)


@pytest.mark.parametrize(*_DRIFT_CASES)
def test_p02_drift_alarm_silent_when_aligned(
        monkeypatch, caplog, drift_events, env_name, cfg, key, resolve):
    monkeypatch.setenv(env_name, "off")
    with caplog.at_level(logging.WARNING):
        resolve(cfg)
    assert not [e for e in drift_events if e.get("event") == "config_env_drift"]


def test_p02_drift_alarm_fires_once_per_process(
        monkeypatch, caplog, drift_events):
    monkeypatch.setenv("HERMES_ATR_REGIME_CALIB_MODE", "shadow")
    cfg = {"atr_regime_calibration": {"mode": "off"}}
    with caplog.at_level(logging.WARNING):
        executor._atr_calib_config(cfg)
        executor._atr_calib_config(cfg)
    drift = [e for e in drift_events if e.get("event") == "config_env_drift"]
    assert len(drift) == 1


def test_p02_drift_alarm_silent_on_invalid_env(
        monkeypatch, caplog, drift_events):
    monkeypatch.setenv("HERMES_ATR_REGIME_CALIB_MODE", "banana")
    cfg = {"atr_regime_calibration": {"mode": "off"}}
    with caplog.at_level(logging.WARNING):
        resolved = executor._atr_calib_config(cfg)
    assert resolved["mode"] == "off"
    assert not [e for e in drift_events if e.get("event") == "config_env_drift"]


def test_p02_sizing_v2_legacy_bool_no_longer_counts_as_file_enforce(
        monkeypatch, caplog, drift_events):
    """P1-4 Phase 1 step 5: the retired sizing_v2_enabled boolean must not
    count as a file-side mode; env=shadow vs bool=true reports the file
    value as off and still alarms on the env override."""
    monkeypatch.setenv("HERMES_SIZING_V2_MODE", "shadow")
    cfg = {"atr_risk_sizing": {"sizing_v2_enabled": True}}
    effective = executor._sizing_v2_config(cfg)
    assert effective["mode"] == "shadow"
    drift = [e for e in drift_events if e.get("event") == "config_env_drift"]
    assert len(drift) == 1
    assert drift[0]["config_value"] == "off"


# ── P0-3: accessor-effective snapshot view ─────────────────────────────────

def test_p03_accessor_view_closes_loop_runtime_blind_spot(
        monkeypatch, tmp_path):
    missing = tmp_path / "no-config.json"
    monkeypatch.setattr(config_store, "CONFIG_PATH", str(missing))
    monkeypatch.setattr(config_store, "_CONFIG_LOCK_PATH", str(missing) + ".lock")
    config_store._invalidate_raw_cache()
    monkeypatch.setenv("HERMES_SCAN_DYNAMIC", "1")
    snap = config_store.build_effective_config_snapshot()
    # Canonical provenance walk cannot see the dedicated legacy env ...
    assert snap["keys"]["loop_runtime.scan_dynamic"]["value"] is False
    # ... but the accessor-effective view resolves the ACTIVE value.
    rt = snap["accessor_effective"]["loop_runtime"]
    assert rt["scan_dynamic"] is True
    assert set(rt) == set(loop_runtime.LOOP_RUNTIME_DEFAULTS)


def test_p03_accessor_view_exposes_all_four_gray_modes(monkeypatch):
    monkeypatch.setenv("HERMES_SIGNAL_AGE_DECAY_MODE", "shadow")
    snap = config_store.build_effective_config_snapshot()
    modes = snap["accessor_effective"]["gray_modes"]
    assert set(modes) == {
        "atr_regime_calibration.mode",
        "confidence_decay.mode",
        "atr_risk_sizing.sizing_v2_mode",
        "signal_age_decay.mode",
    }
    assert modes["signal_age_decay.mode"] == "shadow"
    # The canonical leaf still shows the file/default value — the exact blind
    # spot this view exists to close.
    assert snap["keys"]["signal_age_decay.mode"]["value"] in ("off", None)


# ── P0-4: dedicated kill/observability switches registered ────────────────

def test_p04_dedicated_switches_snapshotted_even_when_unset(monkeypatch):
    for name in ("HERMES_HL_RATE_STATS", "HERMES_PRICE_CROSSCHECK_ENABLED",
                 "HERMES_IP_DRIFT_WATCH"):
        monkeypatch.delenv(name, raising=False)
    snap = config_store.build_effective_config_snapshot()
    switches = snap["env_switches"]
    assert switches["HERMES_HL_RATE_STATS"] == {
        "env": None, "default_when_unset": True}
    assert switches["HERMES_PRICE_CROSSCHECK_ENABLED"] == {
        "env": None, "default_when_unset": True}
    assert switches["HERMES_IP_DRIFT_WATCH"] == {
        "env": None, "default_when_unset": False}


def test_p04_dedicated_switches_record_raw_env_when_set(monkeypatch):
    monkeypatch.setenv("HERMES_HL_RATE_STATS", "0")
    monkeypatch.setenv("HERMES_IP_DRIFT_WATCH", "1")
    snap = config_store.build_effective_config_snapshot()
    switches = snap["env_switches"]
    assert switches["HERMES_HL_RATE_STATS"]["env"] == "0"
    assert switches["HERMES_IP_DRIFT_WATCH"]["env"] == "1"
