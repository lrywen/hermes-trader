"""P0 safety hardening (2026-09-12):

P0-1 — explicit LIVE authorization gate
  * config_store.live_trading_authorized() truth table on HERMES_ENABLE_LIVE
    (fail-closed: only 1/true/yes/on, case-insensitive, authorize)
  * executor.maybe_execute() denies LIVE entries with reason
    "live_not_authorized" before ANY order surface is touched, and with the
    env grant set the request proceeds PAST the gate (proved by reaching a
    later, well-known fail-closed branch)
  * SHADOW mode is unaffected without the env grant
  * the manual POST /api/hl/place-order route returns 409 for LIVE without
    the grant and passes that check once granted

P0-2 — effective-config visibility
  * build_effective_config_snapshot() resolves every canonical leaf and
    labels provenance cfg_env / file / default
  * write_effective_config_snapshot() atomically persists the snapshot,
    honors HERMES_EFFECTIVE_CONFIG_SNAPSHOT (empty disables), and never
    raises when the write fails (best-effort startup step)
"""

from __future__ import annotations

import json

import pytest

from hermes_trader.agents import config_store, executor


# ── P0-1: live_trading_authorized truth table ──────────────────────────────

@pytest.mark.parametrize("value", ["1", "true", "TRUE", "True", "yes", "on",
                                   "  true  "])
def test_p01_live_authorized_truthy(monkeypatch, value):
    monkeypatch.setenv("HERMES_ENABLE_LIVE", value)
    assert config_store.live_trading_authorized() is True


@pytest.mark.parametrize("value", ["", "0", "false", "no", "off", "2",
                                   "please", " true x"])
def test_p01_live_authorized_falsy(monkeypatch, value):
    monkeypatch.setenv("HERMES_ENABLE_LIVE", value)
    assert config_store.live_trading_authorized() is False


def test_p01_live_authorized_absent_fails_closed(monkeypatch):
    monkeypatch.delenv("HERMES_ENABLE_LIVE", raising=False)
    assert config_store.live_trading_authorized() is False


# ── P0-1: maybe_execute gate (autonomous + MCP entry funnel) ───────────────

class _DeniedOrder(Exception):
    pass


def _minimal_live_wire(monkeypatch):
    """Wire just enough for maybe_execute to run through its early gates.

    The P0-1 check happens immediately after the OFF check, before memory /
    gate / sizing I/O, so the deny path needs only a config stub. For the
    "granted" control case we make get_max_leverage raise — an existing
    fail-closed branch well AFTER the P0-1 gate — proving the request passed
    the gate without needing the full order-placement harness.
    """
    monkeypatch.setattr(executor, "read_agent_config",
                        lambda: {"mode": "LIVE"})
    # apply_coin_override on a plain dict without coin_overrides is a no-op
    # merge; leave the real function in place.
    monkeypatch.setattr(executor, "get_max_leverage",
                        lambda _c: (_ for _ in ()).throw(
                            ValueError("Unknown coin P0TEST")))

    def _forbid_order(*_a, **_k):
        raise _DeniedOrder("place_hl_order must never be called here")

    monkeypatch.setattr(executor, "place_hl_order", _forbid_order)


def _analysis():
    return {
        "id": "p0live", "coin": "P0TEST", "action": "LONG", "side": "long",
        "confidence": 0.9, "composite_score": 80,
        "entry_px": 100.0, "stop_px": 99.0, "tp_px": 110.0,
        "reasoning": "p0 live gate test",
    }


def test_p01_maybe_execute_denies_live_without_env(monkeypatch):
    monkeypatch.delenv("HERMES_ENABLE_LIVE", raising=False)
    _minimal_live_wire(monkeypatch)
    res = executor.maybe_execute(_analysis())
    assert res["executed"] is False
    assert res["mode"] == "LIVE"
    assert res["reason"] == "live_not_authorized"


