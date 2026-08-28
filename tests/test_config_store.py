"""Unit tests for the unified config-reading layer (config_store.cfg_get).

Validates:
  * CANONICAL_DEFAULTS parity with .agent-config.json
  * cfg_get resolution order: env > config dict > canonical default > caller default
  * Nested dotted-key lookup (dsl_exit.protect_pct etc.)
  * Environment-variable type coercion (int/float/bool/list)
  * read_agent_config deep-merge (new keys present even when absent on disk)
  * write_agent_config backup / restore_backup round-trip
  * The production values that were once at risk of fallback drift
    (leverage=10, max_daily_loss=-30, daily_giveback_halt_pct=0.35,
    min_short_volume=50M, counter_regime=0.8, dsl_exit.protect_pct=1.25,
    etc.) are returned correctly.
"""

import json
import os

import pytest

from hermes_trader.agents import config_store
from hermes_trader.agents.config_store import (
    CANONICAL_DEFAULTS,
    backup_config,
    cfg_get,
    read_agent_config,
    restore_backup,
    write_agent_config,
    _coerce,
    _deep_merge,
    _env_override,
    _lookup_default,
    _lookup_in_dict,
)


# ── CANONICAL_DEFAULTS integrity ────────────────────────────────────────────

def test_canonical_defaults_contain_all_production_keys():
    """Every key previously at risk of fallback drift must exist with the
    correct production value."""
    expected = {
        "leverage": 10,
        "max_trade_notional_usd": 800,
        "max_concurrent": 10,
        "max_total_notional_pct": 10.0,
        "max_daily_loss_usd": -30,
        "daily_giveback_halt_pct": 0.35,
        "daily_giveback_min_peak_usd": 25.0,
        "min_short_volume_usd": 50_000_000,
        "min_market_volume_usd": 5_000_000,
        "counter_regime_min_conf": 0.8,
        "min_ai_confidence": 0.7,
        "cooldown_min": 30,
        "loss_cooldown_min": 180,
        "min_ai_close_hold_min": 25,
        "force_execute_composite": 30,
        "sl_atr_mult": 1.5,
        "min_trend_score": 0.55,
        "chop_min_conf": 0.75,
        "chop_min_score": 55.0,
        "against_funding_min_conf": 0.85,
        "against_funding_min_score": 60.0,
        "max_atr_pct": 15.0,
        "max_spread_pct": 1.0,
    }
    for key, val in expected.items():
        assert CANONICAL_DEFAULTS[key] == val, (
            f"CANONICAL_DEFAULTS[{key!r}] = {CANONICAL_DEFAULTS[key]!r}, "
            f"expected {val!r}"
        )


def test_canonical_dsl_exit_subkeys():
    dsl = CANONICAL_DEFAULTS["dsl_exit"]
    assert dsl["protect_pct"] == 1.25
    assert dsl["retrace_threshold"] == 0.2
    assert dsl["max_loss_pct"] == 0.4
    assert dsl["max_loss_roe_pct"] == 5.0
    assert dsl["hard_timeout_minutes"] == 1800.0


# ── _deep_merge ─────────────────────────────────────────────────────────────

def test_deep_merge_overrides_scalars():
    base = {"a": 1, "b": 2}
    overlay = {"b": 99}
    merged = _deep_merge(base, overlay)
    assert merged == {"a": 1, "b": 99}


def test_deep_merge_merges_nested_dicts():
    base = {"dsl_exit": {"protect_pct": 1.25, "retrace_threshold": 0.2}}
    overlay = {"dsl_exit": {"protect_pct": 3.0}}
    merged = _deep_merge(base, overlay)
    assert merged["dsl_exit"]["protect_pct"] == 3.0
    assert merged["dsl_exit"]["retrace_threshold"] == 0.2


def test_deep_merge_does_not_mutate_inputs():
    base = {"a": {"x": 1}}
    overlay = {"a": {"y": 2}}
    merged = _deep_merge(base, overlay)
    assert "y" not in base["a"]
    assert merged is not base


