"""T-01 (DEF-01): every config-write path that arms mode=LIVE MUST pass the
B-13 acceptance gate. update_agent_config() is the single choke point, so a
flip to LIVE without a valid record raises and leaves the on-disk file
byte-level unchanged (INV-01), while a non-LIVE write and a LIVE write with a
valid record both succeed.
"""
from __future__ import annotations

import json

import pytest

from hermes_trader.agents import live_gate
from hermes_trader.agents.config_store import update_agent_config


@pytest.fixture
def gate_file(tmp_path, monkeypatch):
    p = tmp_path / "live_acceptance_gate.json"
    monkeypatch.setenv("HERMES_LIVE_GATE_FILE", str(p))
    return p


def _write_shadow_config():
    # Seed a readable raw config on disk. update_agent_config refuses a missing
    # file, and write_agent_config itself goes through the RMW critical
    # section, so write a minimal raw JSON directly to the configured path.
    from hermes_trader.agents import config_store
    with open(config_store.CONFIG_PATH, "w", encoding="utf-8") as fh:
        json.dump({"mode": "SHADOW"}, fh)


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


def test_live_write_without_record_refuses_and_file_unchanged(
        gate_file, monkeypatch):
    from hermes_trader.agents import config_store
    _write_shadow_config()
    config_path = config_store.CONFIG_PATH
    before = open(config_path, "rb").read()

    with pytest.raises(RuntimeError, match="mode=LIVE|B-13"):
        with update_agent_config(backup=False, via="test_arm") as cfg:
            cfg["mode"] = "LIVE"

    after = open(config_path, "rb").read()
    assert before == after  # INV-01: byte-level unchanged
    # And the readable config is still SHADOW.
    assert config_store.read_agent_config().get("mode") == "SHADOW"


def test_non_live_write_succeeds_without_record(gate_file):
    _write_shadow_config()
    with update_agent_config(backup=False, via="test_off") as cfg:
        cfg["mode"] = "OFF"
    from hermes_trader.agents.config_store import read_agent_config
    assert read_agent_config().get("mode") == "OFF"


def test_live_write_with_valid_record_succeeds(gate_file):
    from hermes_trader.agents.config_store import read_agent_config
    _write_shadow_config()
    # The guard validates against the *resulting* (LIVE) merged config. Build
    # the record from a LIVE config that matches what the write will produce.
    target = dict(read_agent_config())
    target["mode"] = "LIVE"
    gate_file.write_text(json.dumps(_good_record(target)))

    with update_agent_config(backup=False, via="test_arm_ok") as cfg:
        cfg["mode"] = "LIVE"
    assert read_agent_config().get("mode") == "LIVE"
