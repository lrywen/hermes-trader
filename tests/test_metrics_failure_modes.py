"""P0-2: failure-mode observability gauges refreshed by ``/metrics``.

Contract (see the 2026-09-17 pathiel re-audit §5 P0-2):

* Every new gauge is refreshed from LOCAL state only — cross-process state
  files under the writable data dir, the positions snapshot, in-process
  memory and ``/proc``. A scrape must NEVER issue a network call.
* "Never ran / source missing" is an explicit sentinel sample, never a
  missing sample: a plain Gauge exports 0.0 even when never set, so
  ``absent_over_time()`` / age alerts cannot tell "healthy zero" apart from
  "the source never wrote".
* Each source is guarded independently — one corrupt/unreadable file must
  not blind the other gauges.
"""

from __future__ import annotations

import json
import os
import shutil
import time

import pytest

from prometheus_client.parser import text_string_to_metric_families

from hermes_trader import metrics, positions_snapshot, session_log
from hermes_trader.agents import pullback_gate_state
from hermes_trader.agents.memory import memory


def _samples(body: bytes) -> dict:
    """Parse prometheus text into {(metric_name, label_tuple): value}."""
    out: dict = {}
    for family in text_string_to_metric_families(body.decode("utf-8")):
        for sample in family.samples:
            out[(sample.name, tuple(sorted(sample.labels.items())))] = sample.value
    return out


def _value(samples: dict, name: str, **labels) -> float:
    return samples[(name, tuple(sorted(labels.items())))]


@pytest.fixture
def rendered():
    def _render():
        body, _ = metrics.render_metrics()
        return _samples(body)

    return _render


# ── loop heartbeat (positions snapshot saved_at) ──────────────────────

def test_loop_heartbeat_fresh(rendered, tmp_path, monkeypatch):
    snap = tmp_path / ".positions-snapshot.json"
    monkeypatch.setattr(positions_snapshot, "SNAPSHOT_FILE", str(snap))
    now_ms = int(time.time() * 1000)
    snap.write_text(json.dumps({"version": 1, "saved_at": now_ms,
                                "asset_positions": []}))

    s = rendered()
    ts = _value(s, "hermes_loop_heartbeat_timestamp_seconds")
    assert ts == pytest.approx(time.time(), abs=5)
    assert 0.0 <= _value(s, "hermes_loop_heartbeat_age_seconds") <= 5.0


def test_loop_heartbeat_missing_then_corrupt_sentinel(rendered, tmp_path, monkeypatch):
    snap = tmp_path / ".positions-snapshot.json"
    monkeypatch.setattr(positions_snapshot, "SNAPSHOT_FILE", str(snap))

    s = rendered()
    assert _value(s, "hermes_loop_heartbeat_timestamp_seconds") == 0.0
    assert _value(s, "hermes_loop_heartbeat_age_seconds") == 1e9

    snap.write_text("{not json")
    s = rendered()
    assert _value(s, "hermes_loop_heartbeat_timestamp_seconds") == 0.0
    assert _value(s, "hermes_loop_heartbeat_age_seconds") == 1e9


# ── arm heartbeats (cross-process /data state files) ───────────────────

def test_arm_heartbeats_fresh(rendered, tmp_path, monkeypatch):
    pb = tmp_path / "pb.state"
    ro = tmp_path / "ro.state"
    xs = tmp_path / "xs.state"
    monkeypatch.setattr(pullback_gate_state, "STATE_FILE", str(pb))
    monkeypatch.setenv("HERMES_REGIME_OVERLAY_STATE_FILE", str(ro))
    monkeypatch.setenv("HERMES_XS_REVERSAL_STATE_FILE", str(xs))
    for p in (pb, ro, xs):
        p.write_text(json.dumps({"version": 1, "ts": time.time()}))

    s = rendered()
    for arm in ("pullback_gate", "regime_overlay", "xs_reversal"):
        assert _value(s, "hermes_arm_heartbeat_timestamp_seconds", arm=arm) > 0
        assert 0.0 <= _value(s, "hermes_arm_heartbeat_age_seconds", arm=arm) <= 5.0


