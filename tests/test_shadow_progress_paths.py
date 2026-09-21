"""Audit 2026-09-07 (shadow-progress path/mode resolution fix).

The read-only SHADOW collection-health inspector (scripts/shadow_progress.py)
mis-resolved the trend_filter_200ma arm so its health verdict silently lied:
the write side (risk_gates.py) reads env HERMES_TREND_FILTER_SHADOW_FILE /
HERMES_TREND_FILTER_MODE, but the inspector table carried
HERMES_TREND_FILTER_200MA_*, so an env-overridden path/mode was never
inspected at the real file.

(The sizing_v2 row and its tests were removed in the 2026-09-21 cleanup when
the sizing_v2 shadow JSONL branch was deleted.)

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


def test_every_arm_row_has_six_fields_and_valid_defaults(sp):
    """Structural guard: all rows carry (label, blk, env, default, mode_key,
    path_key) and env names follow the *_SHADOW_FILE / *_MODE convention."""
    assert len(sp.ARMS) >= 9
    for row in sp.ARMS:
        assert len(row) == 6, row
        label, blk, env_file, default_name, mode_key, path_key = row
        assert env_file.endswith("_SHADOW_FILE"), row
        assert default_name.endswith(".jsonl"), row
        assert mode_key in ("mode", "sizing_v2_mode"), row
        assert path_key in ("shadow_log_path", "sizing_v2_shadow_log_path"), row