# ── _lookup helpers ─────────────────────────────────────────────────────────

def test_lookup_default_nested():
    assert _lookup_default("dsl_exit.protect_pct") == 1.25
    assert _lookup_default("dsl_exit.atr_stop.atr_mult") == 1.5


def test_lookup_default_missing_raises_keyerror():
    with pytest.raises(KeyError):
        _lookup_default("nonexistent.key")


def test_lookup_in_dict_nested():
    d = {"outer": {"inner": 42}}
    assert _lookup_in_dict(d, "outer.inner") == 42


def test_lookup_in_dict_missing_raises_keyerror():
    with pytest.raises(KeyError):
        _lookup_in_dict({}, "x")


# ── _coerce ─────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw,expected", [
    ("1", True), ("true", True), ("yes", True), ("on", True),
    ("0", False), ("false", False), ("no", False), ("off", False),
])
def test_coerce_bool(raw, expected):
    assert _coerce(raw, bool) is expected


def test_coerce_int():
    assert _coerce("12", int) == 12
    assert _coerce("not-an-int", int) == "not-an-int"  # graceful passthrough


def test_coerce_float():
    assert _coerce("1.25", float) == 1.25


def test_coerce_list():
    assert _coerce("TON,TRX,BTC", list) == ["TON", "TRX", "BTC"]
    assert _coerce("", list) == []


# ── cfg_get resolution order ────────────────────────────────────────────────

def test_cfg_get_returns_canonical_default_when_key_absent():
    cfg = {}
    assert cfg_get("leverage", config=cfg) == 10
    assert cfg_get("dsl_exit.protect_pct", config=cfg) == 1.25


def test_cfg_get_prefers_config_dict_value():
    cfg = {"leverage": 25, "dsl_exit": {"protect_pct": 5.0}}
    assert cfg_get("leverage", config=cfg) == 25
    assert cfg_get("dsl_exit.protect_pct", config=cfg) == 5.0


def test_cfg_get_falls_back_to_caller_default_for_unknown_key():
    assert cfg_get("totally_unknown_key", 42, config={}) == 42
    assert cfg_get("totally_unknown_key", config={}) is None


def test_cfg_get_env_override_takes_precedence(monkeypatch):
    monkeypatch.setenv("HERMES_CFG_LEVERAGE", "20")
    assert cfg_get("leverage", config={"leverage": 12}) == 20


def test_cfg_get_env_override_nested_key(monkeypatch):
    monkeypatch.setenv("HERMES_CFG_DSL_EXIT__PROTECT_PCT", "3.5")
    assert cfg_get("dsl_exit.protect_pct", config={}) == 3.5


def test_cfg_get_env_override_coerces_bool(monkeypatch):
    monkeypatch.setenv("HERMES_CFG_SPREAD_GATE_FAIL_OPEN", "true")
    assert cfg_get("spread_gate_fail_open", config={}) is True


def test_cfg_get_env_override_coerces_list(monkeypatch):
    monkeypatch.setenv("HERMES_CFG_COIN_BLOCKLIST", "BTC,ETH")
    result = cfg_get("coin_blocklist", config={})
    assert result == ["BTC", "ETH"]


def test_cfg_get_env_override_unknown_key_returns_string(monkeypatch):
    monkeypatch.setenv("HERMES_CFG_CUSTOM_VAR", "hello")
    assert cfg_get("custom_var", config={}) == "hello"