def test_arm_heartbeat_one_missing_does_not_blind_others(rendered, tmp_path, monkeypatch):
    pb = tmp_path / "pb.state"
    ro = tmp_path / "ro.state"
    xs = tmp_path / "xs.state"
    monkeypatch.setattr(pullback_gate_state, "STATE_FILE", str(pb))
    monkeypatch.setenv("HERMES_REGIME_OVERLAY_STATE_FILE", str(ro))
    monkeypatch.setenv("HERMES_XS_REVERSAL_STATE_FILE", str(xs))
    pb.write_text(json.dumps({"version": 1, "ts": time.time()}))
    ro.write_text(json.dumps({"version": 1, "ts": time.time()}))
    # xs state deliberately absent; corrupt pb instead in a second pass to
    # prove per-source isolation.
    pb.write_text("garbage")

    s = rendered()
    # corrupt pullback → explicit sentinel on that arm only
    assert _value(s, "hermes_arm_heartbeat_timestamp_seconds", arm="pullback_gate") == 0.0
    assert _value(s, "hermes_arm_heartbeat_age_seconds", arm="pullback_gate") == 1e9
    # missing xs → same sentinel
    assert _value(s, "hermes_arm_heartbeat_age_seconds", arm="xs_reversal") == 1e9
    # regime overlay still fresh
    assert 0.0 <= _value(s, "hermes_arm_heartbeat_age_seconds", arm="regime_overlay") <= 5.0


# ── disk free ──────────────────────────────────────────────────────────

def test_data_disk_free(rendered, tmp_path, monkeypatch):
    monkeypatch.setattr(metrics, "_WRITABLE_DATA_DIR", str(tmp_path))
    s = rendered()
    assert _value(s, "hermes_data_disk_free_bytes") == pytest.approx(
        float(shutil.disk_usage(str(tmp_path)).free), rel=1e-6)


# ── drawdown (memory peakEquity) ───────────────────────────────────────

def test_drawdown_fraction(rendered, monkeypatch):
    monkeypatch.setattr(memory, "get_full_state",
                        lambda: {"equity": 9000.0, "peakEquity": 10000.0})
    s = rendered()
    assert _value(s, "hermes_drawdown_fraction") == pytest.approx(0.1)

    monkeypatch.setattr(memory, "get_full_state",
                        lambda: {"equity": 100.0, "peakEquity": 0.0})
    s = rendered()
    assert _value(s, "hermes_drawdown_fraction") == 0.0


# ── grading age (nightly shadow grader history mtime) ──────────────────

def test_grading_age_never_ran_then_fresh(rendered, tmp_path, monkeypatch):
    monkeypatch.setattr(metrics, "_WRITABLE_DATA_DIR", str(tmp_path))
    s = rendered()
    assert _value(s, "hermes_grading_age_seconds") == 1e6

    hist = tmp_path / "shadow_grade_history.jsonl"
    hist.write_text("{}\n")
    old = time.time() - 3 * 3600
    os.utime(hist, (old, old))
    s = rendered()
    assert _value(s, "hermes_grading_age_seconds") == pytest.approx(3 * 3600, abs=5)


# ── session log bytes (active + rotated gz, never the lock sidecar) ────

def test_session_log_bytes(rendered, tmp_path, monkeypatch):
    base = tmp_path / "session-log.jsonl"
    monkeypatch.setattr(session_log, "SESSION_LOG_FILE", str(base))

    s = rendered()
    assert _value(s, "hermes_session_log_bytes") == 0.0

    base.write_text("x" * 100)
    (tmp_path / "session-log.jsonl.1700000000.gz").write_bytes(b"g" * 50)
    (tmp_path / "session-log.jsonl.lock").write_text("l" * 999)
    s = rendered()
    assert _value(s, "hermes_session_log_bytes") == 150.0


# ── per-process RSS (/proc scan) ───────────────────────────────────────

