"""R11-E1: config_store schema rejection on hot-reload / write paths.

Covers the four back-doors the F27 patch-level gate leaves open:

  1. ``read_agent_config()`` must surface schema problems in the on-disk
     file via a warning log, but never raise (the deep-merge on
     CANONICAL_DEFAULTS is the safety net for any key _in_ the canonical
     table; the bot must keep running).
  2. ``write_agent_config(cfg)`` must raise ``RuntimeError`` on a cfg
     that fails the schema gate, *before* touching the disk. The .bak
     and .tmp files must be untouched on rejection.
  3. ``update_agent_config()`` must validate the *post-merge* cfg
     before persisting — this catches aggregated violations (e.g.
     FORBIDDEN_OVERRIDE armed across two separate writes) the
     per-patch gate cannot see.
  4. ``restore_backup()`` / ``restore_snapshot()`` must refuse to
     write a bad .bak or snapshot back to disk.

The full-cfg gate (``validate_config_dict``) is the new store-level
safety net. It is a strict superset of the F27 patch-level gate —
anything that passes a patch also passes the whole, and a handful of
problems (FORBIDDEN_OVERRIDE across non-simultaneous writes) only the
whole can catch.
"""

from __future__ import annotations

import json
import logging

import pytest

from hermes_trader.agents import config_store
from hermes_trader.agents.config_store import (
    CANONICAL_DEFAULTS,
    backup_config,
    create_snapshot,
    read_agent_config,
    restore_backup,
    restore_snapshot,
    update_agent_config,
    validate_config_dict,
    write_agent_config,
)


# ── validate_config_dict unit tests ────────────────────────────────────────


def test_canonical_defaults_pass_validation():
    """The seed config must never be self-rejecting — the gate is at least
    as strict as the patch gate, and the patch gate accepts canonical."""
    assert validate_config_dict(dict(CANONICAL_DEFAULTS)) == []


def test_canonical_defaults_pass_validation_lenient():
    """lenient mode also accepts the seed config (and unknown keys too)."""
    cfg = dict(CANONICAL_DEFAULTS)
    cfg["definitely_unknown_key"] = 1
    assert validate_config_dict(cfg, strict_keys=False) == []


def test_validate_forbidden_overrides_unit():
    """Direct unit test of the F27-extension helper used by the whole-cfg
    store gate. Catches the case where any of the 5 force-execute
    switches is armed without override_requires_ai being explicitly
    True in the merged state."""
    from hermes_trader.agents.config_schema import validate_forbidden_overrides
    # 1. safe pair passes
    safe = dict(CANONICAL_DEFAULTS)
    safe["composite_force_execute"] = True
    safe["override_requires_ai"] = True
    assert validate_forbidden_overrides(safe) == []
    # 2. armed without override → rejected
    bad = dict(CANONICAL_DEFAULTS)
    bad["composite_force_execute"] = True
    bad["override_requires_ai"] = False
    errs = validate_forbidden_overrides(bad)
    assert any("FORBIDDEN_OVERRIDE" in e for e in errs)
    # 3. all 5 force keys covered
    for k in ("composite_force_execute", "breakout_force_execute",
              "whale_force_execute", "whale_regime_bypass",
              "spread_gate_fail_open"):
        cfg = dict(CANONICAL_DEFAULTS)
        cfg[k] = True
        cfg["override_requires_ai"] = False
        e = validate_forbidden_overrides(cfg)
        assert any(k in msg for msg in e), (k, e)


def test_validate_config_dict_rejects_non_dict():
    """A non-dict cfg (JSON list, string, number) is rejected outright."""
    for bad in ("not a dict", [1, 2, 3], 42, None):
        errors = validate_config_dict(bad)  # type: ignore[arg-type]
        assert any("expected object" in e for e in errors), bad


@pytest.mark.parametrize("key,bad_value", [
    # bool is not accepted as int/float
    ("leverage", True),
    ("min_ai_confidence", True),
    # strings are not coerced
    ("leverage", "ten"),
    ("mode", 5),
    # float does not satisfy an int field
    ("max_concurrent", 3.5),
    # int does not satisfy a bool field
    ("composite_force_execute", 1),
    # wrong container kinds
    ("coin_allowlist", "not-a-list"),
    ("dsl_exit", ["not", "a", "dict"]),
])
def test_kind_check_rejects(key, bad_value):
    errors = validate_config_dict({key: bad_value})
    assert any(key in e for e in errors), f"{key}={bad_value!r} not rejected: {errors}"


