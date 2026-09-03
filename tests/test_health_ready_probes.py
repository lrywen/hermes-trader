"""Offline tests for the P1-3 liveness/readiness probes.

Liveness (/api/health) must ALWAYS return 200 as long as the web process can
serve HTTP — it never checks dependencies, because a liveness failure restarts
the pod and a transient feed/API blip must never trigger a restart cascade.

Readiness (/api/ready) fuses the loop's feed-heartbeat freshness:
  * during the boot grace period a missing/stale heartbeat is tolerated (200,
    "warming") — the loop is still starting up;
  * after grace, a live/fresh heartbeat yields 200;
  * after grace, an offline/stale-past-dead feed yields 503 (pull from rotation
    WITHOUT restart).

These tests use monkeypatching only — no network, no state files, no trading.
"""

from fastapi.testclient import TestClient

from hermes_trader import server
from hermes_trader.server import app

client = TestClient(app)


def test_health_is_always_running_even_without_feed():
    """Liveness is process-level: no loop heartbeat / no feed -> still 200."""
    resp = client.get("/api/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "running"
    assert body["service"] == "Hermes-Trader"


def test_ready_warming_during_boot_grace(monkeypatch):
    """Within the boot grace window, a missing heartbeat is tolerated."""
    monkeypatch.setattr(server, "_READINESS_REQUIRE_LOOP", True)
    monkeypatch.setattr(server, "_READINESS_GRACE_S", 300.0)
    # Process started "now" -> uptime ~0 < grace.
    monkeypatch.setattr(server, "_PROCESS_START_TS", __import__("time").time())
    # No heartbeat available at all (CI has no session log).
    monkeypatch.setattr(
        server.dashboard, "_risk_status_payload",
        lambda: {"feed_status": "offline", "feed_age_s": None},
    )
    resp = client.get("/api/ready")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ready"] is True
    assert body["checks"]["loop"] == "warming"


def test_ready_503_when_feed_dead_after_grace(monkeypatch):
    """After grace, an offline feed must take the pod out of rotation (503)."""
    monkeypatch.setattr(server, "_READINESS_REQUIRE_LOOP", True)
    monkeypatch.setattr(server, "_READINESS_GRACE_S", 0.0)
    monkeypatch.setattr(server, "_READINESS_STALE_S", 180.0)
    monkeypatch.setattr(server, "_READINESS_DEAD_S", 600.0)
    monkeypatch.setattr(server, "_PROCESS_START_TS", __import__("time").time() - 9999)
    monkeypatch.setattr(
        server.dashboard, "_risk_status_payload",
        lambda: {"feed_status": "offline", "feed_age_s": None},
    )
    resp = client.get("/api/ready")
    assert resp.status_code == 503
    body = resp.json()
    assert body["ready"] is False
    assert body["checks"]["loop"] == "unavailable"


def test_ready_200_when_feed_live_after_grace(monkeypatch):
    """A fresh heartbeat after grace -> ready to serve traffic."""
    monkeypatch.setattr(server, "_READINESS_REQUIRE_LOOP", True)
    monkeypatch.setattr(server, "_READINESS_GRACE_S", 0.0)
    monkeypatch.setattr(server, "_READINESS_STALE_S", 180.0)
    monkeypatch.setattr(server, "_PROCESS_START_TS", __import__("time").time() - 9999)
    monkeypatch.setattr(
        server.dashboard, "_risk_status_payload",
        lambda: {"feed_status": "live", "feed_age_s": 12},
    )
    resp = client.get("/api/ready")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ready"] is True
    assert body["checks"]["loop"] == "ok"


def test_ready_200_stale_within_dead_window(monkeypatch):
    """A stale-but-recent heartbeat (slow research pass) still serves, flagged."""
    monkeypatch.setattr(server, "_READINESS_REQUIRE_LOOP", True)
    monkeypatch.setattr(server, "_READINESS_GRACE_S", 0.0)
    monkeypatch.setattr(server, "_READINESS_STALE_S", 180.0)
    monkeypatch.setattr(server, "_READINESS_DEAD_S", 600.0)
    monkeypatch.setattr(server, "_PROCESS_START_TS", __import__("time").time() - 9999)
    monkeypatch.setattr(
        server.dashboard, "_risk_status_payload",
        lambda: {"feed_status": "stale", "feed_age_s": 300},
    )
    resp = client.get("/api/ready")
    assert resp.status_code == 200
    assert resp.json()["checks"]["loop"] == "stale"


def test_ready_read_error_degrades_to_503_after_grace(monkeypatch):
    """If the liveness read itself raises, readiness is conservatively not-ready."""
    def _boom():
        raise RuntimeError("unreadable log")

    monkeypatch.setattr(server, "_READINESS_REQUIRE_LOOP", True)
    monkeypatch.setattr(server, "_READINESS_GRACE_S", 0.0)
    monkeypatch.setattr(server, "_PROCESS_START_TS", __import__("time").time() - 9999)
    monkeypatch.setattr(server.dashboard, "_risk_status_payload", _boom)
    resp = client.get("/api/ready")
    assert resp.status_code == 503


def test_ready_env_kill_switch_always_ready(monkeypatch):
    """HERMES_READINESS_REQUIRE_LOOP=0 -> readiness never gates on the loop."""
    monkeypatch.setattr(server, "_READINESS_REQUIRE_LOOP", False)
    monkeypatch.setattr(server, "_PROCESS_START_TS", __import__("time").time() - 9999)
    monkeypatch.setattr(
        server.dashboard, "_risk_status_payload",
        lambda: {"feed_status": "offline", "feed_age_s": None},
    )
    resp = client.get("/api/ready")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ready"] is True
    assert "loop" not in body["checks"]
