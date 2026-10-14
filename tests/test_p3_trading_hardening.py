"""P3 trading-layer hardening tests.

- P3-a: emergency-flatten policy for a LIVE fill that ends with no confirmed
  server-side stop (opt-in, LIVE-only, never SHADOW).
- P3-c: atomic JSON writer strict ``allow_nan=False`` mode (legacy default
  still emits NaN; strict mode raises and leaves no tmp behind).
- config/schema registration of ``flatten_on_sl_missing``.
"""
from __future__ import annotations

import json
import math

import pytest


# ── P3-a emergency flatten policy ──────────────────────────────────────────

def test_emergency_flatten_requires_all_three_conditions():
    from hermes_trader.agents import executor as ex
    f = ex._should_emergency_flatten
    armed = {"flatten_on_sl_missing": True}

    # the one fully-qualified case
    assert f(sl_missing=True, mode="LIVE", config=armed) is True
    # default config (opt-out) → even LIVE+missing does not flatten
    assert f(sl_missing=True, mode="LIVE", config={}) is False
    # SHADOW/paper never flattens even when armed (no real position)
    assert f(sl_missing=True, mode="SHADOW", config=armed) is False
    assert f(sl_missing=True, mode="PAPER", config=armed) is False
    # stop present → nothing to do
    assert f(sl_missing=False, mode="LIVE", config=armed) is False
    # mode is case-insensitive
    assert f(sl_missing=True, mode="live", config=armed) is True


def test_flatten_config_default_is_opt_out_and_registered():
    from hermes_trader.agents.config_defaults import CANONICAL_DEFAULTS
    assert CANONICAL_DEFAULTS["flatten_on_sl_missing"] is False
    # schema model accepts the typed bool and defaults from canonical
    from hermes_trader.agents.config_schema import _ConfigPatch
    m = _ConfigPatch()
    assert m.flatten_on_sl_missing is False


# ── P3-c atomic JSON allow_nan ──────────────────────────────────────────────

def test_atomic_write_strict_nan_raises(tmp_path):
    from hermes_trader.agents import atomic_io
    p = tmp_path / "state.json"
    with pytest.raises(ValueError):
        atomic_io.write_json_atomic(p, {"x": float("nan")}, allow_nan=False)
    # nothing written, no tmp leftover
    assert not p.exists()
    assert list(tmp_path.glob("*.tmp")) == []


def test_atomic_write_strict_infinity_raises(tmp_path):
    from hermes_trader.agents import atomic_io
    with pytest.raises(ValueError):
        atomic_io.write_json_atomic(
            tmp_path / "s.json", {"x": float("inf")}, allow_nan=False)


def test_atomic_write_legacy_allow_nan_default(tmp_path):
    from hermes_trader.agents import atomic_io
    p = tmp_path / "legacy.json"
    # default preserves prior behavior (NaN token written, file readable by
    # the lenient stdlib loader) — must not silently change state writers.
    atomic_io.write_json_atomic(p, {"x": float("nan")})
    assert math.isnan(json.loads(p.read_text())["x"])


def test_locked_write_strict_nan_raises_and_releases_lock(tmp_path):
    from hermes_trader.agents import atomic_io
    p = tmp_path / "locked.json"
    with pytest.raises(ValueError):
        atomic_io.locked_write_json_atomic(
            p, {"x": float("nan")}, allow_nan=False)
    # a second writer can acquire the lock afterward (no leaked LOCK_EX)
    atomic_io.locked_write_json_atomic(p, {"x": 1}, allow_nan=False)
    assert json.loads(p.read_text()) == {"x": 1}
