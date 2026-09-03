"""P3-14: tests for the egress IP drift watchdog (scripts/ip_drift_watch.py).

The script is stdlib-only and safe to import (the daemon loop is behind
``__main__``). It is loaded directly from the scripts dir via importlib so
these tests never depend on the scripts directory being a package.
"""
from __future__ import annotations

import importlib.util
import json
import logging
from pathlib import Path

import pytest

_SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
_SPEC = importlib.util.spec_from_file_location(
    "ip_drift_watch", _SCRIPTS_DIR / "ip_drift_watch.py"
)
assert _SPEC is not None and _SPEC.loader is not None
watch = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(watch)


# ── env knobs: defaults & parsing ──────────────────────────────────────────

def test_p3_14_watch_disabled_by_default(monkeypatch):
    monkeypatch.delenv("HERMES_IP_DRIFT_WATCH", raising=False)
    assert watch.watch_enabled() is False


@pytest.mark.parametrize("token", ["1", "true", "yes", "on", "TRUE", " On "])
def test_p3_14_watch_switch_truthy(monkeypatch, token):
    monkeypatch.setenv("HERMES_IP_DRIFT_WATCH", token)
    assert watch.watch_enabled() is True


@pytest.mark.parametrize("token", ["0", "false", "no", "off", "", "junk"])
def test_p3_14_watch_switch_falsy(monkeypatch, token):
    monkeypatch.setenv("HERMES_IP_DRIFT_WATCH", token)
    assert watch.watch_enabled() is False


def test_p3_14_interval_default_and_parse(monkeypatch):
    monkeypatch.delenv("HERMES_IP_DRIFT_CHECK_S", raising=False)
    assert watch.check_interval_s() == 300
    monkeypatch.setenv("HERMES_IP_DRIFT_CHECK_S", "60")
    assert watch.check_interval_s() == 60
    # Garbage / non-positive falls back to the safe default.
    monkeypatch.setenv("HERMES_IP_DRIFT_CHECK_S", "abc")
    assert watch.check_interval_s() == 300
    monkeypatch.setenv("HERMES_IP_DRIFT_CHECK_S", "-5")
    assert watch.check_interval_s() == 300


def test_p3_14_fail_warn_after_default(monkeypatch):
    monkeypatch.delenv("HERMES_IP_DRIFT_FAIL_WARN_AFTER", raising=False)
    assert watch.fail_warn_after() == 3


