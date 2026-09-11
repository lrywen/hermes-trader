"""Batch S1 guards — close the unauthenticated /metrics exposure via the proxy.

Audit 2026-09-11 (Q2+Q3) found that hermes-nginx mounted a PREFIX WILDCARD
``location /trader/ { proxy_pass http://trader_backend/; }`` which exposed every
trader route at ``0.0.0.0:8443/trader/**``. Combined with the metrics route's
``require_operator_or_internal`` gate — which trusts any RFC-1918 socket peer —
an unauthenticated remote caller was served Prometheus metrics, because trader
always saw nginx's own private bridge IP as the peer.

Two-layer fix (both required):
  * nginx: /trader/ is an allowlist of the Feishu postmortem paths only;
    everything else under /trader/ returns 404.
  * trader: /metrics uses require_operator_or_loopback (true loopback or a valid
    operator token), NOT the broad RFC-1918 internal gate. Postmortems keep the
    internal gate because token-free LAN viewing from Feishu push cards is an
    explicit product requirement.

These tests pin the trader-side dependency behavior. The nginx allowlist is
pinned by a source guard on the deployed config path when present.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from hermes_trader import server, dashboard

_OP_TOKEN = "test-op-secret-s1"

# A non-loopback RFC-1918 address, simulating nginx's docker bridge peer as it
# appears to trader when a remote client comes in via 0.0.0.0:8443.
_PROXY_PEER = ("172.19.0.4", 51234)
_LOOP_PEER = ("127.0.0.1", 51235)


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setenv("HERMES_OPERATOR_TOKEN", _OP_TOKEN)
    return TestClient(server.app, raise_server_exceptions=False)


# ── /metrics: loopback OK without token; proxy-peer must present a token ──────

def test_metrics_loopback_peer_open_without_token(client):
    c = TestClient(server.app, client=_LOOP_PEER, raise_server_exceptions=False)
    r = c.get("/metrics")
    assert r.status_code == 200
    assert "text/plain" in r.headers["content-type"]


def test_metrics_proxy_private_peer_rejected_without_token(client):
    c = TestClient(server.app, client=_PROXY_PEER, raise_server_exceptions=False)
    r = c.get("/metrics")
    assert r.status_code == 401, r.text
    # Postmortems-style RFC-1918 trust must NOT apply to metrics any more.
    assert r.status_code != 200


def test_metrics_proxy_private_peer_allowed_with_operator_token(client):
    c = TestClient(server.app, client=_PROXY_PEER, raise_server_exceptions=False)
    r = c.get("/metrics", headers={"X-Operator-Token": _OP_TOKEN})
    assert r.status_code == 200
    assert "text/plain" in r.headers["content-type"]


def test_metrics_proxy_private_peer_bad_token_rejected(client):
    c = TestClient(server.app, client=_PROXY_PEER, raise_server_exceptions=False)
    r = c.get("/metrics", headers={"X-Operator-Token": "wrong"})
    assert r.status_code in (401, 429)


# ── postmortems: RFC-1918 proxy peer still passes the gate (Feishu no-login) ──

def test_postmortems_private_peer_still_internal_allowed(client, monkeypatch):
    """The Feishu push-card viewer relies on token-free LAN/proxy access; the
    dependency must admit a private bridge peer even though /metrics no longer
    does. The handler may return an empty list in CI — only the gate matters."""
    c = TestClient(server.app, client=_PROXY_PEER, raise_server_exceptions=False)
    r = c.get("/postmortems")
    # Passed the auth dependency (not 401/503). 200 with a JSON body expected.
    assert r.status_code == 200, r.text


def test_postmortems_public_peer_rejected_without_token(client):
    c = TestClient(server.app, client=("8.8.8.8", 51236), raise_server_exceptions=False)
    r = c.get("/postmortems")
    assert r.status_code == 401


# ── source guards ─────────────────────────────────────────────────────────────

def test_metrics_route_uses_loopback_gate_not_internal():
    text = Path(server.__file__).read_text(encoding="utf-8")
    # /metrics is guarded by the loopback dependency...
    assert '"/metrics", dependencies=[Depends(require_operator_or_loopback)]' in text
    # ...and postmortems retain the internal (LAN token-free) gate.
    assert '"/postmortems", dependencies=[Depends(require_operator_or_internal)]' in text
    assert '"/postmortems/{name}", dependencies=[Depends(require_operator_or_internal)]' in text
    # The tightened helper exists.
    assert "def require_operator_or_loopback" in Path(dashboard.__file__).read_text(
        encoding="utf-8")


def _deployed_nginx_conf() -> Path:
    # The deployed nginx config lives outside this repo; assert its shape only
    # when that path is available on this host (no-op in plain CI).
    candidates = [
        Path("/home/ldy/hermes-portal/nginx/nginx.conf"),
        Path(__file__).resolve().parents[2] / "hermes-portal" / "nginx" / "nginx.conf",
    ]
    conf = next((p for p in candidates if p.exists()), None)
    if conf is None:
        pytest.skip("deployed nginx.conf not present in this environment")
    return conf


def test_nginx_trader_prefix_is_allowlist_when_config_present():
    text = _deployed_nginx_conf().read_text(encoding="utf-8")
    # Wildcard proxy_pass at /trader/ root must be gone.
    assert "proxy_pass http://trader_backend/;" not in text
    # Only the postmortem allowlist may proxy under /trader/.
    assert "location ^~ /trader/postmortems/" in text
    assert "proxy_pass http://trader_backend/postmortems/" in text
    # Everything else under /trader/ is explicitly refused.
    assert "location ^~ /trader/" in text


def test_nginx_bare_trader_paths_are_private_network_only():
    """Q2 remainder: /api/dashboard/ and /api/feed/ bypass the portal BFF and
    reach trader directly, where public.py / shadow.py / audit.py read routes are
    anonymous by design (their RBAC lives in the BFF's _PATH_RULES). Those two
    locations must therefore stay reachable from the LAN only, never from the
    public edge on 0.0.0.0:8443."""
    text = _deployed_nginx_conf().read_text(encoding="utf-8")
    blocks = _location_bodies(text)
    for path in ("/api/dashboard/", "/api/feed/"):
        body = blocks.get(path)
        assert body is not None, f"location {path} missing from deployed config"
        for rule in (
            "allow 127.0.0.1;",
            "allow 10.0.0.0/8;",
            "allow 172.16.0.0/12;",
            "allow 192.168.0.0/16;",
            "deny  all;",
        ):
            assert rule in body, f"{path} lost allowlist rule {rule!r}"
        # deny must come last, otherwise the allowlist is a no-op.
        assert body.index("deny  all;") > body.index("allow 192.168.0.0/16;")


def test_nginx_postmortems_allowlist_is_private_network_only():
    """Q3 follow-up: the two /trader/postmortems locations are the only holes in
    the /trader/ 404 wall, and trader's require_operator_or_internal sees every
    proxied request as a private bridge peer — so without an edge allowlist the
    reports (account equity, killswitch thresholds, leverage, order ids) are
    served to anyone who can reach 0.0.0.0:8443. Feishu cards are opened from the
    LAN, so restricting to RFC-1918 keeps the token-free viewer working."""
    text = _deployed_nginx_conf().read_text(encoding="utf-8")
    blocks = _location_bodies(text)
    for path in ("/trader/postmortems", "/trader/postmortems/"):
        body = blocks.get(path)
        assert body is not None, f"location {path} missing from deployed config"
        for rule in (
            "allow 127.0.0.1;",
            "allow 10.0.0.0/8;",
            "allow 172.16.0.0/12;",
            "allow 192.168.0.0/16;",
            "deny  all;",
        ):
            assert rule in body, f"{path} lost allowlist rule {rule!r}"
        assert body.index("deny  all;") > body.index("allow 192.168.0.0/16;")
        # The allowlist must precede proxy_pass so access phase runs first.
        assert body.index("deny  all;") < body.index("proxy_pass")


def _location_bodies(text: str) -> dict[str, str]:
    """Map ``location <path>`` -> raw body text for simple one-level blocks.

    The optional nginx modifier (``=``, ``^~``, ``~``, ``~*``) is stripped so the
    key is always the bare path/pattern.
    """
    bodies: dict[str, str] = {}
    pattern = r"location\s+(?:(?:=|\^~|~\*|~)\s+)?([^\s{]+)\s*\{"
    for match in re.finditer(pattern, text):
        start = match.end()
        depth = 1
        idx = start
        while idx < len(text) and depth:
            if text[idx] == "{":
                depth += 1
            elif text[idx] == "}":
                depth -= 1
            idx += 1
        bodies[match.group(1)] = text[start : idx - 1]
    return bodies