def test_p01_maybe_execute_granted_passes_gate(monkeypatch):
    """With the grant the request must pass the P0-1 gate; it then reaches
    the unknown-leverage fail-closed branch (which sits strictly later)."""
    monkeypatch.setenv("HERMES_ENABLE_LIVE", "true")
    _minimal_live_wire(monkeypatch)
    res = executor.maybe_execute(_analysis())
    assert res["executed"] is False
    assert res["reason"] == "unknown_max_leverage_P0TEST"


def test_p01_maybe_execute_shadow_unaffected_without_env(monkeypatch):
    """SHADOW/paper entries never consult the LIVE grant: deny must not fire
    in SHADOW even with the env var absent."""
    monkeypatch.delenv("HERMES_ENABLE_LIVE", raising=False)
    monkeypatch.setattr(executor, "read_agent_config",
                        lambda: {"mode": "SHADOW"})
    monkeypatch.setattr(executor, "get_max_leverage",
                        lambda _c: (_ for _ in ()).throw(
                            ValueError("Unknown coin P0TEST")))
    res = executor.maybe_execute(_analysis())
    assert res["reason"] == "unknown_max_leverage_P0TEST"


def test_p01_maybe_execute_off_still_blocks(monkeypatch):
    """OFF must short-circuit before the P0-1 check (unchanged behavior)."""
    monkeypatch.delenv("HERMES_ENABLE_LIVE", raising=False)
    monkeypatch.setattr(executor, "read_agent_config",
                        lambda: {"mode": "OFF"})
    res = executor.maybe_execute(_analysis())
    assert res["reason"] == "mode_off"


# ── P0-1: manual route guard ───────────────────────────────────────────────

_OP_TOKEN = "test-op-secret-p0"


def _manual_client(monkeypatch):
    from fastapi.testclient import TestClient

    from hermes_trader import server as srv

    monkeypatch.setenv("HERMES_OPERATOR_TOKEN", _OP_TOKEN)
    monkeypatch.setattr(srv, "read_agent_config",
                        lambda: {"mode": "LIVE"})
    return TestClient(srv.app, raise_server_exceptions=False), srv


def _post(client, coin="BTC", side="long"):
    return client.post(
        "/api/hl/place-order",
        json={"coin": coin, "side": side, "leverage": 5, "riskUSD": 100.0},
        headers={"Authorization": f"Bearer {_OP_TOKEN}"})


def test_p01_manual_route_blocks_live_without_env(monkeypatch):
    monkeypatch.delenv("HERMES_ENABLE_LIVE", raising=False)
    client, _srv = _manual_client(monkeypatch)
    r = _post(client)
    assert r.status_code == 409
    assert "HERMES_ENABLE_LIVE" in r.text


def test_p01_manual_route_granted_passes_gate(monkeypatch):
    """With the grant the LIVE check passes; the request then fails input
    validation/downstream (400/500) — never the 409 P0-1 message."""
    monkeypatch.setenv("HERMES_ENABLE_LIVE", "true")
    client, _srv = _manual_client(monkeypatch)
    r = _post(client, coin="BAD-COIN-NAME", side="nonsense")
    assert r.status_code == 400
    assert "HERMES_ENABLE_LIVE" not in r.text


# ── P0-2: effective-config snapshot ────────────────────────────────────────

def test_p02_snapshot_structure_and_default_provenance(monkeypatch, tmp_path):
    # Isolate from any config file another test may have left behind: point
    # CONFIG_PATH at a nonexistent file in our own tmp dir so every leaf
    # provably resolves from canonical defaults, independent of suite order.
    missing = tmp_path / "no-config.json"
    monkeypatch.setattr(config_store, "CONFIG_PATH", str(missing))
    monkeypatch.setattr(config_store, "_CONFIG_LOCK_PATH", str(missing) + ".lock")
    config_store._invalidate_raw_cache()
    monkeypatch.delenv("HERMES_ENABLE_LIVE", raising=False)
    snap = config_store.build_effective_config_snapshot()
    assert set(snap) >= {
        "generated_at_ms", "config_file", "live_enabled",
        "legacy_env_overrides", "keys"}
    assert snap["live_enabled"] is False
    assert isinstance(snap["generated_at_ms"], int)
    assert snap["keys"]["mode"]["source"] == "default"
    assert snap["keys"]["mode"]["value"] == config_store.CANONICAL_DEFAULTS[
        "mode"]
    # Provenance vocabulary is closed.
    assert all(v["source"] in ("cfg_env", "file", "default", "unknown")
               or v["source"].startswith("error:")
               for v in snap["keys"].values())


