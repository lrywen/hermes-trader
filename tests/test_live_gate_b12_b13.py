"""B-12/B-13 G3 live-trading acceptance gate tests (2026-09-20).

Covers the boot-time gate in hermes_trader/agents/live_gate.py:
  * B-12: contradictory HERMES_ENABLE_LIVE × mode combinations warn;
  * B-13: mode=LIVE refuses without a valid outcome-A acceptance record,
    SHADOW/OFF always pass, and an invalid/negative-CI record is fatal.
The gate path is redirected via HERMES_LIVE_GATE_FILE so no /data is touched.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from hermes_trader.agents import live_gate

# Fixed "now" for the dated fixtures (issued 2026-09-20, valid to 2031).
NOW = datetime(2026, 10, 15, tzinfo=timezone.utc)


TOKEN = "test-operator-token"


@pytest.fixture
def gate_file(tmp_path, monkeypatch):
    p = tmp_path / "live_acceptance_gate.json"
    monkeypatch.setenv("HERMES_LIVE_GATE_FILE", str(p))
    return p


@pytest.fixture(autouse=True)
def _operator_token(monkeypatch):
    # P4-2: records must verify against an operator-token HMAC.
    monkeypatch.setenv("HERMES_OPERATOR_TOKEN", TOKEN)


def _good_record(cfg=None, *, utc_iso="2026-09-20T00:00:00Z",
                 not_after="2031-09-20T00:00:00Z", sign=True):
    cfg = cfg or {"mode": "LIVE"}
    rec = {
        "version": 2,
        "decision": "outcome_a_go_live",
        "arms": ["filt"],
        "block_bootstrap_ci": [1.2, 8.4],
        "config_src": "/data/.agent-config.json",
        "config_sha256": live_gate.config_sha256(cfg),
        "operator": "ops@2026-09-20",
        "utc_iso": utc_iso,
        "not_after": not_after,
    }
    if sign:
        rec = live_gate.sign_acceptance_record(rec, token=TOKEN)
    return rec


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
    assert live_gate.validate_acceptance_record(_good_record(), now=NOW) == []


def test_b13_missing_fields_listed():
    errs = live_gate.validate_acceptance_record({"version": 1})
    missing = {e.split("'")[1] for e in errs if "missing field" in e}
    assert {"decision", "arms", "block_bootstrap_ci", "config_src",
            "config_sha256", "operator", "utc_iso"} <= missing


def test_b13_wrong_decision_rejected():
    rec = _good_record()
    rec["decision"] = "outcome_b_negative"
    errs = live_gate.validate_acceptance_record(rec, now=NOW)
    assert any("outcome_a_go_live" in e for e in errs)


@pytest.mark.parametrize("ci", ([0.0, 5.0], [-2.0, -0.1], [3.0, 1.0], [5.0]))
def test_b13_nonpositive_or_bad_ci_rejected(ci):
    rec = _good_record()
    rec["block_bootstrap_ci"] = ci
    errs = live_gate.validate_acceptance_record(rec, now=NOW)
    assert errs  # lo<=0 / lo>hi / wrong length all fatal


def test_b13_config_hash_mismatch_rejected():
    rec = _good_record({"mode": "LIVE"})
    errs = live_gate.validate_acceptance_record(
        rec, expected_config_sha256="deadbeef", now=NOW)
    assert any("config_sha256" in e for e in errs)


def test_b13_bad_version_rejected():
    rec = _good_record()
    rec["version"] = 99
    assert any("version" in e for e in live_gate.validate_acceptance_record(rec, now=NOW))


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
        cfg, env_authorized=True, now=NOW)
    assert fatal == [] and warnings == []


def test_live_with_negative_ci_record_is_fatal(gate_file):
    cfg = {"mode": "LIVE"}
    rec = _good_record(cfg)
    rec["block_bootstrap_ci"] = [-16.0, -10.0]  # outcome B → must stay SHADOW
    gate_file.write_text(json.dumps(rec))
    fatal, _ = live_gate.startup_live_gate_errors(cfg, env_authorized=True, now=NOW)
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
    fatal, _ = live_gate.startup_live_gate_errors(cfg, env_authorized=True, now=NOW)
    assert fatal == []


@pytest.mark.parametrize("lo,hi", [(0.0, 5.0), (-1.0, 2.0), (5.0, 1.0)])
def test_build_helper_refuses_non_positive_ci(lo, hi):
    with pytest.raises(ValueError, match="SHADOW|positive"):
        live_gate.build_acceptance_record(
            {"mode": "LIVE"}, arms=["filt"], ci_lo=lo, ci_hi=hi,
            config_src="src", operator="ops", utc_iso="2026-09-20T00:00:00Z")


# ---------------- P4-2: TTL (not_after) + HMAC signature ----------------

def test_p4_record_carries_not_after_and_sig():
    rec = live_gate.build_acceptance_record(
        {"mode": "LIVE"}, arms=["filt"], ci_lo=1.2, ci_hi=8.4,
        config_src="src", operator="ops", utc_iso="2026-09-20T00:00:00Z")
    assert rec["not_after"] == "2026-10-20T00:00:00Z"  # +30d default
    assert rec["sig_algo"] == "hmac-sha256" and len(rec["sig"]) == 64


def test_p4_custom_ttl():
    rec = live_gate.build_acceptance_record(
        {"mode": "LIVE"}, arms=["filt"], ci_lo=1.2, ci_hi=8.4,
        config_src="src", operator="ops", utc_iso="2026-09-20T00:00:00Z",
        ttl_days=7)
    assert rec["not_after"] == "2026-09-27T00:00:00Z"


def test_p4_expired_record_blocked_at_startup_and_runtime(gate_file):
    cfg = {"mode": "LIVE"}
    rec = _good_record(cfg, utc_iso="2026-08-01T00:00:00Z",
                       not_after="2026-08-31T00:00:00Z")
    gate_file.write_text(json.dumps(rec))
    fatal, _ = live_gate.startup_live_gate_errors(
        cfg, env_authorized=True, now=NOW)
    assert any("expired" in e for e in fatal)
    assert live_gate.live_entry_runtime_error(cfg, now=NOW) == "live_gate_record_invalid"


def test_p4_runtime_allows_unexpired(gate_file):
    cfg = {"mode": "LIVE"}
    gate_file.write_text(json.dumps(_good_record(cfg)))
    assert live_gate.live_entry_runtime_error(cfg, now=NOW) is None


def test_p4_tampered_signature_rejected():
    rec = _good_record()
    rec["operator"] = "mallory"  # body changed after signing → HMAC mismatch
    errs = live_gate.validate_acceptance_record(rec, now=NOW)
    assert any("signature" in e for e in errs)


def test_p4_wrong_token_rejected(monkeypatch):
    rec = _good_record()
    errs = live_gate.validate_acceptance_record(
        rec, now=NOW, token="a-different-token")
    assert any("signature" in e for e in errs)


def test_p4_missing_token_env_fails_closed(monkeypatch):
    monkeypatch.delenv("HERMES_OPERATOR_TOKEN", raising=False)
    rec = _good_record()
    errs = live_gate.validate_acceptance_record(rec, now=NOW)
    assert any("signature" in e for e in errs)


def test_p4_unsigned_record_rejected():
    rec = _good_record(sign=False)
    # Unsigned record is missing the 'sig'/'sig_algo' fields -> rejected.
    assert live_gate.validate_acceptance_record(rec, now=NOW)


def test_p4_build_without_token_raises(monkeypatch):
    monkeypatch.delenv("HERMES_OPERATOR_TOKEN", raising=False)
    with pytest.raises(ValueError, match="HERMES_OPERATOR_TOKEN"):
        live_gate.build_acceptance_record(
            {"mode": "LIVE"}, arms=["filt"], ci_lo=1.2, ci_hi=8.4,
            config_src="src", operator="ops", utc_iso="2026-09-20T00:00:00Z")


def test_p4_nonpositive_ttl_rejected():
    with pytest.raises(ValueError, match="ttl_days"):
        live_gate.build_acceptance_record(
            {"mode": "LIVE"}, arms=["filt"], ci_lo=1.2, ci_hi=8.4,
            config_src="src", operator="ops",
            utc_iso="2026-09-20T00:00:00Z", ttl_days=0)


def test_p4_not_after_before_issued_rejected():
    rec = _good_record(utc_iso="2026-09-20T00:00:00Z",
                       not_after="2026-09-10T00:00:00Z")
    errs = live_gate.validate_acceptance_record(rec, now=NOW)
    assert any("later than utc_iso" in e for e in errs)


def test_p4_v1_legacy_record_rejected():
    rec = _good_record()
    rec["version"] = 1
    errs = live_gate.validate_acceptance_record(rec, now=NOW)
    assert any("version" in e for e in errs)
