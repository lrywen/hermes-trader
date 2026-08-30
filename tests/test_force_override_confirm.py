"""O-1: arming a FORBIDDEN_OVERRIDE force-execute switch over HTTP is a
deliberate two-step action, not a single leaked-token keystroke.

Flow:
  1. POST /api/dashboard/config/force-confirm {"updates": {...}} validates
     the patch and returns a one-time confirm_token bound to the exact
     payload + operator IP (TTL 120s).
  2. The real write must echo {"confirm_token": ..., "updates": {...}} with
     the same body; the token is single-use and a mismatched payload / IP /
     expired token is rejected (409).
  3. After one successful arm, the same IP is in a cooldown window (429 +
     Retry-After) and cannot arm again.

Disarming (false) and non-force keys are unaffected; the free-form terminal
`set` verb refuses to arm at all.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hermes_trader import dashboard
from hermes_trader.agents.config_store import read_agent_config, write_agent_config
from hermes_trader.dashboard import register_routes

_OP_TOKEN = "test-op-secret-o1"

_ARM_UPDATE = {
    "whale_force_execute": True,
    "override_requires_ai": True,
}


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setenv("HERMES_OPERATOR_TOKEN", _OP_TOKEN)
    # Make the cooldown non-blocking across unrelated assertions except
    # where a test explicitly inspects it; TTL kept generous.
    monkeypatch.setattr(dashboard, "_FORCE_ARM_COOLDOWN_S", 0)
    dashboard._reset_force_override_gate()
    app = FastAPI()
    register_routes(app)
    cfg = read_agent_config()
    # Deterministic start: every force switch disarmed.
    for k in dashboard.FORCE_OVERRIDE_KEYS:
        cfg[k] = False
    cfg["override_requires_ai"] = False
    write_agent_config(cfg, backup=False)
    c = TestClient(app)
    yield c
    # Clean up on-disk state so other test modules never see armed switches.
    after = read_agent_config()
    for k in dashboard.FORCE_OVERRIDE_KEYS:
        after[k] = False
    after["override_requires_ai"] = False
    write_agent_config(after, backup=False)
    dashboard._reset_force_override_gate()


def _auth():
    return {"Authorization": f"Bearer {_OP_TOKEN}"}


def _force_confirm(client, updates):
    return client.post("/api/dashboard/config/force-confirm",
                       json={"updates": updates}, headers=_auth())


# ── happy path ─────────────────────────────────────────────────────────────

def test_two_step_arm_succeeds(client):
    r = _force_confirm(client, _ARM_UPDATE)
    assert r.status_code == 200, r.text
    tok = r.json()["confirm_token"]
    assert r.json()["arming"] == ["whale_force_execute"]

    r = client.post("/api/dashboard/config",
                    json={"updates": _ARM_UPDATE, "confirm_token": tok},
                    headers=_auth())
    assert r.status_code == 200, r.text
    assert read_agent_config()["whale_force_execute"] is True


def test_arm_without_token_is_rejected(client):
    r = client.post("/api/dashboard/config",
                    json={"updates": _ARM_UPDATE}, headers=_auth())
    assert r.status_code == 409
    body = r.json()
    # The error body tells the client where to get the token.
    assert "force-confirm" in str(body)
    assert read_agent_config()["whale_force_execute"] is False


def test_token_is_single_use(client):
    tok = _force_confirm(client, _ARM_UPDATE).json()["confirm_token"]
    r1 = client.post("/api/dashboard/config",
                     json={"updates": _ARM_UPDATE, "confirm_token": tok},
                     headers=_auth())
    assert r1.status_code == 200
    # Replay: token was consumed on the first apply.
    r2 = client.post("/api/dashboard/config",
                     json={"updates": _ARM_UPDATE, "confirm_token": tok},
                     headers=_auth())
    assert r2.status_code == 409


def test_token_bound_to_payload(client):
    tok = _force_confirm(client, _ARM_UPDATE).json()["confirm_token"]
    # Same token, different arming switch -> fingerprint mismatch.
    other = {"breakout_force_execute": True, "override_requires_ai": True}
    r = client.post("/api/dashboard/config",
                    json={"updates": other, "confirm_token": tok},
                    headers=_auth())
    assert r.status_code == 409
    assert read_agent_config().get("breakout_force_execute") is False


def test_force_confirm_validates_schema(client):
    # Arming without override_requires_ai is still schema-rejected at the
    # prepare step (422), so the operator never gets a token for it.
    r = _force_confirm(client, {"whale_force_execute": True})
    assert r.status_code == 422


def test_force_confirm_requires_token(client):
    r = client.post("/api/dashboard/config/force-confirm",
                    json={"updates": _ARM_UPDATE})
    assert r.status_code == 401


def test_force_confirm_rejects_non_arm_update(client):
    r = _force_confirm(client, {"leverage": 5})
    assert r.status_code == 400


def test_disarm_and_normal_keys_unaffected(client):
    # Disarming / ordinary writes never need the confirmation token.
    r = client.post("/api/dashboard/config",
                    json={"updates": {"spread_gate_fail_open": False,
                                      "leverage": 7}},
                    headers=_auth())
    assert r.status_code == 200, r.text


def test_arming_cooldown_blocks_repeat(client, monkeypatch):
    monkeypatch.setattr(dashboard, "_FORCE_ARM_COOLDOWN_S", 300)
    dashboard._reset_force_override_gate()
    tok = _force_confirm(client, _ARM_UPDATE).json()["confirm_token"]
    r = client.post("/api/dashboard/config",
                    json={"updates": _ARM_UPDATE, "confirm_token": tok},
                    headers=_auth())
    assert r.status_code == 200
    # Second prepare within the cooldown -> 429 + Retry-After.
    r2 = _force_confirm(client, _ARM_UPDATE)
    assert r2.status_code == 429
    assert "Retry-After" in r2.headers


def test_terminal_set_cannot_arm(client):
    r = client.post("/api/dashboard/operator/terminal",
                    json={"command": "set whale_force_execute true"},
                    headers=_auth())
    assert r.status_code == 200
    payload = r.json()
    assert payload["kind"] == "error"
    assert "two-step confirmation" in payload["response"]
    assert read_agent_config().get("whale_force_execute") is False


def test_terminal_set_can_disarm(client):
    r = client.post("/api/dashboard/operator/terminal",
                    json={"command": "set whale_force_execute false"},
                    headers=_auth())
    assert r.status_code == 200
    assert r.json()["kind"] == "action"
