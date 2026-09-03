"""P0-7 regression: every state-changing server route must use
``require_operator_write`` so the per-IP F11 rate-limit actually fires.

Previously the write endpoints in ``hermes_trader.server`` (``/api/agent/execute``,
``/api/hl/place-order``, ``/api/hl/close-position``, ``/api/hl/cancel-order``,
``/api/agent/{start,stop,config}``, ``/api/agent/scan``, ``/api/agent/research*``,
``/api/risk/review/stream``) all declared ``Depends(_require_operator)`` — the
``write=False`` default — meaning a valid but leaked token could fire unlimited
state-changing requests. This file pins that down:

  1. Each write route is wired to ``require_operator_write`` (introspected
     from the FastAPI app's dependency tree).
  2. Each write route, when hit past the per-IP cap, returns 429 + Retry-After.
  3. Read-only routes are still on the lighter ``_require_operator`` path
     (no rate-limit, no extra cost on health / dashboard reads).
  4. ``require_operator_write`` is itself the canonical wrapper that pins
     ``write=True`` on ``_require_operator`` (so a future developer cannot
     silently re-introduce ``write=False`` on a write route).
"""

from __future__ import annotations

import inspect

import pytest
from fastapi.testclient import TestClient

# Module-level operator token; we set the env var at fixture setup so
# ``_require_operator`` accepts our Authorization: Bearer header.
_OP_TOKEN = "p07-test-op-token-XYZ"


# ── fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture()
def _env(monkeypatch):
    monkeypatch.setenv("HERMES_OPERATOR_TOKEN", _OP_TOKEN)
    # Speed: drop the write cap to 3 so we can hit the limit deterministically.
    from hermes_trader import dashboard
    monkeypatch.setattr(dashboard, "_WRITE_RATE_MAX", 3)
    monkeypatch.setattr(dashboard, "_WRITE_RATE_WINDOW_S", 60.0)
    dashboard._write_hits.clear()
    dashboard._auth_failures.clear()
    return dashboard


@pytest.fixture()
def client(_env):
    """A TestClient around the real ``hermes_trader.server.app``.

    We use the production app so route introspection sees the actual
    Depends(...) wiring; ``metrics`` test does the same and works fine
    without a live session_log / PID file.
    """
    from hermes_trader.server import app
    return TestClient(app, raise_server_exceptions=False)


def _auth():
    return {"Authorization": f"Bearer {_OP_TOKEN}"}


# ── 1. introspection: every write route uses require_operator_write ────────


# The P0-7 inventory: every state-changing endpoint under hermes_trader.server
# MUST be on require_operator_write. Listed by (method, path) so a future
# addition to the surface forces an explicit decision here.
_WRITE_ROUTES = [
    ("POST", "/api/agent/scan"),
    ("POST", "/api/agent/research/ETH"),
    ("POST", "/api/agent/research/ETH/stream"),
    ("POST", "/api/risk/review/stream"),
    ("POST", "/api/agent/execute"),
    ("POST", "/api/agent/start"),
    ("POST", "/api/agent/stop"),
    ("POST", "/api/agent/config"),
    ("POST", "/api/hl/place-order"),
    ("POST", "/api/hl/close-position"),
    ("POST", "/api/hl/cancel-order"),
]

# Read-only routes stay on the lighter _require_operator (write=False) so
# health-checks / dashboard reads don't burn the F11 budget.
_READ_ROUTES = [
    ("GET", "/api/agent/state"),
    ("GET", "/api/agent/trades"),
    ("GET", "/api/agent/session-log"),
    ("GET", "/api/agent/start"),
    ("GET", "/api/agent/config"),
    ("GET", "/api/hl/account"),
    ("GET", "/api/hl/portfolio"),
]


def _find_route(app, method: str, path: str):
    """Locate the FastAPI route matching (method, path).

    Strips trailing ``/{param}`` placeholders from the registered route
    template so test paths like ``/api/agent/research/ETH`` resolve to
    the template ``/api/agent/research/{coin}``.
    """
    method = method.upper()
    for r in app.routes:
        route_path = getattr(r, "path", None)
        if route_path is None:
            continue
        if method not in getattr(r, "methods", set()):
            continue
        # Convert /api/agent/research/{coin} → /api/agent/research/[^/]+
        # to make the registered path match a concrete request path.
        import re
        pattern = re.sub(r"\{[^/]+\}", r"[^/]+", route_path)
        if re.fullmatch(pattern, path):
            return r
    return None


def _route_dependencies(route):
    """Yield the actual callable for each Depends() declared on ``route``."""
    out = []
    deps = getattr(route, "dependant", None)
    if deps is None:
        # Pre-resolution: fall back to dependencies list.
        return list(getattr(route, "dependencies", []) or [])
    for d in (deps.dependencies or []):
        out.append(d.call)
    return out


