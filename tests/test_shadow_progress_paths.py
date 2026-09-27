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
        assert path_key in ("shadow_log_path", "sizing_v2_shadow_log_path",
                            "log_path"), row


def test_llm_probe_arms_default_to_writable_data_dir(sp):
    """Audit 2026-09-27: collect() must pass label into _arm_path so the LLM
    rollout probes default to the writable /data dir (research.py uses
    HERMES_DATA_DIR), not the read-only HOME mount. Previously the missing
    label made the rater raise a false "read-only mount" health alert while
    the real writes succeeded under /data."""
    cfg = {
        "reasoning_effort_rollout": {"mode": "enforce", "log_path": ""},
        "completion_cap_shadow": {"mode": "shadow", "log_path": ""},
    }
    for label in ("reasoning_effort_rollout", "completion_cap_shadow"):
        _, blk, env_file, default_name, _, path_key = _row(sp, label)
        path = sp._arm_path(cfg, blk, env_file, default_name, path_key,
                            label=label)
        assert path == os.path.join("/data", default_name)

    # End-to-end: collect() must not flag these arms on a read-only mount.
    sp._file_stat = lambda path: {"path": path, "exists": True, "lines": 1,
                                  "bad_lines": 0, "last_mod": None,
                                  "age_min": 0, "rotated": 0, "size": 1}
    report = sp.collect()
    by_arm = {a["arm"]: a for a in report["arms"]}
    for label in ("reasoning_effort_rollout", "completion_cap_shadow"):
        assert by_arm[label]["on_readonly_mount"] is False
    assert not [a for a in report["alerts"]
                if "只读挂载" in a
                and any(label in a for label in
                        ("reasoning_effort_rollout", "completion_cap_shadow"))]