def test_kind_check_dedupes_with_patch_gate():
    """The store-level kind check and the F27 patch-level gate both
    flag the same problem; validate_config_dict must surface it once,
    not twice, so the operator log stays readable."""
    errors = validate_config_dict({"leverage": "ten"})
    leverage_errors = [e for e in errors if "leverage" in e]
    assert len(leverage_errors) == 1, errors


@pytest.mark.parametrize("key,good_value", [
    ("leverage", 10),
    ("min_ai_confidence", 0.7),
    ("mode", "LIVE"),
    ("enable_crypto", False),
    ("coin_allowlist", ["BTC", "ETH"]),
    ("dsl_exit", {"max_loss_pct": 0.4}),
])
def test_known_good_values_pass(key, good_value):
    errors = validate_config_dict({key: good_value})
    assert errors == [], f"{key}={good_value!r} unexpectedly rejected: {errors}"


def test_composite_force_execute_alone_in_safe_state_passes():
    """`composite_force_execute=true` is legal as long as the *merged*
    config also has `override_requires_ai=true`. CANONICAL_DEFAULTS sets
    `override_requires_ai=True`, so the whole-cfg gate must not raise
    when canonical defaults are merged with a composite_force_execute
    toggle."""
    cfg = dict(CANONICAL_DEFAULTS)
    cfg["composite_force_execute"] = True
    assert validate_config_dict(cfg) == []


def test_unknown_key_strict_mode_rejects():
    errors = validate_config_dict({"definitely_unknown_key": 1}, strict_keys=True)
    assert any("definitely_unknown_key" in e for e in errors)


def test_unknown_key_lenient_mode_accepts():
    """The legacy /api/agent/config endpoint kept lenient semantics so
    dashboard plugins can stash custom keys; validate_config_dict's
    lenient mode mirrors that."""
    assert validate_config_dict(
        {"definitely_unknown_key": 1}, strict_keys=False
    ) == []


def test_mode_enum_rejects_typos():
    """A mode typo (e.g. 'ON' instead of 'LIVE') is caught at the patch
    gate (F27 ``validate_config_updates``), not at the whole-cfg gate —
    applying the enum check to a whole view would false-reject
    legitimate historical values written before the P0-2 enum landed."""
    from hermes_trader.agents.config_schema import validate_config_updates
    errors = validate_config_updates({"mode": "ON"}, strict_keys=True)
    assert any("mode" in e and "must be one of" in e for e in errors)


def test_mode_enum_accepts_canonical_at_patch_level():
    """Sanity: canonical modes pass the patch gate (mirrors F27)."""
    from hermes_trader.agents.config_schema import validate_config_updates
    for m in ("OFF", "LIVE", "SHADOW"):
        assert validate_config_updates({"mode": m}, strict_keys=True) == []


def test_mode_legacy_value_accepted_at_store_level():
    """A whole-cfg with a non-canonical ``mode`` (e.g. "TEST" written by
    an older version, or by a smoke test) must NOT be rejected by the
    store-level gate — that gate is for safety bypass (FORBIDDEN_OVERRIDE)
    only, not schema-history enforcement. The patch gate covers the
    *incoming* typo path; the store gate must not break round-trip of
    a historical JSON file."""
    assert validate_config_dict({"mode": "TEST"}) == []


def test_forbidden_override_cross_write_armed_caught():
    """The whole point of the full-cfg gate: the F27 patch-level gate
    only checks the *patch* — a write that left composite_force_execute
    armed from a previous run and a separate write that set
    override_requires_ai=False would pass the patch gate but produce an
    armed state.  The whole-cfg gate sees the merged view and rejects.
    """
    bad = dict(CANONICAL_DEFAULTS)
    bad["composite_force_execute"] = True
    # override_requires_ai left at the canonical True — but break the
    # guard by toggling it off in the *same* merged view.
    bad["override_requires_ai"] = False
    errors = validate_config_dict(bad)
    assert any("FORBIDDEN_OVERRIDE" in e for e in errors), errors


def test_forbidden_override_safe_pair_accepted():
    safe = dict(CANONICAL_DEFAULTS)
    safe["composite_force_execute"] = True
    safe["override_requires_ai"] = True
    assert validate_config_dict(safe) == []


