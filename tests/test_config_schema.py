"""F27: tests for the Pydantic-backed config schema (agents/config_schema.py).

Covers:
  * the model's scalar defaults stay in lock-step with CANONICAL_DEFAULTS
    (a drift sentinel — the model is the single source of truth);
  * validate_config_updates reproduces the strict type/range matrix
    (bool is not a number, strings are not coerced, bounds enforced);
  * strict vs lenient unknown-key handling (write paths vs store callers);
  * POST /api/agent/config now shares the strict contract (D-FCFG-4):
    unknown keys 422 instead of being persisted;
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


# ── POST /api/agent/config write contract (D-FCFG-4: strict) ───────────────

@pytest.fixture()
def server_client(monkeypatch):
    monkeypatch.setenv("HERMES_OPERATOR_TOKEN", _OP_TOKEN)
    from hermes_trader import server
    return TestClient(server.app)


def _auth():
    return {"Authorization": f"Bearer {_OP_TOKEN}"}


def test_agent_config_endpoint_rejects_unknown_keys(server_client):
    """D-FCFG-4 (deep audit 2026-08-28): POST /api/agent/config now applies
    the SAME strict_keys gate as POST /api/dashboard/config. Unknown keys
    are 422'd and never persisted (the lenient deep-merge used to let any
    operator stash arbitrary keys in .agent-config.json)."""
    r = server_client.post(
        "/api/agent/config",
        json={"legacy_schema_custom_key": True},
        headers=_auth(),
    )
    assert r.status_code == 422
    assert "unknown key" in r.text
    assert read_agent_config().get("legacy_schema_custom_key") is None


def test_agent_config_endpoint_rejects_bad_types(server_client):
    r = server_client.post(
        "/api/agent/config",
        json={"leverage": "not-an-int"},
        headers=_auth(),
    )
    assert r.status_code == 422
    # Nothing persisted.
    assert read_agent_config().get("leverage") != "not-an-int"


def test_agent_config_endpoint_none_deletes_key(server_client):
    cfg = read_agent_config()
    cfg["cooldown_min"] = 42
    write_agent_config(cfg, backup=False)
    r = server_client.post(
        "/api/agent/config",
        json={"cooldown_min": None},
        headers=_auth(),
    )
    assert r.status_code == 200, r.text
    # None is a deep-merge deletion marker: the key is dropped from the raw
    # file and read_agent_config falls back to the canonical default (30).
    from hermes_trader.agents.config_store import CANONICAL_DEFAULTS, _read_raw_config
    assert "cooldown_min" not in _read_raw_config()
    assert read_agent_config()["cooldown_min"] == CANONICAL_DEFAULTS["cooldown_min"]


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


# ── D-FCFG-2 (deep audit 2026-08-28): nested risk-block deep validation ────

@pytest.mark.parametrize("block,patch,needle", [
    # Out-of-range leaf: a negative max-loss widens the stop instead of
    # tightening it (the audit's dsl_exit.max_loss_pct=-999 example).
    ("dsl_exit", {"max_loss_pct": -999.0}, "dsl_exit.max_loss_pct"),
    ("dsl_exit", {"atr_stop": {"atr_mult": "1.5x"}},
     "dsl_exit.atr_stop.atr_mult"),
    ("dsl_exit", {"atr_stop": {"floor_pct": 999.0}},
     "dsl_exit.atr_stop.floor_pct"),
    # Unknown nested key: typo / smuggled switch inside a risk block.
    ("dsl_exit", {"atr_stop": {"atrr_mult": 1.5}},
     "dsl_exit.atr_stop.atrr_mult: unknown key"),
    ("dsl_exit", {"regime_aware": {"trend_ride": {"max_loss_pct": 0.5}}},
     "regime_aware.trend_ride.max_loss_pct: unknown key"),
    # phase2_tiers: must be a list of validated objects.
    ("dsl_exit", {"phase2_tiers": {"pct_above_entry": 8.0}},
     "dsl_exit.phase2_tiers: expected list"),
    ("dsl_exit", {"phase2_tiers": [{"pct_above_entry": -5.0,
                                    "retrace_threshold": 0.4}]},
     "dsl_exit.phase2_tiers[0].pct_above_entry"),
    ("dsl_exit", {"phase2_tiers": [{"pct_above_entry": 8.0,
                                    "retrace": 0.4}]},
     "dsl_exit.phase2_tiers[0].retrace: unknown key"),
    # Deeply nested regime leaves.
    ("dsl_exit", {"regime_aware": {"max_loss": {
        "trend": {"max_loss_pct": -1.0}}}},
     "regime_aware.max_loss.trend.max_loss_pct"),
    ("dsl_exit", {"regime_aware": {"max_loss": {
        "non_trend": {"max_loss_roe_pct": "5%"}}}},
     "regime_aware.max_loss.non_trend.max_loss_roe_pct"),
    # Block itself must be an object.
    ("dsl_exit", "not-a-dict", "dsl_exit: expected object"),
    ("dsl_exit", [{"max_loss_pct": 0.4}], "dsl_exit: expected object"),
    # atr_risk_sizing
    ("atr_risk_sizing", {"risk_per_trade_pct": -0.02},
     "atr_risk_sizing.risk_per_trade_pct"),
    ("atr_risk_sizing", {"sizing_basis": "moon_stop"},
     "atr_risk_sizing.sizing_basis"),
    ("atr_risk_sizing", {"sizing_v2_cap_pct": 5.0},
     "atr_risk_sizing.sizing_v2_cap_pct"),
    ("atr_risk_sizing", {"enabled": "yes"},
     "atr_risk_sizing.enabled"),
    ("atr_risk_sizing", {"coin_overrides": {"HYPE": {"sl_floor_pct": -1.5}}},
     "atr_risk_sizing.coin_overrides.HYPE.sl_floor_pct"),
    ("atr_risk_sizing", {"coin_overrides": {"HYPE": 1.5}},
     "atr_risk_sizing.coin_overrides.HYPE: expected object"),
    ("atr_risk_sizing", {"coin_overrides": [1, 2]},
     "atr_risk_sizing.coin_overrides: expected object"),
    ("atr_risk_sizing", {"sneaky_key": True},
     "atr_risk_sizing.sneaky_key: unknown key"),
    # signal_enforcement
    ("signal_enforcement", {"boost_bar_delta": 1.5},
     "signal_enforcement.boost_bar_delta"),
    ("signal_enforcement", {"boost_bar_delta": -1},
     "signal_enforcement.boost_bar_delta"),
    ("signal_enforcement", {"whale_window_min": "15m"},
     "signal_enforcement.whale_window_min"),
    ("signal_enforcement", {"whale_veto_min_usd": -250000},
     "signal_enforcement.whale_veto_min_usd"),
    ("signal_enforcement", {"veto": "true"},
     "signal_enforcement.veto"),
    ("signal_enforcement", {"unknown_gate": True},
     "signal_enforcement.unknown_gate: unknown key"),
])
def test_nested_risk_block_deep_validation_rejects(block, patch, needle):
    """D-FCFG-2: malformed / out-of-range / unknown leaves inside the three
    safety-critical nested blocks are rejected at the patch gate, with a
    dotted path pointing at the offending leaf."""
    errors = validate_config_updates({block: patch}, strict_keys=True)
    assert errors, f"expected rejection for {block}={patch!r}"
    assert any(needle in e for e in errors), (needle, errors)


@pytest.mark.parametrize("block,partial", [
    # Partial patches: a single known leaf is enough (the deep merge fills
    # the rest from canonical defaults).
    ("dsl_exit", {"max_loss_pct": 0.5}),
    ("dsl_exit", {"atr_stop": {"enabled": True, "atr_mult": 2.0}}),
    ("dsl_exit", {"noise_band": {"enabled": True, "atr_mult": 0.8}}),
    ("dsl_exit", {"phase2_tiers": [{"pct_above_entry": 10.0,
                                    "retrace_threshold": 0.5}]}),
    ("dsl_exit", {"regime_aware": {"enabled": False}}),
    ("atr_risk_sizing", {"enabled": False}),
    ("atr_risk_sizing", {"sizing_basis": "dsl_stop"}),
    ("atr_risk_sizing", {"sizing_v2_enabled": True, "sizing_v2_cap_pct": 0.1}),
    # coin_overrides: unknown extension leaves (documented plugin channel,
    # e.g. atr_stop_floor_pct in RISK_OVERHAUL_2026-08-26) are ignored,
    # while the known leaf is still type/range checked.
    ("atr_risk_sizing", {"coin_overrides": {
        "HYPE": {"sl_floor_pct": 1.5, "atr_stop_floor_pct": 1.5}}}),
    ("atr_risk_sizing", {"coin_overrides": {"PURR": {"future_leaf": "x"}}}),
    ("signal_enforcement", {"veto": False}),
    ("signal_enforcement", {"boost_bar_delta": 6}),
    ("signal_enforcement", {"whale_veto_min_usd": 500_000}),
])
def test_nested_risk_block_valid_partial_accepted(block, partial):
    """D-FCFG-2: well-formed partial patches (and documented extension
    leaves) pass — the deep gate must not reject legitimate writes."""
    assert validate_config_updates(
        {block: partial}, strict_keys=True
    ) == [], validate_config_updates({block: partial}, strict_keys=True)


def test_nested_risk_block_canonical_defaults_pass():
    """The shipped canonical defaults for all three blocks satisfy the spec."""
    from hermes_trader.agents.config_store import CANONICAL_DEFAULTS as _CD
    for block in ("dsl_exit", "atr_risk_sizing", "signal_enforcement"):
        assert validate_config_updates(
            {block: _CD[block]}, strict_keys=True
        ) == [], block


def test_agent_config_endpoint_rejects_malformed_nested_block(server_client):
    """D-FCFG-2 end-to-end: a malformed nested leaf is 422'd and never
    reaches .agent-config.json."""
    r = server_client.post(
        "/api/agent/config",
        json={"dsl_exit": {"max_loss_pct": -999.0}},
        headers=_auth(),
    )
    assert r.status_code == 422
    assert "dsl_exit.max_loss_pct" in r.text
    assert read_agent_config()["dsl_exit"]["max_loss_pct"] != -999.0