def test_p3_14_paths_default_to_data(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_IP_DRIFT_STATE_FILE", str(tmp_path / "state.json"))
    monkeypatch.setenv("HERMES_IP_DRIFT_EVENTS_FILE", str(tmp_path / "events.jsonl"))
    assert watch.state_path() == tmp_path / "state.json"
    assert watch.events_path() == tmp_path / "events.jsonl"


# ── stdlib-only hard rule: the watchdog must not import trading code ───────

def test_p3_14_script_imports_stdlib_only():
    src = (_SCRIPTS_DIR / "ip_drift_watch.py").read_text(encoding="utf-8")
    # No trading-module imports / call sites (the docstring *mentions*
    # hermes_trader.event_log to document the format parity; that's why we
    # assert on import lines, not the whole source text).
    import ast
    tree = ast.parse(src)
    allowed_roots = {
        "__future__", "contextlib", "hashlib", "json", "logging", "os", "re",
        "time", "urllib", "datetime", "pathlib", "typing", "fcntl",
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                assert root in allowed_roots, f"non-stdlib import: {alias.name}"
        elif isinstance(node, ast.ImportFrom) and node.module:
            root = node.module.split(".")[0]
            assert root in allowed_roots, f"non-stdlib import: {node.module}"
    # And explicitly: no trading package is referenced at runtime.
    assert "import hermes_trader" not in src
    assert "from hermes_trader" not in src


# ── IP fetching: primary / fallback / malformed body ───────────────────────

def test_p3_14_fetch_ip_primary_json(monkeypatch):
    def fake_get(url, timeout=10.0):
        assert url == watch.PRIMARY_URL
        return '{"ip":"203.0.113.7"}'
    monkeypatch.setattr(watch, "_http_get", fake_get)
    assert watch.fetch_ip() == "203.0.113.7"


def test_p3_14_fetch_ip_falls_back_to_secondary(monkeypatch):
    calls = []

    def fake_get(url, timeout=10.0):
        calls.append(url)
        if url == watch.PRIMARY_URL:
            raise OSError("primary down")
        return "198.51.100.42\n"
    monkeypatch.setattr(watch, "_http_get", fake_get)
    assert watch.fetch_ip() == "198.51.100.42"
    assert len(calls) == 2


def test_p3_14_fetch_ip_both_fail_returns_none(monkeypatch):
    def fake_get(url, timeout=10.0):
        raise OSError("network unreachable")
    monkeypatch.setattr(watch, "_http_get", fake_get)
    assert watch.fetch_ip() is None


def test_p3_14_fetch_ip_rejects_captive_portal_body(monkeypatch):
    # A non-IP body from BOTH sources is a failure, not a "new IP".
    def fake_get(url, timeout=10.0):
        return "<html>please login to wifi</html>"
    monkeypatch.setattr(watch, "_http_get", fake_get)
    assert watch.fetch_ip() is None


def test_p3_14_valid_ip_shapes():
    assert watch._valid_ip("203.0.113.7")
    assert watch._valid_ip("2001:db8::1")
    assert not watch._valid_ip("")
    assert not watch._valid_ip("<html>")
    assert not watch._valid_ip("a" * 46)  # over the 45-char length cap


def test_p3_14_first_check_seeds_state_without_event(monkeypatch, tmp_path):
    monkeypatch.setattr(watch, "fetch_ip", lambda: "203.0.113.7")
    state_file = tmp_path / "state.json"
    events_file = tmp_path / "events.jsonl"
    changed = watch.check_once(state_file=state_file, events_file=events_file)
    assert changed is False
    state = json.loads(state_file.read_text())
    assert state["ip"] == "203.0.113.7"
    assert state["source"] == "scripts/ip_drift_watch.py"
    assert not events_file.exists()  # baseline: nothing to compare against


def test_p3_14_unchanged_ip_emits_nothing(monkeypatch, tmp_path):
    state_file = tmp_path / "state.json"
    events_file = tmp_path / "events.jsonl"
    state_file.write_text(json.dumps({"ip": "203.0.113.7"}))
    monkeypatch.setattr(watch, "fetch_ip", lambda: "203.0.113.7")
    changed = watch.check_once(state_file=state_file, events_file=events_file)
    assert changed is False
    assert not events_file.exists()


def test_p3_14_changed_ip_warns_and_appends_chained_event(monkeypatch, tmp_path, caplog):
    state_file = tmp_path / "state.json"
    events_file = tmp_path / "events.jsonl"
    state_file.write_text(json.dumps({"ip": "203.0.113.7"}))
    # All IP resolution must be stubbed — never make real HTTP calls in tests.
    monkeypatch.setattr(watch, "fetch_ip", lambda: "198.51.100.99")

    with caplog.at_level(logging.WARNING, logger="ip_drift_watch"):
        changed = watch.check_once(state_file=state_file, events_file=events_file)

    assert changed is True
    # Warning carries old + new IP.
    drift_logs = [r for r in caplog.records if "egress IP changed" in r.getMessage()]
    assert len(drift_logs) == 1
    msg = drift_logs[0].getMessage()
    assert "203.0.113.7" in msg and "198.51.100.99" in msg
    # State updated.
    assert json.loads(state_file.read_text())["ip"] == "198.51.100.99"

    # Event line is valid JSON with the chained schema.
    lines = [ln for ln in events_file.read_text().splitlines() if ln.strip()]
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["event"] == "ip_drift"
    assert rec["payload"] == {"old_ip": "203.0.113.7", "new_ip": "198.51.100.99"}
    assert rec["seq"] == 1
    assert rec["prev_hash"] == ""
    assert rec["hash"] == watch._record_hash(rec)


def test_p3_14_chained_event_verifies_with_event_log(tmp_path):
    """The watchdog's hash scheme must match hermes_trader.event_log so
    verify_chain() accepts its records."""
    from hermes_trader import event_log

    events_file = tmp_path / "events.jsonl"
    # Seed a genesis record via the real event_log (writing to our path).
    old_events = event_log.EVENTS_FILE
    event_log.EVENTS_FILE = str(events_file)
    try:
        assert event_log.append("risk", {"note": "seed"}) is True
    finally:
        event_log.EVENTS_FILE = old_events

    # ...then a watchdog record links on top of the chain.
    assert watch.append_ip_drift_event("1.1.1.1", "2.2.2.2", path=events_file) is True

    result = event_log.verify_chain(path=str(events_file))
    assert result["ok"] is True
    assert result["chained_records"] == 2
    assert result["last_seq"] == 2


def test_p3_14_failed_check_returns_none_and_is_silent(monkeypatch, tmp_path, caplog):
    state_file = tmp_path / "state.json"
    events_file = tmp_path / "events.jsonl"
    state_file.write_text(json.dumps({"ip": "203.0.113.7"}))
    monkeypatch.setattr(watch, "fetch_ip", lambda: None)

    with caplog.at_level(logging.WARNING, logger="ip_drift_watch"):
        changed = watch.check_once(state_file=state_file, events_file=events_file)

    assert changed is None
    assert caplog.records == []  # single failures stay at debug
    assert json.loads(state_file.read_text())["ip"] == "203.0.113.7"  # untouched


def test_p3_14_failure_warning_only_after_threshold(monkeypatch, tmp_path, caplog):
    """Consecutive failures: no warning until the Nth, then exactly one."""
    monkeypatch.setenv("HERMES_IP_DRIFT_WATCH", "1")
    monkeypatch.setenv("HERMES_IP_DRIFT_CHECK_S", "1")
    monkeypatch.setenv("HERMES_IP_DRIFT_FAIL_WARN_AFTER", "3")
    monkeypatch.setattr(watch, "check_once", lambda **kw: None)

    ticks = {"n": 0}

    def fake_sleep(_s):
        ticks["n"] += 1
        if ticks["n"] >= 4:
            raise KeyboardInterrupt
    monkeypatch.setattr(watch.time, "sleep", fake_sleep)

    with caplog.at_level(logging.WARNING, logger="ip_drift_watch"):
        with pytest.raises(KeyboardInterrupt):
            watch.run_forever()

    fail_warns = [
        r for r in caplog.records
        if "consecutive egress-IP query failures" in r.getMessage()
    ]
    assert len(fail_warns) == 1  # fired once at streak == 3, not again at 4


def test_p3_14_success_resets_failure_streak(monkeypatch, caplog):
    monkeypatch.setenv("HERMES_IP_DRIFT_WATCH", "1")
    monkeypatch.setenv("HERMES_IP_DRIFT_FAIL_WARN_AFTER", "2")
    results = iter([None, False, None, None])
    monkeypatch.setattr(watch, "check_once", lambda **kw: next(results))

    ticks = {"n": 0}

    def stop_after(_s):
        ticks["n"] += 1
        if ticks["n"] >= 4:
            raise KeyboardInterrupt
    monkeypatch.setattr(watch.time, "sleep", stop_after)

    with caplog.at_level(logging.WARNING, logger="ip_drift_watch"):
        with pytest.raises(KeyboardInterrupt):
            watch.run_forever()

    fail_warns = [
        r for r in caplog.records
        if "consecutive egress-IP query failures" in r.getMessage()
    ]
    assert len(fail_warns) == 1  # streak of 2 after the success reset it


def test_p3_14_run_forever_exits_when_disabled(monkeypatch, caplog):
    monkeypatch.delenv("HERMES_IP_DRIFT_WATCH", raising=False)
    with caplog.at_level(logging.INFO, logger="ip_drift_watch"):
        watch.run_forever()  # returns immediately
    assert any("disabled" in r.getMessage() for r in caplog.records)


def test_p3_14_state_roundtrip_and_corruption(monkeypatch, tmp_path):
    state_file = tmp_path / "state.json"
    assert watch.load_state(state_file) == {}
    watch.save_state({"ip": "1.1.1.1", "updated_at": "t0"}, path=state_file)
    assert watch.load_state(state_file)["ip"] == "1.1.1.1"
    # Corrupt file → empty state (never raises).
    state_file.write_text("{not json")
    assert watch.load_state(state_file) == {}
