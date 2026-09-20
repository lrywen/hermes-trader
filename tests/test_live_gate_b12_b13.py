"""B-12/B-13 G3 live-trading acceptance gate tests (2026-09-20).

Covers the boot-time gate in hermes_trader/agents/live_gate.py:
  * B-12: contradictory HERMES_ENABLE_LIVE × mode combinations warn;
  * B-13: mode=LIVE refuses without a valid outcome-A acceptance record,
    SHADOW/OFF always pass, and an invalid/negative-CI record is fatal.
The gate path is redirected via HERMES_LIVE_GATE_FILE so no /data is touched.
"""
from __future__ import annotations

import json

import pytest

from hermes_trader.agents import live_gate


@pytest.fixture
def gate_file(tmp_path, monkeypatch):
    p = tmp_path / "live_acceptance_gate.json"
    monkeypatch.setenv("HERMES_LIVE_GATE_FILE", str(p))
    return p


def _good_record(cfg=None):
    cfg = cfg or {"mode": "LIVE"}
    return {
        "version": 1,
        "decision": "outcome_a_go_live",
        "arms": ["filt"],
        "block_bootstrap_ci": [1.2, 8.4],
        "config_src": "/data/.agent-config.json",
        "config_sha256": live_gate.config_sha256(cfg),
        "operator": "ops@2026-09-20",
        "utc_iso": "2026-09-20T00:00:00Z",
    }


# ---------------- B-12 combination warnings ----------------

def test_b12_env_armed_but_shadow_warns():
    w = live_gate.evaluate_live_combo("SHADOW", env_authorized=True)
    assert len(w) == 1 and "HERMES_ENABLE_LIVE=true" in w[0] and "SHADOW" in w[0]


def test_b12_live_without_env_warns():
    w = live_gate.evaluate_live_combo("LIVE", env_authorized=False)
    assert len(w) == 1 and "P0-1 gate" in w[0]


def test_b12_live_with_env_clean():
    assert live_gate.evaluate_live_combo("LIVE", env_authorized=True) == []


def test_b12_shadow_without_env_clean():
    assert live_gate.evaluate_live_combo("SHADOW", env_authorized=False) == []


# ---------------- B-13 record validation ----------------

def test_b13_good_record_valid():
    assert live_gate.validate_acceptance_record(_good_record()) == []


def test_b13_missing_fields_listed():
    errs = live_gate.validate_acceptance_record({"version": 1})
    missing = {e.split("'")[1] for e in errs if "missing field" in e}
    assert {"decision", "arms", "block_bootstrap_ci", "config_src",
            "config_sha256", "operator", "utc_iso"} <= missing


def test_b13_wrong_decision_rejected():
    rec = _good_record()
    rec["decision"] = "outcome_b_negative"
    errs = live_gate.validate_acceptance_record(rec)
    assert any("outcome_a_go_live" in e for e in errs)


@pytest.mark.parametrize("ci", ([0.0, 5.0], [-2.0, -0.1], [3.0, 1.0], [5.0]))
def test_b13_nonpositive_or_bad_ci_rejected(ci):
    rec = _good_record()
    rec["block_bootstrap_ci"] = ci
    errs = live_gate.validate_acceptance_record(rec)
    assert errs  # lo<=0 / lo>hi / wrong length all fatal


def test_b13_config_hash_mismatch_rejected():
    rec = _good_record({"mode": "LIVE"})
    errs = live_gate.validate_acceptance_record(
        rec, expected_config_sha256="deadbeef")
    assert any("config_sha256" in e for e in errs)


def test_b13_bad_version_rejected():
    rec = _good_record()
    rec["version"] = 99
    assert any("version" in e for e in live_gate.validate_acceptance_record(rec))


# ---------------- startup integration ----------------

def test_shadow_passes_without_record(gate_file):
    fatal, warnings = live_gate.startup_live_gate_errors(
        {"mode": "SHADOW"}, env_authorized=False)
    assert fatal == [] and warnings == []


def test_off_passes_without_record(gate_file):
    fatal, _ = live_gate.startup_live_gate_errors(
        {"mode": "OFF"}, env_authorized=False)
    assert fatal == []


def test_live_without_record_is_fatal(gate_file):
    fatal, _ = live_gate.startup_live_gate_errors(
        {"mode": "LIVE"}, env_authorized=True)
    assert len(fatal) == 1 and "B-13" in fatal[0] and "not found" in fatal[0]


def test_live_with_valid_record_passes(gate_file):
    cfg = {"mode": "LIVE"}
    gate_file.write_text(json.dumps(_good_record(cfg)))
    fatal, warnings = live_gate.startup_live_gate_errors(
        cfg, env_authorized=True)
    assert fatal == [] and warnings == []


def test_live_with_negative_ci_record_is_fatal(gate_file):
    cfg = {"mode": "LIVE"}
    rec = _good_record(cfg)
    rec["block_bootstrap_ci"] = [-16.0, -10.0]  # outcome B → must stay SHADOW
    gate_file.write_text(json.dumps(rec))
    fatal, _ = live_gate.startup_live_gate_errors(cfg, env_authorized=True)
    assert any("strictly > 0" in e for e in fatal)


def test_live_corrupt_record_is_fatal(gate_file):
    gate_file.write_text("{not json")
    fatal, _ = live_gate.startup_live_gate_errors(
        {"mode": "LIVE"}, env_authorized=True)
    assert len(fatal) == 1 and "unreadable" in fatal[0]


def test_config_sha_is_stable():
    a = {"x": 1, "y": [1, 2]}
    b = {"y": [1, 2], "x": 1}  # different key order
    assert live_gate.config_sha256(a) == live_gate.config_sha256(b)
    assert live_gate.config_sha256({"x": 2}) != live_gate.config_sha256(a)


def test_build_helper_roundtrip(gate_file):
    cfg = {"mode": "LIVE"}
    rec = live_gate.build_acceptance_record(
        cfg, arms=["filt"], ci_lo=1.2, ci_hi=8.4,
        config_src="/data/.agent-config.json",
        operator="ops", utc_iso="2026-09-20T00:00:00Z")
    gate_file.write_text(json.dumps(rec))
    fatal, _ = live_gate.startup_live_gate_errors(cfg, env_authorized=True)
    assert fatal == []


@pytest.mark.parametrize("lo,hi", [(0.0, 5.0), (-1.0, 2.0), (5.0, 1.0)])
def test_build_helper_refuses_non_positive_ci(lo, hi):
    with pytest.raises(ValueError, match="SHADOW|positive"):
        live_gate.build_acceptance_record(
            {"mode": "LIVE"}, arms=["filt"], ci_lo=lo, ci_hi=hi,
            config_src="src", operator="ops", utc_iso="t")
