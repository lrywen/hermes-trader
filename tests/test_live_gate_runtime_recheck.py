"""T-02 (DEF-01): runtime B-13 recheck on the LIVE entry path.

The executor re-validates the acceptance record at order time (mode can change
after boot), with a short TTL cache so the hot path stays cheap. These tests
cover the pure cached leaf: non-LIVE is not evaluated, a missing record blocks,
a valid record passes, and repeated calls inside the TTL read the file at most
once. maybe_execute's refusal path asserts no order placement.
"""
from __future__ import annotations

import json

import pytest

from hermes_trader.agents import executor, live_gate


@pytest.fixture
def gate_file(tmp_path, monkeypatch):
    p = tmp_path / "live_acceptance_gate.json"
    monkeypatch.setenv("HERMES_LIVE_GATE_FILE", str(p))
    return p


@pytest.fixture(autouse=True)
def _reset_cache():
    # Force a fresh TTL window for every test.
    cache = executor._live_gate_cache
    cache["checked_at"] = -1e18
    cache["error"] = None
    yield
    cache["checked_at"] = -1e18
    cache["error"] = None


def _good_record(cfg):
    return {
        "version": 1,
        "decision": "outcome_a_go_live",
        "arms": ["filt"],
        "block_bootstrap_ci": [1.2, 8.4],
        "config_src": "/data/.agent-config.json",
        "config_sha256": live_gate.config_sha256(cfg),
        "operator": "ops",
        "utc_iso": "2026-09-24T00:00:00Z",
    }


def test_non_live_not_evaluated(gate_file):
    assert executor._live_gate_runtime_block({"mode": "SHADOW"}) is None
    assert executor._live_gate_runtime_block({"mode": "OFF"}) is None


def test_live_missing_record_blocks(gate_file):
    assert executor._live_gate_runtime_block({"mode": "LIVE"}) == \
        "live_gate_record_invalid"


def test_live_valid_record_passes(gate_file):
    cfg = {"mode": "LIVE"}
    gate_file.write_text(json.dumps(_good_record(cfg)))
    assert executor._live_gate_runtime_block(cfg) is None


def test_ttl_caches_file_read(gate_file, monkeypatch):
    # TTL=0 in this test forces re-read; instead assert caching under normal
    # TTL: spy on load_acceptance_record, 100 calls → one actual load.
    cfg = {"mode": "LIVE"}
    gate_file.write_text(json.dumps(_good_record(cfg)))
    calls = {"n": 0}
    real = live_gate.load_acceptance_record

    def _spy(*a, **k):
        calls["n"] += 1
        return real(*a, **k)

    monkeypatch.setattr(live_gate, "load_acceptance_record", _spy)
    for _ in range(100):
        assert executor._live_gate_runtime_block(cfg) is None
    assert calls["n"] == 1


def test_maybe_execute_blocks_without_order(gate_file, monkeypatch):
    # Force LIVE config with no record; the entry MUST return the block reason
    # and never invoke an exchange order function.
    monkeypatch.setattr(
        executor, "read_agent_config", lambda: {"mode": "LIVE"})
    monkeypatch.setattr(executor, "apply_coin_override",
                        lambda c, _coin: c)
    called = {"n": 0}
    for name in ("place_order", "_place_order", "open_position"):
        if hasattr(executor, name):
            monkeypatch.setattr(
                executor, name,
                lambda *a, **k: called.__setitem__("n", called["n"] + 1))

    analysis = {"id": "a1", "coin": "BTC", "verdict": "LONG",
                "confidence": 0.9}
    result = executor.maybe_execute(analysis)
    assert result["executed"] is False
    assert result["reason"] == "live_gate_record_invalid"
    assert called["n"] == 0
