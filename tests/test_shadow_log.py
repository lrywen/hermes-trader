"""Audit 2026-09-06 (F2, engineering hygiene): table-driven tests for the
shared shadow/audit JSONL writer (hermes_trader.shadow_log).

Covers: happy-path append (one JSON object per line, parent dir auto-created,
UTF-8 + non-ASCII), size-based rotation chain (.1 .. .5, oldest dropped),
never-raises failure paths (empty path, non-serialisable record, unwritable
directory) returning False and incrementing the error metric.
"""
from __future__ import annotations

import json
import os
import time

import pytest

from hermes_trader import shadow_log


# ── append happy path ───────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "record",
    [
        {"a": 1},
        {"verdict": "block", "bps": 12.5, "coin": "BTC"},
        {"k": "值", "nested": {"x": [1, 2, 3]}},
        {"empty": None},
    ],
)
def test_append_jsonl_writes_one_json_line(tmp_path, record):
    path = str(tmp_path / "sub" / "shadow.jsonl")
    assert shadow_log.append_jsonl(path, record, stream="t") is True
    with open(path, "r", encoding="utf-8") as f:
        lines = f.read().splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0]) == record


def test_append_jsonl_accumulates_and_preserves_order(tmp_path):
    path = str(tmp_path / "s.jsonl")
    for i in range(3):
        assert shadow_log.append_jsonl(path, {"i": i}, stream="t") is True
    with open(path, "r", encoding="utf-8") as f:
        rows = [json.loads(line) for line in f if line.strip()]
    assert [r["i"] for r in rows] == [0, 1, 2]


# ── rotation ────────────────────────────────────────────────────────────────

def test_rotation_chain_and_oldest_drop(tmp_path, monkeypatch):
    """Once the active file crosses MAX_BYTES it becomes .1; a second crossing
    shifts .1 -> .2 ... up to BACKUP_COUNT; the oldest sibling is dropped."""
    monkeypatch.setattr(shadow_log, "MAX_BYTES", 100)
    monkeypatch.setattr(shadow_log, "BACKUP_COUNT", 3)
    # Force the module's rotation gate back on (a previous test may have
    # disabled it via disable_rotation()).
    monkeypatch.setattr(shadow_log, "_rotation_enabled", True)
    path = str(tmp_path / "rot.jsonl")

    # File 0: grow the active file past the threshold, then trigger rotation.
    shadow_log.append_jsonl(path, {"pad": "x" * 120}, stream="t")
    assert os.path.exists(path)
    shadow_log.append_jsonl(path, {"after": 1}, stream="t")
    assert os.path.exists(f"{path}.1")

    # Two more rotations: .1 -> .2 -> .3 (BACKUP_COUNT), oldest dropped.
    for n in range(2, 4):
        shadow_log.append_jsonl(path, {"pad": "y" * 120}, stream="t")
        shadow_log.append_jsonl(path, {"after": n}, stream="t")
    assert os.path.exists(f"{path}.1")
    assert os.path.exists(f"{path}.2")
    assert os.path.exists(f"{path}.3")
    # .4 must never exist (BACKUP_COUNT == 3); oldest is silently dropped.
    assert not os.path.exists(f"{path}.4")


def test_rotation_disabled_keeps_single_file(tmp_path, monkeypatch):
    monkeypatch.setattr(shadow_log, "MAX_BYTES", 10)
    shadow_log.disable_rotation()
    try:
        path = str(tmp_path / "norot.jsonl")
        for _ in range(5):
            shadow_log.append_jsonl(path, {"pad": "z" * 50}, stream="t")
        assert not os.path.exists(f"{path}.1")
        assert os.path.exists(path)
    finally:
        # Restore global state for any later test in the same process.
        shadow_log._rotation_enabled = True


# ── daily rotation (Audit 2026-09-06, F2) ───────────────────────────────────

def test_daily_rotation_rolls_stale_active_file(tmp_path, monkeypatch):
    """An active file left over from a previous local day is rotated through
    the .1/.2 chain on the first append of the new day (mtime is the source of
    truth, so this also covers the post-restart path)."""
    monkeypatch.setattr(shadow_log, "_rotation_enabled", True)
    shadow_log._active_day.clear()
    path = str(tmp_path / "daily.jsonl")

    # Yesterday's active file (mtime forced to two days ago; size well under
    # MAX_BYTES so only the daily rule can fire).
    assert shadow_log.append_jsonl(path, {"day": 0}, stream="t") is True
    old_ts = time.time() - 2 * 86400
    os.utime(path, (old_ts, old_ts))
    # Simulate a fresh process: no in-memory day cache for this path.
    shadow_log._active_day.clear()

    assert shadow_log.append_jsonl(path, {"day": 1}, stream="t") is True

    # The stale file was rotated away and the fresh active file holds only
    # today's record.
    assert os.path.exists(f"{path}.1")
    with open(f"{path}.1", "r", encoding="utf-8") as f:
        rotated = [json.loads(line) for line in f if line.strip()]
    with open(path, "r", encoding="utf-8") as f:
        active = [json.loads(line) for line in f if line.strip()]
    assert rotated == [{"day": 0}]
    assert active == [{"day": 1}]


def test_daily_rotation_same_day_keeps_single_file(tmp_path, monkeypatch):
    """Repeated appends within the same local day never trigger daily
    rotation (the size threshold is far above these tiny records)."""
    monkeypatch.setattr(shadow_log, "_rotation_enabled", True)
    shadow_log._active_day.clear()
    path = str(tmp_path / "sameday.jsonl")
    for i in range(5):
        assert shadow_log.append_jsonl(path, {"i": i}, stream="t") is True
    assert not os.path.exists(f"{path}.1")
    assert os.path.exists(path)


# ── failure paths: never raise, return False ────────────────────────────────

@pytest.mark.parametrize(
    "path,record",
    [
        ("", {"a": 1}),                       # empty path
        (None, {"a": 1}),                     # type: ignore[dict-item]
    ],
)
def test_append_jsonl_bad_path_returns_false(path, record):
    assert shadow_log.append_jsonl(path, record, stream="t") is False


def test_append_jsonl_unserialisable_returns_false(tmp_path):
    path = str(tmp_path / "s.jsonl")
    assert shadow_log.append_jsonl(path, {"bad": object()}, stream="t") is False
    assert not os.path.exists(path)


def test_append_jsonl_unwritable_dir_returns_false(tmp_path, monkeypatch):
    bad_dir = tmp_path / "readonly"
    bad_dir.mkdir()
    os.chmod(bad_dir, 0o500)  # read+execute, no write
    path = str(bad_dir / "s.jsonl")
    try:
        result = shadow_log.append_jsonl(path, {"a": 1}, stream="t")
        if os.geteuid() == 0:
            pytest.skip("root bypasses directory write permission")
        assert result is False
    finally:
        os.chmod(bad_dir, 0o755)


def test_write_errors_metric_incremented(tmp_path):
    """A swallowed failure increments SHADOW_LOG_WRITE_ERRORS by stream.

    (An empty path returns False before any write attempt and is by design not
    counted; a non-serialisable record goes through the error path.)"""
    from hermes_trader import metrics

    lbl = metrics.SHADOW_LOG_WRITE_ERRORS.labels(stream="mets")
    before = lbl._value.get()
    assert shadow_log.append_jsonl(str(tmp_path / "s.jsonl"),
                                   {"bad": object()}, stream="mets") is False
    after = lbl._value.get()
    assert after == before + 1
