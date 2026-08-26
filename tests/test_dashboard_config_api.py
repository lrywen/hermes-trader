"""Unit tests for the dashboard config-management API (b2).

Covers:
  * GET  /api/dashboard/config           — public read
  * GET  /api/dashboard/config/schema     — key metadata
  * POST /api/dashboard/config            — operator-gated partial update
  * POST /api/dashboard/config            — 401 without token
  * POST /api/dashboard/config            — 422 on type/range validation
  * POST /api/dashboard/config            — 400 on empty/non-dict body
  * GET  /api/dashboard/config/backup     — operator-gated
  * POST /api/dashboard/config/rollback   — operator-gated round-trip
  * GET  /api/dashboard/config/history    — operator-gated audit events
  * GET  /config                          — HTML page served
"""

import os
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hermes_trader.agents import config_store
from hermes_trader.agents.config_store import (
    read_agent_config,
    write_agent_config,
    backup_config,
)
from hermes_trader.dashboard import register_routes


_OP_TOKEN = "test-op-secret-123"


@pytest.fixture()
def client(monkeypatch):
    """Build a fresh FastAPI app with dashboard routes and a known operator token."""
    monkeypatch.setenv("HERMES_OPERATOR_TOKEN", _OP_TOKEN)
    app = FastAPI()
    register_routes(app)
    # Seed a known baseline config so tests have deterministic starting state.
    cfg = read_agent_config()
    cfg["mode"] = "OFF"
    cfg["leverage"] = 10
    cfg["max_concurrent"] = 2
    cfg["min_ai_confidence"] = 0.7
    write_agent_config(cfg, backup=False)
    return TestClient(app)


def _auth():
    return {"Authorization": f"Bearer {_OP_TOKEN}"}


# ── read endpoints ──────────────────────────────────────────────────────────

def test_get_config_returns_full_dict(client):
    r = client.get("/api/dashboard/config")
    assert r.status_code == 200
    data = r.json()
    assert isinstance(data, dict)
    assert "mode" in data
    assert "leverage" in data


def test_get_config_schema_returns_types(client):
    r = client.get("/api/dashboard/config/schema")
    assert r.status_code == 200
    schema = r.json()
    assert schema["leverage"]["type"] == "int"
    assert schema["min_ai_confidence"]["type"] == "float"
    assert schema["mode"]["type"] == "str"
    assert schema["enable_crypto"]["type"] == "bool"
    assert schema["coin_allowlist"]["type"] == "list"
    assert schema["dsl_exit"]["type"] == "object"


def test_config_page_html_served(client):
    r = client.get("/config")
    assert r.status_code == 200
    assert "hermes-trader" in r.text.lower()
    assert "cfg-grid" in r.text


# ── POST /api/dashboard/config ──────────────────────────────────────────────

def test_post_config_requires_token(client):
    r = client.post("/api/dashboard/config", json={"updates": {"leverage": 5}})
    assert r.status_code == 401