def _write_fake_proc(root, pid, cmdline, vmrss_kb):
    pdir = root / str(pid)
    pdir.mkdir(parents=True)
    (pdir / "cmdline").write_bytes(b"\x00".join(part.encode() for part in cmdline) + b"\x00")
    (pdir / "status").write_text(
        "Name:\tpython3\nPid:\t{0}\nVmRSS:\t{1} kB\n".format(pid, vmrss_kb))


def test_scan_loop_rss_helper_finds_loop_only(tmp_path):
    _write_fake_proc(tmp_path, "1", ["python3", "-m", "hermes_trader.server"], 12000)
    _write_fake_proc(tmp_path, "42",
                     ["python3", "scripts/trading_loop.py", "--env", "prod"], 42000)
    assert metrics._scan_cmd_rss_bytes(str(tmp_path), "trading_loop") == 42000 * 1024.0
    assert metrics._read_vmrss_bytes(str(tmp_path), 42) == 42000 * 1024


def test_scan_loop_rss_helper_no_match(tmp_path):
    _write_fake_proc(tmp_path, "1", ["python3", "-m", "hermes_trader.server"], 12000)
    assert metrics._scan_cmd_rss_bytes(str(tmp_path), "trading_loop") is None
    assert metrics._read_vmrss_bytes(str(tmp_path), 999) is None


def test_scan_loop_rss_ignores_sh_wrapper_cmdline(tmp_path):
    # Live layout (docker-compose `sh -c` inline command): the wrapper shell's
    # cmdline carries the ENTIRE launch script, including the literal
    # "python3 scripts/trading_loop.py". A naive substring scan matches the
    # ~1MB shell first and reports its RSS instead of the real loop process.
    wrapper = [
        "sh", "-c",
        "python3 -m hermes_trader.server 2>&1 & "
        "python3 scripts/trading_loop.py --env prod 2>&1 & "
        "python3 scripts/scheduler.py 2>&1 &",
    ]
    _write_fake_proc(tmp_path, "7", wrapper, 824)
    _write_fake_proc(tmp_path, "10",
                     ["python3", "-m", "hermes_trader.server"], 12000)
    _write_fake_proc(tmp_path, "11",
                     ["python3", "scripts/trading_loop.py", "--env", "prod"], 98000)
    assert metrics._scan_cmd_rss_bytes(str(tmp_path), "trading_loop") == 98000 * 1024.0


def test_process_rss_gauges(rendered, tmp_path, monkeypatch):
    _write_fake_proc(tmp_path, "self", ["python3", "-m", "hermes_trader.server"], 12000)
    _write_fake_proc(tmp_path, "42",
                     ["python3", "scripts/trading_loop.py", "--env", "prod"], 42000)
    monkeypatch.setattr(metrics, "_PROC_ROOT", str(tmp_path))

    s = rendered()
    assert _value(s, "hermes_process_rss_bytes", role="server") == 12000 * 1024.0
    assert _value(s, "hermes_process_rss_bytes", role="loop") == 42000 * 1024.0


def test_process_rss_loop_absent_sentinel(rendered, tmp_path, monkeypatch):
    _write_fake_proc(tmp_path, "self", ["python3", "-m", "hermes_trader.server"], 12000)
    monkeypatch.setattr(metrics, "_PROC_ROOT", str(tmp_path))

    s = rendered()
    assert _value(s, "hermes_process_rss_bytes", role="server") > 0
    assert _value(s, "hermes_process_rss_bytes", role="loop") == -1.0


# ── network-free scrape contract ───────────────────────────────────────

def test_scrape_never_issues_http(monkeypatch):
    import requests

    def _forbidden(*args, **kwargs):
        raise AssertionError("metrics scrape must not perform any HTTP call")

    monkeypatch.setattr(requests.Session, "request", _forbidden)
    body, content_type = metrics.render_metrics()
    assert b"hermes_loop_heartbeat_age_seconds" in body
    assert content_type  # non-empty content type