def test_cfg_get_production_values_no_drift():
    """The exact set of values that previously drifted between modules must
    all resolve to their canonical production values when config is empty."""
    cfg = {}
    assert cfg_get("max_daily_loss_usd", config=cfg) == -30
    assert cfg_get("daily_giveback_halt_pct", config=cfg) == 0.35
    assert cfg_get("daily_giveback_min_peak_usd", config=cfg) == 25.0
    assert cfg_get("min_short_volume_usd", config=cfg) == 50_000_000
    assert cfg_get("counter_regime_min_conf", config=cfg) == 0.8
    assert cfg_get("loss_cooldown_min", config=cfg) == 180
    assert cfg_get("min_ai_close_hold_min", config=cfg) == 25
    assert cfg_get("force_execute_composite", config=cfg) == 30
    assert cfg_get("dsl_exit.max_loss_pct", config=cfg) == 0.4
    assert cfg_get("dsl_exit.max_loss_roe_pct", config=cfg) == 5.0
    assert cfg_get("dsl_exit.hard_timeout_minutes", config=cfg) == 1800.0


# ── read_agent_config ───────────────────────────────────────────────────────

def test_read_agent_config_returns_canonical_when_file_missing():
    # conftest points HERMES_AGENT_CONFIG_FILE at a temp path that doesn't exist
    result = read_agent_config()
    assert result["leverage"] == 10
    assert result["dsl_exit"]["protect_pct"] == 1.25


def test_read_agent_config_deep_merges_disk_values(tmp_path, monkeypatch):
    cfg_file = tmp_path / ".agent-config.json"
    cfg_file.write_text(json.dumps({
        "leverage": 20,
        "dsl_exit": {"protect_pct": 4.0},
    }))
    monkeypatch.setattr(config_store, "CONFIG_PATH", str(cfg_file))
    monkeypatch.setattr(config_store, "_CONFIG_LOCK_PATH", str(cfg_file) + ".lock")

    result = read_agent_config()
    # Overridden values from disk
    assert result["leverage"] == 20
    assert result["dsl_exit"]["protect_pct"] == 4.0
    # Canonical values still present (not clobbered by shallow merge)
    assert result["dsl_exit"]["retrace_threshold"] == 0.2
    assert result["max_concurrent"] == 10


def test_read_agent_config_corrupt_file_falls_back(tmp_path, monkeypatch):
    cfg_file = tmp_path / ".agent-config.json"
    cfg_file.write_text("{ this is not valid json")
    monkeypatch.setattr(config_store, "CONFIG_PATH", str(cfg_file))
    monkeypatch.setattr(config_store, "_CONFIG_LOCK_PATH", str(cfg_file) + ".lock")

    result = read_agent_config()
    assert result["leverage"] == 10  # canonical fallback


# ── write / backup / restore ────────────────────────────────────────────────

def test_write_and_read_roundtrip(tmp_path, monkeypatch):
    cfg_file = tmp_path / ".agent-config.json"
    monkeypatch.setattr(config_store, "CONFIG_PATH", str(cfg_file))
    monkeypatch.setattr(config_store, "_CONFIG_LOCK_PATH", str(cfg_file) + ".lock")
    monkeypatch.setattr(config_store, "_BACKUP_PATH", str(cfg_file) + ".bak")

    write_agent_config({"mode": "TEST", "leverage": 5}, backup=False)
    result = read_agent_config()
    assert result["mode"] == "TEST"
    assert result["leverage"] == 5
    # New keys still merged in
    assert result["max_concurrent"] == 10


def test_backup_created_on_write(tmp_path, monkeypatch):
    cfg_file = tmp_path / ".agent-config.json"
    bak_file = str(cfg_file) + ".bak"
    monkeypatch.setattr(config_store, "CONFIG_PATH", str(cfg_file))
    monkeypatch.setattr(config_store, "_CONFIG_LOCK_PATH", str(cfg_file) + ".lock")
    monkeypatch.setattr(config_store, "_BACKUP_PATH", bak_file)

    write_agent_config({"mode": "FIRST"}, backup=False)
    write_agent_config({"mode": "SECOND"}, backup=True)

    assert os.path.exists(bak_file)
    backed_up = backup_config()
    assert backed_up["mode"] == "FIRST"


