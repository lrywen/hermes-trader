"""F27: tests for the Pydantic-backed config schema (agents/config_schema.py).

Covers:
  * the model's scalar defaults stay in lock-step with CANONICAL_DEFAULTS
    (a drift sentinel — the model is the single source of truth);
  * validate_config_updates reproduces the strict type/range matrix
    (bool is not a number, strings are not coerced, bounds enforced);
  * strict vs lenient unknown-key handling (web/CLI vs legacy endpoint);
  * the legacy POST /api/agent/config endpoint still persists unknown keys
    but now 422s on type/range errors;
  * coerce_config_value parses bool/null/JSON list/JSON object for the CLI.
"""

import pytest
from fastapi.testclient import TestClient

from hermes_trader.agents.config_schema import (
    _ConfigPatch,
    coerce_config_value,
    validate_config_updates,
)
from hermes_trader.agents.config_store import CANONICAL_DEFAULTS, read_agent_config, write_agent_config

_OP_TOKEN = "test-op-secret-123"


# ── default drift sentinel ─────────────────────────────────────────────────

def test_model_scalar_defaults_match_canonical_defaults():
    """Every model field's default must equal CANONICAL_DEFAULTS — the two
    tables must never silently drift apart."""
    missing = []
    mismatched = []
    for key, default in CANONICAL_DEFAULTS.items():
        field = _ConfigPatch.model_fields.get(key)
        if field is None:
            # _comment can't be a Pydantic field (underscore prefix).
            if key == "_comment":
                continue
            missing.append(key)
            continue
        if field.default_factory is not None:
            # list / dict defaults: factory output must deep-equal canonical.
            built = field.default_factory()
            if built != default:
                mismatched.append((key, built, default))
        elif field.default is not None and field.default != default:
            mismatched.append((key, field.default, default))
    assert not missing, f"CANONICAL_DEFAULTS keys missing from model: {missing}"
    assert not mismatched, f"model defaults drifted: {mismatched}"


# ── strict type / range matrix ─────────────────────────────────────────────

@pytest.mark.parametrize("key,bad_value", [
    # bool is not accepted as int/float
    ("force_execute_composite", True),
    ("leverage", True),
    ("min_ai_confidence", True),
    # strings are not coerced
    ("leverage", "not-an-int"),
    ("whale_size_multiplier", "big"),
    ("enable_crypto", "yes"),
    ("mode", 5),
    # float does not satisfy an int field
    ("research_cooldown_min", 3.5),
    # int does not satisfy a bool field
    ("composite_force_execute", 1),
    ("whale_force_execute", 0),
    # range violations
    ("leverage", 999),
    ("leverage", 0),
    ("max_concurrent", -1),
    ("min_ai_confidence", 1.5),
    ("min_ai_confidence", -0.1),
    ("equity_fraction_per_trade", 0.0),
    ("equity_fraction_per_trade", 1.5),
    ("max_daily_loss_usd", 50.0),
    ("tp_scale_fraction", 1.5),
    ("against_funding_min_score", 150.0),
    ("funding_lookback_hours", 0),
    # wrong container kinds
    ("coin_allowlist", "not-a-list"),
    ("dsl_exit", ["not", "a", "dict"]),
])
def test_strict_validation_rejects(key, bad_value):
    errors = validate_config_updates({key: bad_value}, strict_keys=True)
    assert any(key in e for e in errors), f"{key}={bad_value!r} not rejected: {errors}"


def test_canonical_defaults_all_pass_validation():
    """The seed config must never be self-rejecting."""
    assert validate_config_updates(dict(CANONICAL_DEFAULTS), strict_keys=True) == []


def test_unknown_key_strict_vs_lenient():
    errors = validate_config_updates({"totally_unknown_key": 1}, strict_keys=True)
    assert any("totally_unknown_key" in e for e in errors)
    # Lenient mode (legacy endpoint): unknown keys are ignored, not rejected.
    assert validate_config_updates({"totally_unknown_key": 1}, strict_keys=False) == []
    # ...but a known key with a bad type is still rejected in lenient mode.
    errors = validate_config_updates({"leverage": "nope"}, strict_keys=False)
    assert any("leverage" in e for e in errors)


def test_comment_key_accepted_as_is():
    # _comment is not a model field but must pass validation like before.
    assert validate_config_updates({"_comment": "free-form note"}, strict_keys=True) == []


# ── legacy POST /api/agent/config contract ─────────────────────────────────

@pytest.fixture()
def server_client(monkeypatch):
    monkeypatch.setenv("HERMES_OPERATOR_TOKEN", _OP_TOKEN)
    from hermes_trader import server
    cfg = read_agent_config()
    cfg["legacy_schema_seed"] = 1
    write_agent_config(cfg, backup=False)
    return TestClient(server.app)


def _auth():
    return {"Authorization": f"Bearer {_OP_TOKEN}"}


def test_legacy_endpoint_persists_unknown_keys(server_client):
    r = server_client.post(
        "/api/agent/config",
        json={"legacy_schema_custom_key": True},
        headers=_auth(),
    )
    assert r.status_code == 200, r.text
    assert read_agent_config().get("legacy_schema_custom_key") is True


def test_legacy_endpoint_rejects_bad_types(server_client):
    r = server_client.post(
        "/api/agent/config",
        json={"leverage": "not-an-int"},
        headers=_auth(),
    )
    assert r.status_code == 422
    # Nothing persisted.
    assert read_agent_config().get("leverage") != "not-an-int"


def test_legacy_endpoint_none_deletes_key(server_client):
    cfg = read_agent_config()
    cfg["legacy_schema_none_key"] = 42
    write_agent_config(cfg, backup=False)
    r = server_client.post(
        "/api/agent/config",
        json={"legacy_schema_none_key": None},
        headers=_auth(),
    )
    assert r.status_code == 200, r.text
    assert "legacy_schema_none_key" not in read_agent_config()


# ── CLI / terminal coercion ────────────────────────────────────────────────

@pytest.mark.parametrize("raw,expected", [
    ("true", True),
    ("false", False),
    ("null", None),
    ("none", None),
    ("10", 10),
    ("0.5", 0.5),
    ("[1, 2, 3]", [1, 2, 3]),
    ('{"a": 1}', {"a": 1}),
    ("LIVE", "LIVE"),
])
def test_coerce_config_value(raw, expected):
    assert coerce_config_value(raw) == expected


def test_cli_style_json_list_update_passes_validation(monkeypatch):
    """A CLI `set coin_allowlist='["BTC"]'` must coerce to a list and validate."""
    val = coerce_config_value('["BTC"]')
    assert val == ["BTC"]
    assert validate_config_updates({"coin_allowlist": val}, strict_keys=True) == []
