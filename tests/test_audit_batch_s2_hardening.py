"""Batch S2 guards — XFF lockdown (Q1) and portal-user audit attribution (Q4).

Q1: the edge nginx must OVERWRITE X-Forwarded-For with $remote_addr (not append),
and the deployed compose must pin HERMES_TRUST_PROXY=0, so a client-supplied XFF
first hop can never rotate the brute-force lockout identity.

Q4: the portal BFF injects one shared X-Operator-Token for every portal user plus
an X-Portal-User header. trader now reads that header (strictly sanitized) and
records it on write-side audit rows, so an action taken "via portal" can be
attributed to the actual portal account.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_trader import server
from hermes_trader.dashboard import operator_portal_user


def _req_with_headers(headers):
    from starlette.requests import Request
    scope = {"type": "http", "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()]}
    return Request(scope)


# ── Q4: operator_portal_user parsing/sanitization ────────────────────────────

def test_portal_user_passthrough():
    assert operator_portal_user(_req_with_headers({"X-Portal-User": "alice.01"})) == "alice.01"
    assert operator_portal_user(_req_with_headers({"X-Portal-User": "a@b.com"})) == "a@b.com"


def test_portal_user_absent_is_empty():
    assert operator_portal_user(_req_with_headers({})) == ""


@pytest.mark.parametrize("evil", [
    "alice\nGET /admin",       # CRLF / log injection
    "alice bob",               # space
    "alice$(reboot)",          # shell metachars
    "alice/bob",               # path sep
    "x" * 65,                  # too long
    "<script>",
    "alice,bob",               # comma (would poison CSV-like audit)
])
def test_portal_user_rejects_injection(evil):
    assert operator_portal_user(_req_with_headers({"X-Portal-User": evil})) == ""


def test_http_audit_records_portal_user(monkeypatch):
    import hermes_trader.event_log as event_log
    captured = {}
    monkeypatch.setattr(event_log, "append",
                        lambda event, payload=None: captured.update(payload=payload) or True)
    req = _req_with_headers({"X-Portal-User": "trader_li"})
    server._http_operator_audit("cancel_order", request=req, oid=5, ok=False)
    assert captured["payload"]["portal_user"] == "trader_li"
    assert captured["payload"]["action"] == "cancel_order"
    assert captured["payload"]["via"] == "http"


def test_http_audit_without_portal_user_omits_field(monkeypatch):
    import hermes_trader.event_log as event_log
    captured = {}
    monkeypatch.setattr(event_log, "append",
                        lambda event, payload=None: captured.update(payload=payload) or True)
    server._http_operator_audit("cancel_order", request=_req_with_headers({}), oid=6)
    assert "portal_user" not in captured["payload"]


# ── Q1: edge config guards (only when the deployed paths are present) ─────────

def test_nginx_overwrites_xff_and_compose_pins_trust_proxy():
    nginx_candidates = [
        Path("/home/ldy/hermes-portal/nginx/nginx.conf"),
        Path(__file__).resolve().parents[2] / "hermes-portal" / "nginx" / "nginx.conf",
    ]
    nginx_conf = next((p for p in nginx_candidates if p.exists()), None)
    if nginx_conf is None:
        pytest.skip("deployed nginx.conf not present")
    ntext = nginx_conf.read_text(encoding="utf-8")
    # Overwrite semantics on the edge hop; the append form must be gone.
    assert "X-Forwarded-For   $remote_addr;" in ntext
    assert "X-Forwarded-For   $proxy_add_x_forwarded_for;" not in ntext

    compose_candidates = [
        Path("/home/ldy/hermes-deploy/docker-compose.yml"),
    ]
    compose = next((p for p in compose_candidates if p.exists()), None)
    if compose is None:
        pytest.skip("deployed docker-compose.yml not present")
    ctext = compose.read_text(encoding="utf-8")
    assert "HERMES_TRUST_PROXY=0" in ctext
