"""H-1 (audit 2026-08-29): POST /api/hl/flatten-all — emergency flat-all.

Enumerates EVERY open perp position (incl. HIP-3 clearinghouses) and closes
each through executor.close_position_market. Red-line control: requires the
explicit ``X-Confirm-Flatten: confirm`` header on top of operator-write auth,
so an induced/CSRF single POST cannot flatten the whole book.
"""

import pytest

_OP_TOKEN = "flatten-all-test-op-token-XYZ"


@pytest.fixture()
def _env(monkeypatch):
    monkeypatch.setenv("HERMES_OPERATOR_TOKEN", _OP_TOKEN)
    from hermes_trader import dashboard
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


def _confirm():
    return {"X-Confirm-Flatten": "confirm"}


def _state(coins_with_szi):
    """Build a fetch_account_state-style payload from {coin: szi}."""
    return {"asset_positions": [
        {"position": {"coin": c, "szi": str(s)}} for c, s in coins_with_szi
    ]}


def test_flatten_all_without_confirm_header_is_409(client):
    r = client.post("/api/hl/flatten-all", headers=_auth())
    assert r.status_code == 409, r.text
    assert "X-Confirm-Flatten" in str(r.json().get("detail", ""))


def test_flatten_all_unauthenticated_is_401_not_409(client):
    # Auth dependency fires before the handler: no token → 401 even with the
    # confirm header.
    r = client.post("/api/hl/flatten-all", headers=_confirm())
    assert r.status_code == 401, r.text


def test_flatten_all_wrong_confirm_value_is_409(client):
    r = client.post("/api/hl/flatten-all",
                    headers={**_auth(), "X-Confirm-Flatten": "yes"})
    assert r.status_code == 409, r.text


def test_flatten_all_no_wallet_is_400(client, monkeypatch):
    from hermes_trader import server
    monkeypatch.setattr(server, "resolve_user_address", lambda: None)
    r = client.post("/api/hl/flatten-all", headers={**_auth(), **_confirm()})
    assert r.status_code == 400, r.text


def test_flatten_all_account_fetch_failure_is_502(client, monkeypatch):
    from hermes_trader import server
    monkeypatch.setattr(server, "resolve_user_address", lambda: "0xabc")

    def _boom(*_a, **_k):
        raise RuntimeError("exchange down")

    monkeypatch.setattr(server, "fetch_account_state", _boom)
    r = client.post("/api/hl/flatten-all", headers={**_auth(), **_confirm()})
    assert r.status_code == 502, r.text


def test_flatten_all_no_open_positions_is_noop(client, monkeypatch):
    from hermes_trader import server
    monkeypatch.setattr(server, "resolve_user_address", lambda: "0xabc")
    # include_hip3 kwarg must be accepted; flat book + one zero-szi row.
    monkeypatch.setattr(server, "fetch_account_state",
                        lambda *a, **k: _state([("BTC", "0")]))
    closed = []
    monkeypatch.setattr(server, "close_position_market",
                        lambda c: closed.append(c) or {"ok": True, "coin": c})
    r = client.post("/api/hl/flatten-all", headers={**_auth(), **_confirm()})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body.get("noop") == "no_open_positions"
    assert body.get("total") == 0 and closed == []


def test_flatten_all_closes_every_coin_incl_hip3(client, monkeypatch):
    from hermes_trader import server
    monkeypatch.setattr(server, "resolve_user_address", lambda: "0xabc")
    # HIP-3 name (xyz:MU) must survive untouched — no client-side upper().
    monkeypatch.setattr(server, "fetch_account_state",
                        lambda *a, **k: _state([("BTC", "0.1"),
                                                ("xyz:MU", "-5.0"),
                                                ("ETH", "0")]))
    closed = []
    monkeypatch.setattr(server, "close_position_market",
                        lambda c: closed.append(c) or {"ok": True, "coin": c})
    r = client.post("/api/hl/flatten-all", headers={**_auth(), **_confirm()})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body.get("total") == 2
    assert set(body.get("flattened", [])) == {"BTC", "xyz:MU"}
    assert body.get("failed") == []
    assert set(closed) == {"BTC", "xyz:MU"}


def test_flatten_all_partial_failure_returns_200_with_failures(client, monkeypatch):
    from hermes_trader import server
    monkeypatch.setattr(server, "resolve_user_address", lambda: "0xabc")
    monkeypatch.setattr(server, "fetch_account_state",
                        lambda *a, **k: _state([("BTC", "0.1"), ("ETH", "2.0")]))

    def _close(c):
        if c == "ETH":
            return {"ok": False, "coin": c, "error": "slippage"}
        return {"ok": True, "coin": c}

    monkeypatch.setattr(server, "close_position_market", _close)
    r = client.post("/api/hl/flatten-all", headers={**_auth(), **_confirm()})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body.get("flattened") == ["BTC"]
    assert body.get("failed") == [{"coin": "ETH", "error": "slippage"}]


def test_flatten_all_total_failure_is_400(client, monkeypatch):
    from hermes_trader import server
    monkeypatch.setattr(server, "resolve_user_address", lambda: "0xabc")
    monkeypatch.setattr(server, "fetch_account_state",
                        lambda *a, **k: _state([("BTC", "0.1")]))

    def _boom_close(c):
        raise RuntimeError("exchange down")

    monkeypatch.setattr(server, "close_position_market", _boom_close)
    r = client.post("/api/hl/flatten-all", headers={**_auth(), **_confirm()})
    assert r.status_code == 400, r.text


def test_flatten_all_confirm_header_case_insensitive(client, monkeypatch):
    from hermes_trader import server
    monkeypatch.setattr(server, "resolve_user_address", lambda: "0xabc")
    monkeypatch.setattr(server, "fetch_account_state",
                        lambda *a, **k: _state([]))
    r = client.post("/api/hl/flatten-all",
                    headers={**_auth(), "X-Confirm-Flatten": "CONFIRM"})
    assert r.status_code == 200, r.text
