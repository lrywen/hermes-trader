"""Fail-closed runtime profiles for the trading loop.

The legacy loop launch remains the default (``full``) so existing callers are
unchanged. ``shadow`` is the explicit single-operator profile: it can only
start when the runtime config is SHADOW, LONG-only, HIP-3-disabled and both
short paths are off. The profile also removes process-level LIVE authorization.

This module intentionally has no I/O so startup policy can be unit-tested
without importing the side-effectful ``scripts/trading_loop.py``.
"""

from __future__ import annotations

import os
from typing import Any, Mapping, MutableMapping

PROFILE_ENV = "HERMES_PROFILE"
PROFILE_FULL = "full"
PROFILE_SHADOW = "shadow"
PROFILE_OFF = "off"
VALID_PROFILES = frozenset({PROFILE_FULL, PROFILE_SHADOW, PROFILE_OFF})


def resolve_profile(explicit: str | None = None, env: Mapping[str, str] | None = None) -> str:
    """Resolve an explicit CLI profile over the environment.

    ``full`` is the compatibility default so an old ``trading_loop.py`` launch
    keeps its previous behaviour. An empty environment value is treated as
    unset; an unknown value fails instead of silently weakening the guard.
    """
    source = os.environ if env is None else env
    raw = explicit or source.get(PROFILE_ENV) or PROFILE_FULL
    value = str(raw).strip().lower()
    if value not in VALID_PROFILES:
        allowed = ", ".join(sorted(VALID_PROFILES))
        raise ValueError(f"unknown runtime profile {raw!r}; expected one of: {allowed}")
    return value


def apply_profile_environment(
    profile: str,
    environ: MutableMapping[str, str] | None = None,
) -> None:
    """Record the selected profile and deny LIVE authorization when non-full."""
    value = resolve_profile(profile, {})
    target = os.environ if environ is None else environ
    target[PROFILE_ENV] = value
    if value in {PROFILE_SHADOW, PROFILE_OFF}:
        target["HERMES_ENABLE_LIVE"] = "0"


def _nested(config: Mapping[str, Any], *path: str) -> Any:
    node: Any = config
    for part in path:
        if not isinstance(node, Mapping):
            return None
        node = node.get(part)
    return node


def _as_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "1", "yes", "on"}:
            return True
        if lowered in {"false", "0", "no", "off", ""}:
            return False
    return None


def profile_config_errors(profile: str, config: Mapping[str, Any]) -> list[str]:
    """Return fail-closed startup errors for the selected profile.

    ``full`` keeps the legacy behaviour and imposes no additional policy. The
    existing startup safety, config parity and LIVE acceptance gates still run
    independently.
    """
    selected = resolve_profile(profile, {})
    if selected == PROFILE_FULL:
        return []

    errors: list[str] = []
    mode = str(config.get("mode", "OFF")).strip().upper()
    expected_mode = "SHADOW" if selected == PROFILE_SHADOW else "OFF"
    if mode != expected_mode:
        errors.append(f"profile={selected} requires mode={expected_mode}, got {mode or '?'}")

    if selected == PROFILE_OFF:
        return errors

    long_only = _as_bool(config.get("scan_long_only"))
    if long_only is not True:
        errors.append("profile=shadow requires scan_long_only=true")

    allow_shorts = _as_bool(_nested(config, "runner_entry_gate", "allow_shorts"))
    if allow_shorts is not False:
        errors.append("profile=shadow requires runner_entry_gate.allow_shorts=false")

    short_shadow = _as_bool(_nested(config, "s3_shadow_shorts", "enabled"))
    if short_shadow is not False:
        errors.append("profile=shadow requires s3_shadow_shorts.enabled=false")

    hip3 = _as_bool(config.get("enable_hip3"))
    if hip3 is True:
        errors.append("profile=shadow requires enable_hip3=false")

    return errors