class TestWriteRouteInventory:
    """Every P0-7 write route must depend on require_operator_write."""

    @pytest.mark.parametrize("method,path", _WRITE_ROUTES)
    def test_write_route_uses_require_operator_write(self, _env, method, path):
        from hermes_trader.server import app
        route = _find_route(app, method, path)
        assert route is not None, f"{method} {path} not registered"
        deps = _route_dependencies(route)
        # The wrapper may be referenced via Depends() or a sub-dependency.
        dep_names = {getattr(d, "__name__", repr(d)) for d in deps}
        # Match by name (most stable across module reloads).
        assert "require_operator_write" in dep_names, (
            f"{method} {path} dependencies={dep_names!r} — must include "
            f"require_operator_write (P0-7)"
        )

    @pytest.mark.parametrize("method,path", _READ_ROUTES)
    def test_read_route_still_on_require_operator(self, _env, method, path):
        from hermes_trader.server import app
        route = _find_route(app, method, path)
        assert route is not None, f"{method} {path} not registered"
        deps = _route_dependencies(route)
        dep_names = {getattr(d, "__name__", repr(d)) for d in deps}
        # Reads stay on the lighter path; require_operator_write must NOT
        # be wired in (that would unnecessarily rate-limit dashboards).
        assert "require_operator_write" not in dep_names, (
            f"{method} {path} should NOT depend on require_operator_write"
        )
        assert "_require_operator" in dep_names, (
            f"{method} {path} should depend on _require_operator"
        )


# ── 2. require_operator_write is a real wrapper around write=True ──────────


def test_require_operator_write_passes_write_true(_env):
    """The wrapper must internally invoke _require_operator with write=True.
    Pinned by signature inspection so a refactor cannot silently drop the
    write flag again."""
    from hermes_trader.dashboard import require_operator_write
    src = inspect.getsource(require_operator_write)
    assert "_require_operator" in src
    assert "write=True" in src, (
        "require_operator_write must pin write=True; otherwise the F11 "
        "per-IP write rate-limit never fires on state-changing endpoints."
    )


# ── 3. end-to-end: a real write route trips the F11 cap at 4th hit ──────────


def test_write_endpoint_returns_429_past_cap(client, _env):
    """Hammer POST /api/agent/stop (smallest, no body) past the 3-call cap
    and verify the 4th call gets 429 + Retry-After. Uses a real route so
    the test catches any future refactor that drops the Depends() wiring."""
    # The route has its own body validation but limiter fires BEFORE the
    # handler — first 3 should return whatever the handler returns (likely
    # 200 or 400/409 because no scanner is running, never 429); the 4th
    # MUST be 429 from the dependency.
    last_status = None
    for _ in range(3):
        r = client.post("/api/agent/stop", headers=_auth())
        last_status = r.status_code
        # Must NOT be 429 within the budget.
        assert r.status_code != 429, f"premature 429: {r.text}"
    # Past the cap.
    r = client.post("/api/agent/stop", headers=_auth())
    assert r.status_code == 429, (
        f"4th write did not trip the limiter (got {r.status_code}={r.text!r})"
    )
    assert r.headers.get("retry-after")


def test_read_endpoint_never_trips_write_limiter(client, _env):
    """Sanity: reads do NOT consume the F11 write budget. We pound a read
    20 times and then confirm a single write still has the full 3-call
    budget remaining (writes cap is independent of read traffic)."""
    for _ in range(20):
        r = client.get("/api/agent/state", headers=_auth())
        assert r.status_code == 200, r.text
    # Now the first write should pass (we still have the full 3-call budget).
    r = client.post("/api/agent/stop", headers=_auth())
    assert r.status_code != 429, (
        f"reads should not consume write budget; got 429 on first write: {r.text}"
    )


def test_write_endpoint_unauthenticated_returns_401(client, _env):
    """No token => 401 from the auth gate, NOT 429 from the limiter
    (the limiter only fires once auth succeeds)."""
    r = client.post("/api/agent/stop", json={})
    assert r.status_code == 401


def test_write_endpoint_with_bad_token_returns_401(client, _env):
    """Bad token => 401, and the bad attempt should NOT itself count against
    the F11 write budget (the limiter only fires for AUTHENTICATED writes)."""
    bad = {"Authorization": "Bearer not-the-real-token"}
    r = client.post("/api/agent/stop", headers=bad)
    assert r.status_code == 401
    # Subsequent valid write should still have its full 3-call budget.
    r = client.post("/api/agent/stop", headers=_auth())
    assert r.status_code != 429
