from __future__ import annotations

import pytest

from hermes_trader.runtime_profile import (
    PROFILE_ENV,
    PROFILE_FULL,
    PROFILE_OFF,
    PROFILE_SHADOW,
    apply_profile_environment,
    profile_config_errors,
    resolve_profile,
)


@pytest.mark.parametrize(
    ("explicit", "env", "expected"),
    [
        (None, {}, PROFILE_FULL),
        ("shadow", {}, PROFILE_SHADOW),
        (None, {PROFILE_ENV: "off"}, PROFILE_OFF),
        ("full", {PROFILE_ENV: "shadow"}, PROFILE_FULL),
        (None, {PROFILE_ENV: "  SHADOW  "}, PROFILE_SHADOW),
    ],
)
def test_resolve_profile(explicit, env, expected):
    assert resolve_profile(explicit, env) == expected


def test_unknown_profile_fails_closed():
    with pytest.raises(ValueError, match="unknown runtime profile"):
        resolve_profile(None, {PROFILE_ENV: "unsafe"})


def test_shadow_environment_revokes_live_authorization():
    env = {"HERMES_ENABLE_LIVE": "1"}
    apply_profile_environment(PROFILE_SHADOW, env)
    assert env[PROFILE_ENV] == PROFILE_SHADOW
    assert env["HERMES_ENABLE_LIVE"] == "0"


def test_full_environment_preserves_legacy_startup():
    env = {"HERMES_ENABLE_LIVE": "1"}
    apply_profile_environment(PROFILE_FULL, env)
    assert env[PROFILE_ENV] == PROFILE_FULL
    assert env["HERMES_ENABLE_LIVE"] == "1"


def _shadow_config() -> dict:
    return {
        "mode": "SHADOW",
        "scan_long_only": True,
        "enable_hip3": False,
        "runner_entry_gate": {"allow_shorts": False},
        "s3_shadow_shorts": {"enabled": False},
    }


def test_shadow_accepts_current_baseline_contract():
    assert profile_config_errors(PROFILE_SHADOW, _shadow_config()) == []


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda cfg: cfg.update(mode="LIVE"), "requires mode=SHADOW"),
        (lambda cfg: cfg.update(scan_long_only=False), "scan_long_only=true"),
        (
            lambda cfg: cfg["runner_entry_gate"].update(allow_shorts=True),
            "allow_shorts=false",
        ),
        (
            lambda cfg: cfg["s3_shadow_shorts"].update(enabled=True),
            "s3_shadow_shorts.enabled=false",
        ),
        (lambda cfg: cfg.update(enable_hip3=True), "enable_hip3=false"),
    ],
)
def test_shadow_rejects_unsafe_or_out_of_scope_config(mutate, message):
    cfg = _shadow_config()
    mutate(cfg)
    errors = profile_config_errors(PROFILE_SHADOW, cfg)
    assert any(message in error for error in errors)


def test_off_requires_off_mode():
    cfg = _shadow_config()
    assert profile_config_errors(PROFILE_OFF, cfg) == [
        "profile=off requires mode=OFF, got SHADOW"
    ]
    cfg["mode"] = "off"
    assert profile_config_errors(PROFILE_OFF, cfg) == []


def test_full_has_no_additional_policy_guard():
    assert profile_config_errors(PROFILE_FULL, {"mode": "LIVE"}) == []
