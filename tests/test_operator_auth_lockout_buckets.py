"""P0-2: operator auth-failure lockout must not collapse into one global bucket.

Behind the BFF every portal request shares one socket peer (the nginx bridge
IP), so the old purely-per-IP lockout let any LAN host 429 the whole operator
surface with 5 bad tokens. The fix keys the lockout on the BFF-authenticated
X-Portal-User when present (per-user buckets) while always keeping the socket
IP as a brute-force backstop. The IP backstop ceiling is relaxed ONLY for
trusted RFC1918 proxy hops, so a direct public client cannot raise its own IP
ceiling by forging X-Portal-User.
"""
from __future__ import annotations

import time

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from hermes_trader import dashboard as d


def _req(headers, client_host):
    scope = {
        "type": "http",
        "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
        "client": (client_host, 12345),
    }
    return Request(scope)


@pytest.fixture(autouse=True)
def _reset_auth(monkeypatch):
    monkeypatch.setenv("HERMES_OPERATOR_TOKEN", "good-token")
    d._auth_failures.clear()
    yield
    d._auth_failures.clear()


def _fail(req):
    with pytest.raises(HTTPException) as ei:
        d._require_operator(req)
    return ei.value.status_code


def test_bff_users_have_independent_buckets():
    """5 failures from portal user A lock out A only; user B (same nginx IP)
    still authenticates fine — the whole-site 429 is gone."""
    nginx_ip = "172.19.0.2"
    a = {"X-Portal-User": "alice", "X-Operator-Token": "bad"}
    for _ in range(5):
        _fail(_req(a, nginx_ip))
    # A is now locked (429, not 401).
    assert _fail(_req(a, nginx_ip)) == 429
    # B, behind the SAME nginx IP, with a valid token, is NOT locked out.
    b_ok = {"X-Portal-User": "bob", "X-Operator-Token": "good-token"}
    d._require_operator(_req(b_ok, nginx_ip))  # must not raise


def test_direct_client_cannot_raise_ip_ceiling_by_forging_portal_user():
    """A direct PUBLIC client forging X-Portal-User still faces the STRICT IP
    ceiling (5), because the relaxed ceiling only applies to RFC1918 hops."""
    public_ip = "203.0.113.9"
    forged = {"X-Portal-User": "fake", "X-Operator-Token": "bad"}
    for _ in range(5):
        _fail(_req(forged, public_ip))
    # 5 failures on the strict IP bucket -> locked, even though each carried a
    # (forged) portal user. Rotating the forged name must not help.
    forged2 = {"X-Portal-User": "fake2", "X-Operator-Token": "bad"}
    assert _fail(_req(forged2, public_ip)) == 429


def test_rfc1918_proxy_ip_backstop_uses_relaxed_ceiling():
    """Many failures across MANY portal users via the nginx IP must not trip the
    shared IP backstop (relaxed to 100) — only per-user buckets lock."""
    nginx_ip = "172.19.0.2"
    # 8 distinct users x 5 failures = 40 failures on the shared nginx IP, well
    # past the strict 5 but under the relaxed 100.
    for u in range(8):
        for _ in range(5):
            _fail(_req({"X-Portal-User": f"user{u}", "X-Operator-Token": "bad"}, nginx_ip))
    # A fresh valid user via the same nginx IP still authenticates.
    d._require_operator(_req({"X-Portal-User": "newbie", "X-Operator-Token": "good-token"}, nginx_ip))


def test_success_clears_user_bucket():
    nginx_ip = "172.19.0.2"
    bad = {"X-Portal-User": "alice", "X-Operator-Token": "bad"}
    for _ in range(3):
        _fail(_req(bad, nginx_ip))
    # A success clears the counter, so the next failures start from zero.
    d._require_operator(_req({"X-Portal-User": "alice", "X-Operator-Token": "good-token"}, nginx_ip))
    for _ in range(4):
        assert _fail(_req(bad, nginx_ip)) == 401  # not yet locked
    assert _fail(_req(bad, nginx_ip)) == 429      # 5th after reset locks


def test_is_rfc1918():
    assert d._is_rfc1918("172.19.0.2")
    assert d._is_rfc1918("10.0.0.5")
    assert d._is_rfc1918("192.168.1.1")
    assert d._is_rfc1918("127.0.0.1")
    assert not d._is_rfc1918("203.0.113.9")
    assert not d._is_rfc1918("not-an-ip")
