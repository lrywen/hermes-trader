"""Batch H-P2 guards:

  5. POST /api/agent/stop reports the REAL process state — "stopped" only
     after the SIGTERM'd pid actually exits; a surviving process yields 503
     stop_failed with the pid file retained for manual escalation.
  6. Interactive API docs (/docs, /redoc, /openapi.json) are disabled by
     default; HERMES_EXPOSE_DOCS=1 re-enables them for local debugging.
  7. The uvicorn entrypoint binds 127.0.0.1 by default; HERMES_HOST overrides
     it, and the container manifests (fly.toml, k8s configmap) set 0.0.0.0
     explicitly so the tighter default cannot break deployed probes/traffic.
"""

import os
import subprocess
import sys

import pytest

_OP_TOKEN = "test-op-secret-hp2"
_SERVER_PY = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "hermes_trader", "server.py",
)
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_FLY_TOML = os.path.join(_REPO_ROOT, "fly.toml")
_K8S_CM = os.path.join(_REPO_ROOT, "k8s", "configmap.yaml")


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setenv("HERMES_OPERATOR_TOKEN", _OP_TOKEN)
    monkeypatch.setenv("HERMES_STOP_WAIT_S", "0")
    from hermes_trader import dashboard
    monkeypatch.setattr(dashboard, "_WRITE_RATE_MAX", 1000)
    dashboard._write_hits.clear()
    dashboard._auth_failures.clear()
    from fastapi.testclient import TestClient
    from hermes_trader.server import app
    return TestClient(app, raise_server_exceptions=False)


def _auth():
    return {"Authorization": f"Bearer {_OP_TOKEN}", "X-Confirm-Stop": "confirm"}


# ── 5. honest agent_stop state ────────────────────────────────────────────────

def test_stop_reports_stopped_only_after_process_exits(client, monkeypatch, tmp_path):
    from hermes_trader import server
    pid_file = tmp_path / "agent.pid"
    pid_file.write_text("4321")
    monkeypatch.setattr(server, "PID_FILE", str(pid_file))

    killed = []
    monkeypatch.setattr(os, "kill", lambda pid, sig: killed.append((pid, sig)))

    calls = {"n": 0}

    def _fake_alive(pid):
        # Handler entry check sees a live process; every check afterwards
        # (poll loop + post-loop confirmation) sees it gone.
        calls["n"] += 1
        return calls["n"] == 1

    monkeypatch.setattr(server, "_is_alive", _fake_alive)

    r = client.post("/api/agent/stop", headers=_auth())
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "stopped"
    assert body["pid"] == 4321
    assert killed == [(4321, 15)]  # SIGTERM delivered exactly once
    assert not pid_file.exists()   # pid file cleaned up only once dead


def test_stop_surviving_process_is_503_stop_failed_and_keeps_pidfile(client, monkeypatch, tmp_path):
    from hermes_trader import server
    pid_file = tmp_path / "agent.pid"
    pid_file.write_text("4322")
    monkeypatch.setattr(server, "PID_FILE", str(pid_file))

    killed = []
    monkeypatch.setattr(os, "kill", lambda pid, sig: killed.append((pid, sig)))
    monkeypatch.setattr(server, "_is_alive", lambda pid: True)  # never exits

    r = client.post("/api/agent/stop", headers=_auth())
    assert r.status_code == 503, r.text
    body = r.json()
    assert body["status"] == "stop_failed"
    assert body["pid"] == 4322
    assert "SIGTERM" in body["detail"]
    assert killed == [(4322, 15)]
    assert pid_file.exists()  # evidence retained for SIGKILL escalation


def test_stop_signal_delivery_error_when_still_alive_is_503(client, monkeypatch, tmp_path):
    from hermes_trader import server
    pid_file = tmp_path / "agent.pid"
    pid_file.write_text("4323")
    monkeypatch.setattr(server, "PID_FILE", str(pid_file))

    killed = []

    def _kill(pid, sig):
        killed.append((pid, sig))
        raise OSError("EPERM")

    monkeypatch.setattr(os, "kill", _kill)
    monkeypatch.setattr(server, "_is_alive", lambda pid: True)

    r = client.post("/api/agent/stop", headers=_auth())
    assert r.status_code == 503, r.text
    assert r.json()["status"] == "stop_failed"
    # SIGTERM delivery was actually attempted once before the failure surfaced.
    assert killed == [(4323, 15)]
    assert pid_file.exists()


# ── 6. docs disabled by default ───────────────────────────────────────────────

def test_docs_disabled_by_default(client):
    from hermes_trader.server import app
    assert app.docs_url is None
    assert app.redoc_url is None
    assert app.openapi_url is None
    for path in ("/docs", "/redoc", "/openapi.json"):
        r = client.get(path)
        assert r.status_code == 404, (path, r.status_code)


def test_docs_enabled_when_env_set():
    # The app object is a process-wide singleton, so verify the env-enabled
    # branch in a fresh subprocess importing the production module.
    code = (
        "from hermes_trader.server import app;"
        "assert app.docs_url == '/docs', app.docs_url;"
        "assert app.redoc_url == '/redoc';"
        "assert app.openapi_url == '/openapi.json';"
        "print('docs-on')"
    )
    env = dict(os.environ)
    env["HERMES_EXPOSE_DOCS"] = "1"
    env["PYTHONPATH"] = _REPO_ROOT
    proc = subprocess.run(
        [sys.executable, "-c", code],
        cwd=_REPO_ROOT, env=env, capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    assert "docs-on" in proc.stdout


# ── source guards ─────────────────────────────────────────────────────────────

def test_source_agent_stop_honest_state_and_host_default():
    src = open(_SERVER_PY, encoding="utf-8").read()
    # 5. honest stop: verify-then-report, no unconditional "stopped" return.
    assert '"stop_failed"' in src
    assert "HERMES_STOP_WAIT_S" in src
    assert "still alive after SIGTERM" in src
    i_verify = src.index("_deadline")
    i_stopped = src.index('"status": "stopped"')
    i_failed = src.index('"status": "stop_failed"')
    assert i_verify < i_stopped < i_failed
    # 7. loopback default, no hard-coded 0.0.0.0 bind.
    assert 'os.environ.get("HERMES_HOST", "127.0.0.1")' in src
    assert 'host="0.0.0.0"' not in src
    # 6. docs gated.
    assert "HERMES_EXPOSE_DOCS" in src
    assert 'docs_url="/docs" if _expose_docs else None' in src


def test_container_manifests_explicitly_bind_all_interfaces():
    fly = open(_FLY_TOML, encoding="utf-8").read()
    assert 'HERMES_HOST = "0.0.0.0"' in fly
    cm = open(_K8S_CM, encoding="utf-8").read()
    assert 'HERMES_HOST: "0.0.0.0"' in cm
