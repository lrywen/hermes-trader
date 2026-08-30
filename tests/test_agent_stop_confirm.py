"""Red-line control: POST /api/agent/stop requires an explicit confirmation
header in addition to operator-write auth. Stopping the scanner halts both
automated trading and automated risk monitoring / exits, so a single induced
or CSRF POST must not be able to fire it.
"""

import os

import pytest

_OP_TOKEN = "stop-confirm-test-op-token-XYZ"


@pytest.fixture()
def _env(monkeypatch):
    monkeypatch.setenv("HERMES_OPERATOR_TOKEN", _OP_TOKEN)
    from hermes_trader import dashboard
    # Generous cap so the repeated authenticated calls here don't trip F11.
    monkeypatch.setattr(dashboard, "_WRITE_RATE_MAX", 1000)
    monkeypatch.setattr(dashboard, "_WRITE_RATE_WINDOW_S", 60.0)
    dashboard._write_hits.clear()
    dashboard._auth_failures.clear()


@pytest.fixture()
def client(_env):
    from fastapi.testclient import TestClient

    from hermes_trader.server import app
    return TestClient(app, raise_server_exceptions=False)


def _auth():
    return {"Authorization": f"Bearer {_OP_TOKEN}"}


def test_stop_without_confirm_header_is_409(client):
    # Authenticated + under rate limit, but no explicit acknowledgement →
    # refused with 409 and a message telling the caller what header to echo.
    r = client.post("/api/agent/stop", headers=_auth())
    assert r.status_code == 409, r.text
    assert "X-Confirm-Stop" in str(r.json().get("detail", ""))


def test_stop_unauthenticated_is_401_not_409(client):
    # Auth gate fires first: no token → 401 even with the confirm header.
    r = client.post("/api/agent/stop", headers={"X-Confirm-Stop": "confirm"})
    assert r.status_code == 401, r.text


def test_stop_with_wrong_confirm_value_is_409(client):
    r = client.post("/api/agent/stop",
                    headers={**_auth(), "X-Confirm-Stop": "yes"})
    assert r.status_code == 409, r.text


def test_stop_with_confirm_header_proceeds(client, monkeypatch, tmp_path):
    # With the exact confirmation header the handler runs past the gate.
    # Point PID_FILE at a non-existent temp path so the "proceed" branch is
    # exercised without touching a real agent process → reports not_running
    # (a gate refusal would have been 409).
    from hermes_trader import server
    fake_pid = str(tmp_path / "hermes_test_stop.pid")
    monkeypatch.setattr(server, "PID_FILE", fake_pid)
    assert not os.path.exists(fake_pid)
    r = client.post("/api/agent/stop",
                    headers={**_auth(), "X-Confirm-Stop": "confirm"})
    assert r.status_code == 200, r.text
    assert r.json().get("status") == "not_running"


def test_stop_confirm_header_case_insensitive(client, monkeypatch, tmp_path):
    from hermes_trader import server
    fake_pid = str(tmp_path / "hermes_test_stop2.pid")
    monkeypatch.setattr(server, "PID_FILE", fake_pid)
    r = client.post("/api/agent/stop",
                    headers={**_auth(), "X-Confirm-Stop": "CONFIRM"})
    assert r.status_code == 200, r.text