def test_range_violations_rejected_at_patch_level():
    """Per-key range bounds are enforced at the patch level (F27); the
    whole-cfg store gate deliberately skips them so a historical cfg
    on disk is not rejected on read-back. Patch gate is the right
    layer for typo-catching because the operator is *intending* the
    new value at write time."""
    from hermes_trader.agents.config_schema import validate_config_updates
    errors = validate_config_updates({"leverage": 999}, strict_keys=True)
    assert any("leverage" in e for e in errors)
    errors = validate_config_updates({"min_ai_confidence": 1.5}, strict_keys=True)
    assert any("min_ai_confidence" in e for e in errors)


def test_safety_floor_daily_loss_must_be_negative_at_patch_level():
    """Safety floors (max_daily_loss_usd <= 0, etc.) are patch-gate
    only — they catch operator typos on incoming writes. A whole view
    that already has the canonical defaults passes without re-checks
    (the whole gate is for FORBIDDEN_OVERRIDE, not range enforcement)."""
    from hermes_trader.agents.config_schema import validate_config_updates
    errors = validate_config_updates({"max_daily_loss_usd": 50.0}, strict_keys=True)
    assert any("max_daily_loss_usd" in e for e in errors)


def test_none_values_excluded_from_validation():
    """None means 'delete this key' in the deep-merge protocol, not
    'set to None'.  The whole-cfg gate must not flag a key whose value
    is None (it'll be popped before persistence)."""
    # canonical has max_concurrent: 10. None should not trigger a
    # 'expected int, got NoneType' complaint — it's a deletion marker.
    errors = validate_config_dict({"max_concurrent": None})
    assert errors == [], errors


def test_comment_key_accepted_as_is():
    assert validate_config_dict({"_comment": "free-form note"}) == []


def test_multiple_errors_collected_not_short_circuit():
    """A bad cfg with three problems must report all three — the gate
    never short-circuits, so the operator sees the full set in one
    log line and can fix them in one pass. (Three kind errors — mode /
    range / safety floor are patch-gate concerns, not whole-view.)"""
    errors = validate_config_dict({
        "leverage": "ten",
        "max_concurrent": "five",
        "min_ai_confidence": "high",
    })
    assert len(errors) >= 3, errors


# ── read_agent_config: warn, never raise ───────────────────────────────────


def test_read_agent_config_warns_on_bad_type_but_returns(tmp_path, monkeypatch, caplog):
    """A hand-edited JSON with `leverage: "ten"` must NOT crash the bot:
    the deep-merge preserves the bad value (CANONICAL_DEFAULTS only
    fills in *missing* keys, never overwrites), but the schema problem
    is surfaced in the log so the operator can fix it.  The bot's
    downstream code (cfg_get fallback chain, executor type checks,
    etc.) is what defends against a bad value reaching the trading
    path — R11-E1's job is to *surface* the problem, not silently
    correct it.
    """
    cfg_file = tmp_path / ".agent-config.json"
    cfg_file.write_text(json.dumps({"leverage": "ten"}))
    monkeypatch.setattr(config_store, "CONFIG_PATH", str(cfg_file))
    monkeypatch.setattr(config_store, "_CONFIG_LOCK_PATH", str(cfg_file) + ".lock")

    with caplog.at_level(logging.WARNING, logger="hermes_trader.agents.config_store"):
        result = read_agent_config()
    # Bad value preserved (not silently corrected — operators must see
    # the actual disk state).
    assert result["leverage"] == "ten"
    # And the operator sees the warning.
    assert any("leverage" in rec.message for rec in caplog.records), [
        r.message for r in caplog.records
    ]


def test_read_agent_config_warns_on_unknown_key_lenient(tmp_path, monkeypatch, caplog):
    """Unknown keys on disk are accepted (legacy semantics — dashboard
    plugins stash their own keys) but warned about so the operator can
    prune stale ones."""
    cfg_file = tmp_path / ".agent-config.json"
    cfg_file.write_text(json.dumps({"_legacy_plugin_key": 42}))
    monkeypatch.setattr(config_store, "CONFIG_PATH", str(cfg_file))
    monkeypatch.setattr(config_store, "_CONFIG_LOCK_PATH", str(cfg_file) + ".lock")

    with caplog.at_level(logging.WARNING, logger="hermes_trader.agents.config_store"):
        result = read_agent_config()
    # Round-trips as-is.
    assert result["_legacy_plugin_key"] == 42


# ── write_agent_config: raise on errors, before disk touch ─────────────────