def test_post_config_with_token_updates_value(client):
    r = client.post(
        "/api/dashboard/config",
        json={"updates": {"leverage": 5}},
        headers=_auth(),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["applied"]["leverage"] == 5
    # Verify persistence
    cfg = read_agent_config()
    assert cfg["leverage"] == 5


def test_post_config_multiple_keys(client):
    r = client.post(
        "/api/dashboard/config",
        json={"updates": {"leverage": 8, "max_concurrent": 4, "mode": "LIVE"}},
        headers=_auth(),
    )
    assert r.status_code == 200
    cfg = read_agent_config()
    assert cfg["leverage"] == 8
    assert cfg["max_concurrent"] == 4
    assert cfg["mode"] == "LIVE"


def test_post_config_empty_updates_400(client):
    r = client.post("/api/dashboard/config", json={"updates": {}}, headers=_auth())
    assert r.status_code == 400


def test_post_config_non_dict_updates_400(client):
    r = client.post("/api/dashboard/config", json={"updates": [1, 2]}, headers=_auth())
    assert r.status_code == 400


def test_post_config_unknown_key_422(client):
    r = client.post(
        "/api/dashboard/config",
        json={"updates": {"nonexistent_key_xyz": 1}},
        headers=_auth(),
    )
    assert r.status_code == 422


def test_post_config_type_mismatch_422(client):
    r = client.post(
        "/api/dashboard/config",
        json={"updates": {"leverage": "not-an-int"}},
        headers=_auth(),
    )
    assert r.status_code == 422


def test_post_config_range_validation_leverage(client):
    r = client.post(
        "/api/dashboard/config",
        json={"updates": {"leverage": 999}},
        headers=_auth(),
    )
    assert r.status_code == 422


def test_post_config_range_validation_confidence(client):
    r = client.post(
        "/api/dashboard/config",
        json={"updates": {"min_ai_confidence": 1.5}},
        headers=_auth(),
    )
    assert r.status_code == 422


def test_post_config_bool_rejects_non_bool(client):
    r = client.post(
        "/api/dashboard/config",
        json={"updates": {"enable_crypto": "yes"}},
        headers=_auth(),
    )
    assert r.status_code == 422


def test_post_config_creates_backup(client):
    """A successful POST must back up the previous config before overwriting."""
    before = read_agent_config()["leverage"]
    r = client.post(
        "/api/dashboard/config",
        json={"updates": {"leverage": before + 1}},
        headers=_auth(),
    )
    assert r.status_code == 200
    bak = backup_config()
    assert bak is not None
    assert bak["leverage"] == before


def test_post_config_audit_log(client):
    """A successful POST must append a config_update event to the session log."""
    from hermes_trader import session_log
    r = client.post(
        "/api/dashboard/config",
        json={"updates": {"max_concurrent": 5}},
        headers=_auth(),
    )
    assert r.status_code == 200
    events = session_log.tail(50)
    found = [e for e in events if e.get("event") == "config_update"
             and e.get("updates", {}).get("max_concurrent") == 5]
    assert found, "config_update event not found in session log"


# ── backup / rollback ───────────────────────────────────────────────────────

def test_get_backup_requires_token(client):
    r = client.get("/api/dashboard/config/backup")
    assert r.status_code == 401


def test_get_backup_after_write(client):
    client.post(
        "/api/dashboard/config",
        json={"updates": {"leverage": 7}},
        headers=_auth(),
    )
    r = client.get("/api/dashboard/config/backup", headers=_auth())
    assert r.status_code == 200
    data = r.json()
    assert data["available"] is True
    assert data["config"]["leverage"] == 10  # value before the write


def test_rollback_restores_previous(client):
    # Write a change (backup = original leverage=10)
    client.post(
        "/api/dashboard/config",
        json={"updates": {"leverage": 3}},
        headers=_auth(),
    )
    assert read_agent_config()["leverage"] == 3
    # Roll back
    r = client.post("/api/dashboard/config/rollback", headers=_auth())
    assert r.status_code == 200
    assert read_agent_config()["leverage"] == 10


def test_rollback_requires_token(client):
    r = client.post("/api/dashboard/config/rollback")
    assert r.status_code == 401


# ── history ─────────────────────────────────────────────────────────────────

def test_history_requires_token(client):
    r = client.get("/api/dashboard/config/history")
    assert r.status_code == 401


def test_history_records_updates(client):
    client.post(
        "/api/dashboard/config",
        json={"updates": {"max_concurrent": 6}},
        headers=_auth(),
    )
    r = client.get("/api/dashboard/config/history", headers=_auth())
    assert r.status_code == 200
    history = r.json()["history"]
    assert any(e.get("event") == "config_update" for e in history)


def test_history_records_rollback(client):
    client.post(
        "/api/dashboard/config",
        json={"updates": {"leverage": 4}},
        headers=_auth(),
    )
    client.post("/api/dashboard/config/rollback", headers=_auth())
    r = client.get("/api/dashboard/config/history", headers=_auth())
    assert r.status_code == 200
    events = {e["event"] for e in r.json()["history"]}
    assert "config_rollback" in events


# ── nested object update ────────────────────────────────────────────────────

def test_post_config_nested_object(client):
    """debate_gate is a nested dict — it should be replaceable as a whole."""
    new_gate = {"enabled": False, "min_agreement": 0.8, "min_agree_count": 4}
    r = client.post(
        "/api/dashboard/config",
        json={"updates": {"debate_gate": new_gate}},
        headers=_auth(),
    )
    assert r.status_code == 200, r.text
    assert read_agent_config()["debate_gate"] == new_gate


def test_post_config_list_value(client):
    r = client.post(
        "/api/dashboard/config",
        json={"updates": {"coin_allowlist": ["BTC", "ETH"]}},
        headers=_auth(),
    )
    assert r.status_code == 200, r.text
    assert read_agent_config()["coin_allowlist"] == ["BTC", "ETH"]


def test_post_config_list_wrong_type_422(client):
    r = client.post(
        "/api/dashboard/config",
        json={"updates": {"coin_allowlist": "BTC"}},
        headers=_auth(),
    )
    assert r.status_code == 422
