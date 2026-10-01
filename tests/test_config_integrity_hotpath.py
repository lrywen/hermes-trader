"""Hot-path safety for the config integrity reconciler.

Regression for the 2026-10-01 incident: ``reconcile_config_integrity`` runs on
every trading-loop heartbeat, and it used to call
``session_log.read_all_history()`` — json.loads-ing every rotated gz file plus
the whole active log into one giant list just to learn the latest
``config_write`` ts. Once the log grew, that single pure-CPU call held the GIL
for tens of minutes and stalled the heartbeat / dashboard.

The fix: the write path stamps an O(1) sidecar; the reconciler reads it and
falls back (only when the sidecar is missing) to a BOUNDED scan of the ACTIVE
log alone. These tests pin both the result and the "never scan full history"
guarantee.
"""

import json
import os
import time

import pytest

from hermes_trader import session_log
from hermes_trader.agents import config_store


@pytest.fixture
def clean_sidecar():
    # Ensure no sidecar from another test, and remove it afterwards.
    path = config_store._LAST_CONFIG_WRITE_TS_PATH
    existed = os.path.exists(path)
    backup = None
    if existed:
        with open(path, "r") as f:
            backup = f.read()
        os.unlink(path)
    yield path
    if backup is not None:
        with open(path, "w") as f:
            f.write(backup)


def _write_active_event(event: dict):
    with open(session_log.SESSION_LOG_FILE, "a") as f:
        f.write(json.dumps(event) + "\n")


def test_no_audit_returns_none_and_never_reads_history(
    monkeypatch, tmp_path, clean_sidecar
):
    # Isolate the active log so config_write events emitted by other files in
    # the same pytest session cannot satisfy the "no audit" precondition.
    monkeypatch.setattr(
        session_log, "SESSION_LOG_FILE", str(tmp_path / "session-log.jsonl"))
    called = {"n": 0}

    def _boom(*a, **k):
        called["n"] += 1
        raise AssertionError("read_all_history must not be called on hot path")

    monkeypatch.setattr(session_log, "read_all_history", _boom)
    assert config_store._last_config_write_audit_mtime_ms() is None
    assert called["n"] == 0


def test_sidecar_is_o1_and_does_not_touch_active_log(monkeypatch, clean_sidecar):
    config_store._write_last_config_write_ts(123456789)

    def _boom(*a, **k):
        raise AssertionError("active log must not be read when sidecar exists")

    monkeypatch.setattr(session_log, "tail", _boom)
    monkeypatch.setattr(session_log, "read_all_history", _boom)
    assert config_store._last_config_write_audit_mtime_ms() == 123456789


def test_fallback_reads_only_recent_active_log_lines(monkeypatch, clean_sidecar):
    ts = int(time.time() * 1000)
    _write_active_event({"ts": ts, "event": "config_write", "via": "test"})

    real_tail = session_log.tail
    seen_n = {}

    def _spy_tail(n=10):
        seen_n["n"] = n
        return real_tail(n=n)

    monkeypatch.setattr(session_log, "tail", _spy_tail)
    monkeypatch.setattr(
        session_log, "read_all_history",
        lambda *a, **k: pytest.fail("must not read rotated history"))
    assert config_store._last_config_write_audit_mtime_ms() == ts
    # Fallback scan must be capped (not 10**9 / whole file).
    assert seen_n["n"] == config_store._ACTIVE_LOG_FALLBACK_LINES
    assert seen_n["n"] < 10**9


def test_write_path_stamps_sidecar_and_reconcile_is_clean(
    monkeypatch, tmp_path, clean_sidecar
):
    # Pin CONFIG_PATH-adjacent sidecar into a tmp dir so we don't fight the
    # global path; write through the public API.
    cfg = {"mode": "SHADOW", "equity_fraction_per_trade": 0.02}
    config_store.write_agent_config(cfg, via="test")

    assert os.path.exists(clean_sidecar)
    assert config_store._read_last_config_write_ts() is not None
    # A legit audited write must NOT raise an unattributed-write incident.
    assert config_store.reconcile_config_integrity(grace_s=5.0) is None


def test_unattributed_write_detected_when_no_audit(
    monkeypatch, tmp_path, clean_sidecar
):
    # Isolate the active log: a shared session log may carry config_write
    # events emitted by earlier tests and mask the "no audit" condition.
    monkeypatch.setattr(
        session_log, "SESSION_LOG_FILE", str(tmp_path / "session-log.jsonl"))

    old_ts = time.time() - 3600
    # Ensure the config file exists (a hand-held direct write); conftest only
    # sets the path, it does not create the file.
    with open(config_store.CONFIG_PATH, "w") as f:
        f.write('{"mode": "SHADOW"}')
    os.utime(config_store.CONFIG_PATH, (old_ts, old_ts))
    incident = config_store.reconcile_config_integrity(grace_s=5.0)
    assert incident is not None
    assert incident["event"] == "config_write_unattributed"