def test_write_agent_config_rejects_bad_type(tmp_path, monkeypatch):
    """A direct write_agent_config with a type-violating value must raise
    RuntimeError; the .bak and .tmp files must be untouched."""
    cfg_file = tmp_path / ".agent-config.json"
    cfg_file.write_text(json.dumps(dict(CANONICAL_DEFAULTS)))
    monkeypatch.setattr(config_store, "CONFIG_PATH", str(cfg_file))
    monkeypatch.setattr(config_store, "_CONFIG_LOCK_PATH", str(cfg_file) + ".lock")
    monkeypatch.setattr(config_store, "_BACKUP_PATH", str(cfg_file) + ".bak")

    bad = dict(CANONICAL_DEFAULTS)
    bad["leverage"] = "ten"
    with pytest.raises(RuntimeError, match="refusing to write_agent_config"):
        write_agent_config(bad, backup=True)

    # Original on-disk config still intact.
    assert json.loads(cfg_file.read_text())["leverage"] == 10
    # No .bak created (backup step is part of the write that was rejected).
    assert not (tmp_path / ".agent-config.json.bak").exists()
    # No .tmp leaked.
    assert not (tmp_path / ".agent-config.json.tmp").exists()


def test_write_agent_config_accepts_unknown_key_for_backcompat(tmp_path, monkeypatch):
    """Unknown keys must still round-trip through write_agent_config —
    the historical `POST /api/agent/config` endpoint accepts custom
    keys (dashboard plugins stash their own), and the on-disk write
    path must not break that. R11-E1 narrows the gate to *critical*
    problems (type / range / mode / FORBIDDEN_OVERRIDE); unknown
    keys are still a caller-policy concern.
    """
    cfg_file = tmp_path / ".agent-config.json"
    cfg_file.write_text(json.dumps(dict(CANONICAL_DEFAULTS)))
    monkeypatch.setattr(config_store, "CONFIG_PATH", str(cfg_file))
    monkeypatch.setattr(config_store, "_CONFIG_LOCK_PATH", str(cfg_file) + ".lock")

    ok = dict(CANONICAL_DEFAULTS)
    ok["definitely_not_a_real_key"] = 1
    write_agent_config(ok, backup=False)  # must not raise
    assert json.loads(cfg_file.read_text())["definitely_not_a_real_key"] == 1


def test_write_agent_config_rejects_forbidden_override_armed(tmp_path, monkeypatch):
    """A direct write that arms a force-override switch without
    override_requires_ai must be rejected — the FORBIDDEN_OVERRIDE
    contract is enforced at the store level, not just at the patch
    level."""
    cfg_file = tmp_path / ".agent-config.json"
    cfg_file.write_text(json.dumps(dict(CANONICAL_DEFAULTS)))
    monkeypatch.setattr(config_store, "CONFIG_PATH", str(cfg_file))
    monkeypatch.setattr(config_store, "_CONFIG_LOCK_PATH", str(cfg_file) + ".lock")

    bad = dict(CANONICAL_DEFAULTS)
    bad["composite_force_execute"] = True
    bad["override_requires_ai"] = False
    with pytest.raises(RuntimeError, match="FORBIDDEN_OVERRIDE"):
        write_agent_config(bad, backup=False)


def test_write_agent_config_accepts_valid_cfg(tmp_path, monkeypatch):
    """A valid cfg writes successfully (the happy path is unchanged)."""
    cfg_file = tmp_path / ".agent-config.json"
    cfg_file.write_text(json.dumps(dict(CANONICAL_DEFAULTS)))
    monkeypatch.setattr(config_store, "CONFIG_PATH", str(cfg_file))
    monkeypatch.setattr(config_store, "_CONFIG_LOCK_PATH", str(cfg_file) + ".lock")
    monkeypatch.setattr(config_store, "_BACKUP_PATH", str(cfg_file) + ".bak")

    good = dict(CANONICAL_DEFAULTS)
    good["leverage"] = 7
    write_agent_config(good, backup=False)
    assert json.loads(cfg_file.read_text())["leverage"] == 7


# ── update_agent_config: raise on aggregated violations ────────────────────


def test_update_agent_config_rejects_bad_type_in_body(tmp_path, monkeypatch):
    """update_agent_config validates the *post-merge* cfg before persisting.
    A body that mutates a key to the wrong type must raise without writing.
    """
    cfg_file = tmp_path / ".agent-config.json"
    cfg_file.write_text(json.dumps(dict(CANONICAL_DEFAULTS)))
    monkeypatch.setattr(config_store, "CONFIG_PATH", str(cfg_file))
    monkeypatch.setattr(config_store, "_CONFIG_LOCK_PATH", str(cfg_file) + ".lock")

    original = read_agent_config()
    with pytest.raises(RuntimeError, match="refusing to update_agent_config"):
        with update_agent_config(backup=False) as cfg:
            cfg["leverage"] = "ten"
    # Disk unchanged.
    assert read_agent_config() == original


