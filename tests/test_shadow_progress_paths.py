"""Audit 2026-09-07 (shadow-progress path/mode resolution fix).

The read-only SHADOW collection-health inspector (scripts/shadow_progress.py)
mis-resolved two arms so its health verdicts silently lied:

  * trend_filter_200ma — the write side (risk_gates.py) reads env
    HERMES_TREND_FILTER_SHADOW_FILE / HERMES_TREND_FILTER_MODE, but the
    inspector table carried HERMES_TREND_FILTER_200MA_*, so an env-overridden
    path/mode was never inspected at the real file.
  * sizing_v2 — not a standalone config block: its mode lives under
    atr_risk_sizing.sizing_v2_mode (legacy boolean sizing_v2_enabled=true ->
    enforce) and its path under atr_risk_sizing.sizing_v2_shadow_log_path
    (executor.py). The inspector read cfg["sizing_v2"], which is always absent,
    so the arm was perpetually reported "off" and its path defaulted to the
    read-only home mount.

These tests lock the corrected resolution without touching the disk or any
trade path (pure functions over an in-memory cfg dict + monkeypatched env).
"""

from __future__ import annotations

import importlib.util
import os
import pathlib

import pytest

_SCRIPT = (pathlib.Path(__file__).resolve().parents[1]
           / "scripts" / "shadow_progress.py")


def _load_module():
    spec = importlib.util.spec_from_file_location("shadow_progress_under_test",
                                                  _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def sp(monkeypatch):
    mod = _load_module()
    # Deterministic defaults regardless of the host/crawler env.
    for key in list(os.environ):
        if key.startswith("HERMES_") and key.endswith(("_SHADOW_FILE", "_MODE")):
            monkeypatch.delenv(key, raising=False)
    return mod


def _row(sp, label):
    for row in sp.ARMS:
        if row[0] == label:
            return row
    raise AssertionError(f"arm {label} missing from ARMS")


def test_trend_filter_uses_write_side_env_names(sp, monkeypatch):
    """trend_filter row must reference HERMES_TREND_FILTER_SHADOW_FILE (the
    string risk_gates.py actually writes to), not the _200MA_ variant."""
    label, blk, env_file, default_name, mode_key, path_key = _row(
        sp, "trend_filter_200ma")
    assert env_file == "HERMES_TREND_FILTER_SHADOW_FILE"
    assert default_name == "trend_filter_shadow.jsonl"

    monkeypatch.setenv("HERMES_TREND_FILTER_SHADOW_FILE", "/data/tf_probe.jsonl")
    monkeypatch.setenv("HERMES_TREND_FILTER_MODE", "shadow")
    cfg = {"trend_filter_200ma": {}}
    assert sp._arm_path(cfg, blk, env_file, default_name, path_key) == "/data/tf_probe.jsonl"
    assert sp._arm_mode(cfg, blk, env_file, mode_key) == "shadow"


def test_sizing_v2_parasitic_block_resolution(sp, monkeypatch):
    """sizing_v2 mode/path resolve from the atr_risk_sizing block using the
    sizing_v2_* keys, mirroring executor._sizing_v2_config/_sizing_v2_shadow_path."""
    label, blk, env_file, default_name, mode_key, path_key = _row(sp, "sizing_v2")
    assert blk == "atr_risk_sizing"
    assert mode_key == "sizing_v2_mode"
    assert path_key == "sizing_v2_shadow_log_path"

    # Config block wins.
    cfg = {"atr_risk_sizing": {"sizing_v2_mode": "shadow",
                               "sizing_v2_shadow_log_path": "/data/sv.jsonl"}}
    assert sp._arm_mode(cfg, blk, env_file, mode_key) == "shadow"
    assert sp._arm_path(cfg, blk, env_file, default_name, path_key) == "/data/sv.jsonl"


def test_sizing_v2_legacy_boolean_maps_to_enforce(sp):
    """Legacy atr_risk_sizing.sizing_v2_enabled=true historically meant
    enforce; the inspector must report it as active (enforce), not off."""
    label, blk, env_file, default_name, mode_key, path_key = _row(sp, "sizing_v2")
    cfg = {"atr_risk_sizing": {"sizing_v2_enabled": True}}
    assert sp._arm_mode(cfg, blk, env_file, mode_key) == "enforce"


def test_sizing_v2_env_mode_override(sp, monkeypatch):
    """Gray-release env HERMES_SIZING_V2_MODE takes precedence over config."""
    label, blk, env_file, default_name, mode_key, path_key = _row(sp, "sizing_v2")
    monkeypatch.setenv("HERMES_SIZING_V2_MODE", "enforce")
    cfg = {"atr_risk_sizing": {"sizing_v2_mode": "off"}}
    assert sp._arm_mode(cfg, blk, env_file, mode_key) == "enforce"


def test_every_arm_row_has_six_fields_and_valid_defaults(sp):
    """Structural guard: all rows carry (label, blk, env, default, mode_key,
    path_key) and env names follow the *_SHADOW_FILE / *_MODE convention."""
    assert len(sp.ARMS) >= 12
    for row in sp.ARMS:
        assert len(row) == 6, row
        label, blk, env_file, default_name, mode_key, path_key = row
        assert env_file.endswith("_SHADOW_FILE"), row
        assert default_name.endswith(".jsonl"), row
        assert mode_key in ("mode", "sizing_v2_mode"), row
        assert path_key in ("shadow_log_path", "sizing_v2_shadow_log_path"), row