def test_restore_backup_roundtrip(tmp_path, monkeypatch):
    cfg_file = tmp_path / ".agent-config.json"
    bak_file = str(cfg_file) + ".bak"
    monkeypatch.setattr(config_store, "CONFIG_PATH", str(cfg_file))
    monkeypatch.setattr(config_store, "_CONFIG_LOCK_PATH", str(cfg_file) + ".lock")
    monkeypatch.setattr(config_store, "_BACKUP_PATH", bak_file)

    write_agent_config({"mode": "ORIGINAL", "leverage": 12}, backup=False)
    write_agent_config({"mode": "CHANGED", "leverage": 99}, backup=True)
    assert read_agent_config()["mode"] == "CHANGED"

    ok = restore_backup()
    assert ok is True
    restored = read_agent_config()
    assert restored["mode"] == "ORIGINAL"
    assert restored["leverage"] == 12


def test_restore_backup_returns_false_when_no_backup(tmp_path, monkeypatch):
    cfg_file = tmp_path / ".agent-config.json"
    monkeypatch.setattr(config_store, "CONFIG_PATH", str(cfg_file))
    monkeypatch.setattr(config_store, "_CONFIG_LOCK_PATH", str(cfg_file) + ".lock")
    monkeypatch.setattr(config_store, "_BACKUP_PATH", str(cfg_file) + ".bak")
    assert restore_backup() is False


def test_restore_snapshot_does_not_self_deadlock(tmp_path, monkeypatch):
    """restore_snapshot must NOT hold the config flock itself: flock is bound to
    the open file description, so a second fd in the same process (opened by
    write_agent_config) blocking on LOCK_EX would self-deadlock forever. The
    restore runs in a worker thread; a 10s join timeout fails the regression."""
    import threading
    from hermes_trader.agents.config_store import restore_snapshot, _snap_path

    cfg_file = tmp_path / ".agent-config.json"
    monkeypatch.setattr(config_store, "CONFIG_PATH", str(cfg_file))
    monkeypatch.setattr(config_store, "_CONFIG_LOCK_PATH", str(cfg_file) + ".lock")
    monkeypatch.setattr(config_store, "_BACKUP_PATH", str(cfg_file) + ".bak")
    monkeypatch.setattr(config_store, "_SNAP_PREFIX", str(cfg_file) + ".snap.")
    monkeypatch.setattr(config_store, "_SNAP_SUFFIX", ".json")

    ts = 1700000123
    # Original config → snapshot file; then a divergent live config.
    write_agent_config({"mode": "ORIGINAL", "leverage": 7}, backup=False)
    snap_path = _snap_path(ts)
    with open(snap_path, "w") as f:
        json.dump(read_agent_config(), f)
    write_agent_config({"mode": "CHANGED", "leverage": 99}, backup=False)
    assert read_agent_config()["mode"] == "CHANGED"

    result = {}
    def _worker():
        result["ok"] = restore_snapshot(ts)

    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    t.join(timeout=10)
    assert not t.is_alive(), "restore_snapshot self-deadlocked on the config flock"
    assert result.get("ok") is True
    restored = read_agent_config()
    assert restored["mode"] == "ORIGINAL"
    assert restored["leverage"] == 7


# ── _env_override key mapping ───────────────────────────────────────────────

def test_env_override_flat_key():
    assert _env_override("leverage") is None  # not set in test env
    os.environ["HERMES_CFG_TEST_FLAT"] = "x"
    try:
        assert _env_override("test_flat") == "x"
    finally:
        del os.environ["HERMES_CFG_TEST_FLAT"]


def test_env_override_nested_key_mapping():
    """dsl_exit.protect_pct -> HERMES_CFG_DSL_EXIT__PROTECT_PCT"""
    os.environ["HERMES_CFG_DSL_EXIT__PROTECT_PCT"] = "9.9"
    try:
        assert _env_override("dsl_exit.protect_pct") == "9.9"
    finally:
        del os.environ["HERMES_CFG_DSL_EXIT__PROTECT_PCT"]