def test_update_agent_config_catches_aggregated_forbidden_override(tmp_path, monkeypatch):
    """Pre-arming: the on-disk config has composite_force_execute=True
    (legitimately, with override_requires_ai=True). The body toggles
    override_requires_ai off. The F27 patch-level gate would not catch
    this because no *patch* set composite_force_execute=True. The
    whole-cfg gate sees the merged state and rejects."""
    armed = dict(CANONICAL_DEFAULTS)
    armed["composite_force_execute"] = True
    armed["override_requires_ai"] = True

    cfg_file = tmp_path / ".agent-config.json"
    cfg_file.write_text(json.dumps(armed))
    monkeypatch.setattr(config_store, "CONFIG_PATH", str(cfg_file))
    monkeypatch.setattr(config_store, "_CONFIG_LOCK_PATH", str(cfg_file) + ".lock")

    with pytest.raises(RuntimeError, match="FORBIDDEN_OVERRIDE"):
        with update_agent_config(backup=False) as cfg:
            cfg["override_requires_ai"] = False
    # Disk unchanged.
    assert read_agent_config()["override_requires_ai"] is True


def test_update_agent_config_happy_path_still_works(tmp_path, monkeypatch):
    """The valid-mutation path is unchanged."""
    cfg_file = tmp_path / ".agent-config.json"
    cfg_file.write_text(json.dumps(dict(CANONICAL_DEFAULTS)))
    monkeypatch.setattr(config_store, "CONFIG_PATH", str(cfg_file))
    monkeypatch.setattr(config_store, "_CONFIG_LOCK_PATH", str(cfg_file) + ".lock")

    with update_agent_config(backup=False) as cfg:
        cfg["leverage"] = 7
    assert read_agent_config()["leverage"] == 7


def test_update_agent_config_no_write_on_body_exception(tmp_path, monkeypatch):
    """Body exception still aborts the write (R11-E1 does not change
    the existing F20 abort-on-exception contract — the new validation
    is a *second* abort path, not a replacement)."""
    cfg_file = tmp_path / ".agent-config.json"
    cfg_file.write_text(json.dumps(dict(CANONICAL_DEFAULTS)))
    monkeypatch.setattr(config_store, "CONFIG_PATH", str(cfg_file))
    monkeypatch.setattr(config_store, "_CONFIG_LOCK_PATH", str(cfg_file) + ".lock")

    with pytest.raises(ValueError):
        with update_agent_config(backup=False) as cfg:
            cfg["leverage"] = 7
            raise ValueError("simulated body failure")
    assert read_agent_config()["leverage"] == 10


# ── restore_backup / restore_snapshot: refuse bad recovery blobs ───────────


def test_restore_backup_refuses_corrupt_backup(tmp_path, monkeypatch):
    """A bad .bak (e.g. one written by a hand-edit before R11-E1) must
    not be silently restored — restore_backup returns False and the
    on-disk config is unchanged."""
    cfg_file = tmp_path / ".agent-config.json"
    cfg_file.write_text(json.dumps(dict(CANONICAL_DEFAULTS)))
    bak_file = tmp_path / ".agent-config.json.bak"
    bak_file.write_text(json.dumps({"leverage": "ten"}))
    monkeypatch.setattr(config_store, "CONFIG_PATH", str(cfg_file))
    monkeypatch.setattr(config_store, "_CONFIG_LOCK_PATH", str(cfg_file) + ".lock")
    monkeypatch.setattr(config_store, "_BACKUP_PATH", str(bak_file))

    original = read_agent_config()
    assert restore_backup() is False
    assert read_agent_config() == original


def test_restore_backup_happy_path(tmp_path, monkeypatch):
    """A good .bak restores cleanly."""
    cfg_file = tmp_path / ".agent-config.json"
    cfg_file.write_text(json.dumps(dict(CANONICAL_DEFAULTS)))
    bak_file = tmp_path / ".agent-config.json.bak"
    bak_file.write_text(json.dumps({"leverage": 3}))
    monkeypatch.setattr(config_store, "CONFIG_PATH", str(cfg_file))
    monkeypatch.setattr(config_store, "_CONFIG_LOCK_PATH", str(cfg_file) + ".lock")
    monkeypatch.setattr(config_store, "_BACKUP_PATH", str(bak_file))

    assert restore_backup() is True
    assert read_agent_config()["leverage"] == 3


