"""P1-4 Phase 1 step 2: register ``atr_risk_sizing.sizing_v2_mode``.

Before Phase 1 the sizing v2 gray mode had no canonical leaf: the file
fallback chain in executor._sizing_v2_config read the block directly and
the only file-side knob was the legacy boolean ``sizing_v2_enabled``
(true -> enforce). Phase 1 migrates the env-held "shadow" value into the
file truth source, so the mode leaf must be registered canonically with
the safe default "off" (same default the accessor falls back to today).

The legacy boolean fallback and the env override are intentionally kept
during Phase 1; removing the boolean is a later-cycle step (ledger §5
Phase 1 step 5).
"""

from hermes_trader.agents.config_store import (
    CANONICAL_DEFAULTS,
    cfg_get,
    read_agent_config,
)

_SIZING_V2_MODES = ("off", "shadow", "enforce")


# ── canonical registration ────────────────────────────────────────────

def test_phase1_sizing_v2_mode_registered_in_canonical_block():
    block = CANONICAL_DEFAULTS["atr_risk_sizing"]
    assert "sizing_v2_mode" in block
    assert block["sizing_v2_mode"] == "off"


def test_phase1_sizing_v2_mode_cfg_get_default():
    assert cfg_get("atr_risk_sizing.sizing_v2_mode", config={}) == "off"


def test_phase1_sizing_v2_mode_visible_in_read_agent_config():
    cfg = read_agent_config()
    assert cfg["atr_risk_sizing"]["sizing_v2_mode"] == "off"


def test_phase1_sizing_v2_mode_file_overlay_wins():
    """File value 'shadow' resolves via cfg_get (the Phase 1 migration
    target) without touching any env."""
    cfg = {"atr_risk_sizing": {"sizing_v2_mode": "shadow"}}
    assert cfg_get("atr_risk_sizing.sizing_v2_mode", config=cfg) == "shadow"


# ── schema: enum accepted on strict updates, invalid rejected ─────────

def test_phase1_sizing_v2_mode_schema_accepts_each_mode():
    from hermes_trader.agents.config_schema import validate_config_updates
    for mode in _SIZING_V2_MODES:
        errors = validate_config_updates(
            {"atr_risk_sizing": {"sizing_v2_mode": mode}},
            strict_keys=True,
        )
        assert errors == [], (mode, errors)


def test_phase1_sizing_v2_mode_schema_rejects_unknown_value():
    from hermes_trader.agents.config_schema import validate_config_updates
    errors = validate_config_updates(
        {"atr_risk_sizing": {"sizing_v2_mode": "banana"}},
        strict_keys=True,
    )
    assert any("sizing_v2_mode" in e for e in errors), errors


# ── schema drift sentinel: _ConfigPatch defaults track canonical ──────

def test_phase1_sizing_v2_mode_patch_default_matches_canonical():
    from hermes_trader.agents.config_schema import _ConfigPatch
    canonical_keys = set(CANONICAL_DEFAULTS["atr_risk_sizing"].keys())
    patch_keys = set(
        _ConfigPatch.model_fields["atr_risk_sizing"]
        .default_factory()
        .keys()
    )
    assert canonical_keys == patch_keys, (
        f"atr_risk_sizing drift: canonical={canonical_keys}, "
        f"patch={patch_keys}"
    )


# ── accessor semantics with env unset: file shadow stays shadow ───────

def test_phase1_accessor_file_shadow_without_env(monkeypatch):
    from hermes_trader.agents import executor
    monkeypatch.delenv("HERMES_SIZING_V2_MODE", raising=False)
    cfg = {"atr_risk_sizing": {"sizing_v2_mode": "shadow"}}
    assert executor._sizing_v2_config(cfg)["mode"] == "shadow"


def test_phase1_accessor_default_off_when_nothing_set(monkeypatch):
    from hermes_trader.agents import executor
    monkeypatch.delenv("HERMES_SIZING_V2_MODE", raising=False)
    assert executor._sizing_v2_config({})["mode"] == "off"
