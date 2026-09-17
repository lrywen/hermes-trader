"""P0-2: cross-process loop-observability heartbeat (feed gap / AI brain).

The trading loop is a separate process from the web process that serves
/metrics, so the loop-side feed-quality and LLM-brain signals must be
published to a small state file on the shared /data volume. This module is
the single-writer half of that channel (the loop flushes once per tick);
metrics.py is the read-only consumer.

Contract:
* Pure observability — note_*()/flush() never raise into the trade path.
* feed gap fraction is computed over a bounded rolling window of cold
  candleSnapshot fetches (each tagged with whether its quality report
  carried a "gaps" issue), so an old outage ages out instead of pinning
  the gauge forever.
* trustworthy is the caller's market-data verdict (market_circuit's
  data_ok for the tick), combined with the rolling gap fraction.
* "Never wrote / source missing" degrades to None on read so the metrics
  layer can emit its explicit sentinel rather than a misleading healthy
  zero.
"""

from __future__ import annotations

import json
import time

import pytest

from hermes_trader.agents import loop_observability_state as obs


@pytest.fixture
def state_path(tmp_path, monkeypatch):
    p = tmp_path / ".loop-observability.state"
    monkeypatch.setattr(obs, "STATE_FILE", str(p))
    obs.reset()
    return p


def _read(path):
    with open(path) as fh:
        return json.load(fh)


def test_read_missing_returns_none(state_path):
    assert obs.read_state() is None


def test_cold_fetch_window_gap_fraction(state_path):
    # 4 cold fetches, one carrying a gaps issue → 0.25 over the window.
    obs.note_cold_fetch(issues=("stale",))
    obs.note_cold_fetch(issues=("gaps",))
    obs.note_cold_fetch(issues=())
    obs.note_cold_fetch(issues=("thin",))
    obs.flush(feed_trustworthy=True)
    data = _read(state_path)
    assert data["cold_fetches"] == 4
    assert data["cold_with_gap"] == 1
    assert data["feed_gap_fraction"] == pytest.approx(0.25)


def test_gap_window_is_bounded_and_ages_out(state_path):
    obs.set_window(4)
    for _ in range(3):
        obs.note_cold_fetch(issues=("gaps",))
    obs.note_cold_fetch(issues=())
    # One more clean fetch evicts the oldest gap sample → 2 gaps / 4.
    obs.note_cold_fetch(issues=())
    obs.flush(feed_trustworthy=True)
    data = _read(state_path)
    assert data["cold_fetches"] == 4
    assert data["cold_with_gap"] == 2
    assert data["feed_gap_fraction"] == pytest.approx(0.5)


def test_empty_window_gap_fraction_is_zero(state_path):
    obs.flush(feed_trustworthy=True)
    data = _read(state_path)
    assert data["cold_fetches"] == 0
    assert data["feed_gap_fraction"] == 0.0


def test_trustworthy_reflects_caller_verdict_and_gap(state_path):
    obs.note_cold_fetch(issues=())
    obs.flush(feed_trustworthy=True)
    assert _read(state_path)["feed_trustworthy"] is True

    obs.note_cold_fetch(issues=())
    obs.flush(feed_trustworthy=False)  # market_circuit reported data missing
    assert _read(state_path)["feed_trustworthy"] is False


def test_ai_brain_success_and_failure(state_path, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    t0 = time.time()
    obs.note_llm_success(t0)
    obs.flush(feed_trustworthy=True)
    data = _read(state_path)
    assert data["ai_brain"]["key_configured"] is True
    assert data["ai_brain"]["circuit_open"] is False
    assert data["ai_brain"]["last_success_ts"] == pytest.approx(t0, abs=1)

    obs.note_llm_failure(time.time(), circuit_open=True)
    obs.flush(feed_trustworthy=True)
    data = _read(state_path)
    assert data["ai_brain"]["circuit_open"] is True
    # last_success_ts survives a later failure.
    assert data["ai_brain"]["last_success_ts"] == pytest.approx(t0, abs=1)


def test_ai_brain_missing_key_not_ready(state_path, monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    obs.note_llm_success(time.time())
    obs.flush(feed_trustworthy=True)
    data = _read(state_path)
    # Even with a recent success, no configured key means not ready.
    assert data["ai_brain"]["key_configured"] is False


def test_ai_brain_never_succeeded_last_success_zero(state_path, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    obs.flush(feed_trustworthy=True)
    data = _read(state_path)
    assert data["ai_brain"]["last_success_ts"] == 0.0
    assert data["ai_brain"]["circuit_open"] is False


def test_flush_writes_timestamp_and_version(state_path):
    before = time.time()
    obs.flush(feed_trustworthy=True)
    data = _read(state_path)
    assert data["version"] == 1
    assert data["ts"] >= before


def test_read_corrupt_returns_none(state_path):
    state_path.write_text("{not json")
    assert obs.read_state() is None


def test_flush_never_raises_on_unwritable_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(obs, "STATE_FILE", "/nonexistent-dir-xyz/.state")
    # Must not raise — observability must never perturb the loop.
    obs.note_cold_fetch(issues=("gaps",))
    obs.flush(feed_trustworthy=False)


def test_reset_clears_accumulators(state_path):
    obs.note_cold_fetch(issues=("gaps",))
    obs.note_llm_success(time.time())
    obs.reset()
    obs.flush(feed_trustworthy=True)
    data = _read(state_path)
    assert data["cold_fetches"] == 0
    assert data["ai_brain"]["last_success_ts"] == 0.0