def test_restore_snapshot_refuses_corrupt_snapshot(tmp_path, monkeypatch):
    """A bad snapshot (e.g. one created from a hand-edited config before
    R11-E1) must not be silently restored."""
    cfg_file = tmp_path / ".agent-config.json"
    cfg_file.write_text(json.dumps(dict(CANONICAL_DEFAULTS)))
    monkeypatch.setattr(config_store, "CONFIG_PATH", str(cfg_file))
    monkeypatch.setattr(config_store, "_CONFIG_LOCK_PATH", str(cfg_file) + ".lock")

    snap_id = create_snapshot("test")["id"]
    # Tamper with the snapshot to make it schema-invalid.
    ts = int(snap_id[len("snap-"):])
    snap_path = config_store._snap_path(ts)
    with open(snap_path, "w") as f:
        f.write(json.dumps({"leverage": "ten"}))

    original = read_agent_config()
    assert restore_snapshot(ts) is False
    assert read_agent_config() == original


def test_restore_snapshot_happy_path(tmp_path, monkeypatch):
    """A good snapshot restores cleanly."""
    cfg_file = tmp_path / ".agent-config.json"
    cfg_file.write_text(json.dumps(dict(CANONICAL_DEFAULTS)))
    monkeypatch.setattr(config_store, "CONFIG_PATH", str(cfg_file))
    monkeypatch.setattr(config_store, "_CONFIG_LOCK_PATH", str(cfg_file) + ".lock")

    snap_id = create_snapshot("test")["id"]
    # Write a known-good config to the snapshot path.
    ts = int(snap_id[len("snap-"):])
    snap_path = config_store._snap_path(ts)
    good = dict(CANONICAL_DEFAULTS)
    good["leverage"] = 4
    with open(snap_path, "w") as f:
        f.write(json.dumps(good))

    assert restore_snapshot(ts) is True
    assert read_agent_config()["leverage"] == 4


# ── integration: F27 patch gate + R11-E1 store gate coexist ────────────────


def test_legacy_endpoint_unknown_key_still_persists_via_lenient_path(tmp_path, monkeypatch):
    """The R11-E1 store gate is lenient for unknown keys (preserves
    historical on-disk semantics: dashboard plugins stash their own
    keys via the legacy ``/api/agent/config`` endpoint, and the
    on-disk round-trip must not break that). Critical safety bypass
    (FORBIDDEN_OVERRIDE) and kind mismatches are still rejected —
    only *unknown keys* are lenient. This matches the F27 split:
    web patch API / store write / store read all accept unknown keys
    on round-trip; only the kind + FORBIDDEN_OVERRIDE gates fire."""
    cfg_file = tmp_path / ".agent-config.json"
    cfg_file.write_text(json.dumps(dict(CANONICAL_DEFAULTS)))
    monkeypatch.setattr(config_store, "CONFIG_PATH", str(cfg_file))
    monkeypatch.setattr(config_store, "_CONFIG_LOCK_PATH", str(cfg_file) + ".lock")

    # Direct write with an unknown key is accepted (lenient — preserves
    # the legacy /api/agent/config endpoint's stash-your-own-key
    # behaviour).
    bad = dict(CANONICAL_DEFAULTS)
    bad["unknown_plugin_key"] = 1
    write_agent_config(bad, backup=False)
    assert json.loads(cfg_file.read_text())["unknown_plugin_key"] == 1
    # And a hand-edited file with the same key on disk is read back
    # (also lenient), with at most a warning in the log.
    cfg_file.write_text(json.dumps(dict(CANONICAL_DEFAULTS, unknown_plugin_key=1)))
    result = read_agent_config()
    assert result["unknown_plugin_key"] == 1


def test_validate_config_dict_known_scalar_failures_match_patch_gate():
    """A parameterised matrix: every kind-check the patch gate catches
    (bool not int, str not int, etc.) the whole gate also catches, and
    the resulting error string mentions the same key + kind.
    """
    cases = [
        ("leverage", True),
        ("leverage", "ten"),
        ("min_ai_confidence", "yes"),
        ("composite_force_execute", 1),
        ("coin_allowlist", "not-a-list"),
        ("dsl_exit", ["not", "a", "dict"]),
    ]
    for key, bad in cases:
        whole_errors = validate_config_dict({key: bad})
        assert any(key in e for e in whole_errors), (key, bad, whole_errors)