def test_p02_snapshot_env_override_provenance(monkeypatch, tmp_path):
    missing = tmp_path / "no-config.json"
    monkeypatch.setattr(config_store, "CONFIG_PATH", str(missing))
    monkeypatch.setattr(config_store, "_CONFIG_LOCK_PATH", str(missing) + ".lock")
    config_store._invalidate_raw_cache()
    monkeypatch.setenv("HERMES_CFG_MODE", "SHADOW")
    monkeypatch.setenv("HERMES_ENABLE_LIVE", "true")
    snap = config_store.build_effective_config_snapshot()
    assert snap["keys"]["mode"]["source"] == "cfg_env"
    assert snap["keys"]["mode"]["value"] == "SHADOW"
    assert snap["live_enabled"] is True


def test_p02_snapshot_file_provenance(tmp_path, monkeypatch):
    cfg_file = tmp_path / "p0-agent-config.json"
    cfg_file.write_text(json.dumps({"mode": "LIVE"}))
    monkeypatch.setattr(config_store, "CONFIG_PATH", str(cfg_file))
    monkeypatch.setattr(config_store, "_CONFIG_LOCK_PATH",
                        str(cfg_file) + ".lock")
    config_store._invalidate_raw_cache()
    try:
        value, source = config_store._resolve_provenance("mode")
        assert source == "file"
        assert value == "LIVE"
        snap = config_store.build_effective_config_snapshot()
        assert snap["keys"]["mode"]["source"] == "file"
    finally:
        config_store._invalidate_raw_cache()


def test_p02_snapshot_legacy_env_visible(monkeypatch):
    monkeypatch.setenv("HERMES_SIZING_V2_MODE", "enforce")
    snap = config_store.build_effective_config_snapshot()
    assert snap["legacy_env_overrides"]["HERMES_SIZING_V2_MODE"] == "enforce"


def test_p02_write_snapshot_persists_json(tmp_path, monkeypatch):
    out = tmp_path / "sub" / "effective.json"
    monkeypatch.setenv("HERMES_ENABLE_LIVE", "true")
    written = config_store.write_effective_config_snapshot(str(out))
    assert written == str(out)
    data = json.loads(out.read_text())
    assert data["live_enabled"] is True
    assert "keys" in data and data["keys"]["mode"]


def test_p02_write_snapshot_env_path_and_disable(monkeypatch, tmp_path):
    out = tmp_path / "env-effective.json"
    monkeypatch.setenv("HERMES_EFFECTIVE_CONFIG_SNAPSHOT", str(out))
    assert config_store.write_effective_config_snapshot() == str(out)
    assert out.exists()
    monkeypatch.setenv("HERMES_EFFECTIVE_CONFIG_SNAPSHOT", "")
    assert config_store.write_effective_config_snapshot() is None


def test_p02_write_snapshot_best_effort_on_failure(monkeypatch, tmp_path):
    """A broken write must return None, never raise (startup must proceed)."""
    calls = {"n": 0}
    real = config_store.atomic_io.write_json_atomic

    def _boom(*_a, **_k):
        calls["n"] += 1
        raise OSError("read-only mount simulated")

    monkeypatch.setattr(config_store.atomic_io, "write_json_atomic", _boom)
    # Must not raise.
    assert config_store.write_effective_config_snapshot(
        str(tmp_path / "x.json")) is None
    assert calls["n"] == 1
    # Module-level binding is the same object; restore defensively.
    monkeypatch.setattr(config_store.atomic_io, "write_json_atomic", real)
