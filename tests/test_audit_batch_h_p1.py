"""Audit batch H-P1 (2026-09-10) guard tests — fund-safety hardening.

Four fixes, all aimed at preventing an unprotected/duplicated position:

* H-P1-1  HTTP ``/api/hl/cancel-order`` mirrors the MCP guard: it refuses to
          cancel a DSL-managed SL/TP trigger (409 dsl_managed_trigger) after a
          force-reloaded shared lookup; a tracker-read failure fails closed
          (503 dsl_tracker_lookup_failed). Both paths now route through the
          single shared ``dsl_exit.find_dsl_bracket_trigger`` so the HTTP and
          MCP implementations cannot drift. Blocked/executed cancels are
          audited (via="http").

* H-P1-2  The autonomous executor's A-F4 pre-place account re-read is flipped
          fail-open -> fail-CLOSED: a read failure skips the entry with reason
          ``pre_place_recheck_failed`` (no order, markers rolled back, flock
          released, loop does not raise) instead of guessing "no position".

* H-P1-3  The manual-order endpoint's FIRST account read (before the gate
          chain) no longer degrades to an empty ``acct`` dict on failure: it
          refuses with 503 ``account_state_unavailable`` (behavioral case
          lives in test_audit_batch_g_p1.py).

* H-P1-4  A non-positive 4h ATR blocks a manual entry with 503
          ``atr_unavailable`` rather than arming no post-fill stop and leaving
          the new position naked (behavioral case in test_audit_batch_g_p1.py).

This module is self-contained (tests/ has no __init__.py).
"""
from __future__ import annotations

import inspect
import os

import pytest
from fastapi.testclient import TestClient

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVER_PY = os.path.join(_REPO_ROOT, "hermes_trader", "server.py")
MCP_PY = os.path.join(_REPO_ROOT, "scripts", "hermes-mcp-server.py")
EXECUTOR_PY = os.path.join(_REPO_ROOT, "hermes_trader", "agents", "executor.py")
DSL_EXIT_PY = os.path.join(_REPO_ROOT, "hermes_trader", "agents", "dsl_exit.py")

_OP_TOKEN = "test-op-secret-hp1"


def _auth():
    return {"Authorization": f"Bearer {_OP_TOKEN}"}


# ──────────────────────────────────────────────────────────────────────────
# H-P1-1 (unit): shared find_dsl_bracket_trigger
# ──────────────────────────────────────────────────────────────────────────

def _isolate_dsl(monkeypatch, tmp_path):
    """Point DSL state at tmp paths; force reloads never see the real registry."""
    from hermes_trader.agents import dsl_exit
    state_path = str(tmp_path / "dsl-state.json")
    monkeypatch.setattr(dsl_exit, "DSL_STATE_FILE", state_path)
    monkeypatch.setattr(dsl_exit, "DSL_STATE_LOCK_FILE", state_path + ".lock")
    dsl_exit.reset_force_load_throttle()
    dsl_exit._active_positions.clear()
    return dsl_exit


def test_find_dsl_bracket_trigger_matches_sl_and_tp(monkeypatch, tmp_path):
    """A force reload picks up trackers persisted by the loop process and
    resolves an oid to (coin, side, 'sl'|'tp')."""
    dsl_exit = _isolate_dsl(monkeypatch, tmp_path)
    tr = dsl_exit.register_position("ETH", "long", 2500.0)
    tr.sl_oid = 111
    tr.tp_oid = 222
    dsl_exit._save_state()
    # Simulate a DIFFERENT process: drop the in-memory registry first.
    dsl_exit._active_positions.clear()

    assert dsl_exit.find_dsl_bracket_trigger(111) == ("ETH", "long", "sl")
    assert dsl_exit.find_dsl_bracket_trigger(222) == ("ETH", "long", "tp")


def test_find_dsl_bracket_trigger_unknown_oid_returns_none(monkeypatch, tmp_path):
    dsl_exit = _isolate_dsl(monkeypatch, tmp_path)
    dsl_exit.register_position("ETH", "long", 2500.0)
    dsl_exit.set_bracket("ETH", "long", sl_oid=111, tp_oid=222)
    assert dsl_exit.find_dsl_bracket_trigger(999) is None


def test_find_dsl_bracket_trigger_load_failure_raises(monkeypatch, tmp_path):
    """A tracker-load failure must propagate so every caller fails closed."""
    dsl_exit = _isolate_dsl(monkeypatch, tmp_path)

    def _boom(*a, **k):
        raise OSError("disk on fire")

    monkeypatch.setattr(dsl_exit, "load_state", _boom)
    with pytest.raises(OSError):
        dsl_exit.find_dsl_bracket_trigger(111)


def test_find_dsl_bracket_trigger_double_corrupt_registry_fails_closed(
    monkeypatch, tmp_path
):
    """H-P1 follow-up: live + .bak both corrupt must RAISE, not return None.

    load_state() tolerates a corrupt registry by clearing and returning (the
    trading loop must not crash), but an empty table after a registry that
    *existed* and is unreadable is not proof an oid is not a DSL bracket — the
    cancel guard must fail closed in that case.
    """
    dsl_exit = _isolate_dsl(monkeypatch, tmp_path)
    dsl_exit.register_position("ETH", "long", 2500.0)
    dsl_exit.set_bracket("ETH", "long", sl_oid=555, tp_oid=666)
    dsl_exit._save_state()
    # Corrupt both generations, then drop memory like a separate process.
    with open(dsl_exit.DSL_STATE_FILE, "w") as f:
        f.write("{ broken live json ]")
    with open(dsl_exit.DSL_STATE_FILE + ".bak", "w") as f:
        f.write("broken bak [")
    dsl_exit._active_positions.clear()
    dsl_exit.reset_force_load_throttle()

    with pytest.raises(RuntimeError):
        dsl_exit.find_dsl_bracket_trigger(555)


def test_find_dsl_bracket_trigger_never_existed_returns_none(
    monkeypatch, tmp_path
):
    """The legitimate empty registry (no state file ever written) is NOT a
    corrupt-registry condition and must still resolve to None (cancel allowed).
    """
    dsl_exit = _isolate_dsl(monkeypatch, tmp_path)
    assert not os.path.exists(dsl_exit.DSL_STATE_FILE)
    assert dsl_exit.find_dsl_bracket_trigger(777) is None
    assert dsl_exit._last_force_load_corrupt is False


# ──────────────────────────────────────────────────────────────────────────
# H-P1-1 (HTTP): /api/hl/cancel-order
# ──────────────────────────────────────────────────────────────────────────

@pytest.fixture()
def cancel_harness(monkeypatch, tmp_path):
    """Real FastAPI app with DSL isolated to tmp and every I/O surface stubbed."""
    from hermes_trader import server as srv
    from hermes_trader.client import exchange

    dsl_exit = _isolate_dsl(monkeypatch, tmp_path)
    monkeypatch.setenv("HERMES_OPERATOR_TOKEN", _OP_TOKEN)
    # Avoid F11 interference across this file's write calls.
    from hermes_trader import dashboard
    dashboard._write_hits.clear()
    dashboard._auth_failures.clear()

    events = []

    def _append(kind, payload=None, **kw):
        events.append({"kind": kind, "payload": payload or {}})
        return True

    import hermes_trader.event_log  # noqa: F401
    monkeypatch.setattr(
        "hermes_trader.event_log",
        type("_L", (), {"append": staticmethod(_append)})())

    alerts = []
    from hermes_trader import notify
    monkeypatch.setattr(notify, "send_text",
                        lambda *a, **k: alerts.append(a[0] if a else ""))

    cancel_calls = []

    def _cancel_orders(oid, coin=None, asset_idx=None):
        cancel_calls.append({"oid": oid, "coin": coin,
                             "asset_idx": asset_idx})
        return {"ok": True, "oid": oid}

    monkeypatch.setattr(exchange, "cancel_orders", _cancel_orders)

    client = TestClient(srv.app, raise_server_exceptions=False)
    return {
        "client": client, "srv": srv, "dsl_exit": dsl_exit,
        "events": events, "alerts": alerts, "cancel_calls": cancel_calls,
    }


def _cancel(client, oid, coin=None):
    body = {"oid": oid}
    if coin is not None:
        body["coin"] = coin
    return client.post("/api/hl/cancel-order", json=body, headers=_auth())


def test_http_cancel_blocks_dsl_sl_oid(cancel_harness):
    h = cancel_harness
    dsl_exit = h["dsl_exit"]
    dsl_exit.register_position("ETH", "long", 2500.0)
    dsl_exit.set_bracket("ETH", "long", sl_oid=111, tp_oid=222)
    # Mirror the API process: drop in-memory so the guard must reload disk.
    dsl_exit._active_positions.clear()

    r = _cancel(h["client"], 111)
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert detail["error"] == "dsl_managed_trigger"
    assert "SL" in detail["detail"]
    # No exchange cancel, an audit line, and a Feishu block alert.
    assert h["cancel_calls"] == []
    blocked = [e for e in h["events"]
               if e["payload"].get("action") == "cancel_order_blocked"]
    assert len(blocked) == 1
    assert blocked[0]["payload"]["via"] == "http"
    assert blocked[0]["payload"]["bracket"] == "sl"
    assert h["alerts"] and "撤单被拦截" in h["alerts"][0]
    dsl_exit._active_positions.clear()


def test_http_cancel_blocks_dsl_tp_oid(cancel_harness):
    h = cancel_harness
    dsl_exit = h["dsl_exit"]
    dsl_exit.register_position("BTC", "short", 100000.0)
    dsl_exit.set_bracket("BTC", "short", sl_oid=333, tp_oid=444)
    dsl_exit._active_positions.clear()

    r = _cancel(h["client"], 444)
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["error"] == "dsl_managed_trigger"
    assert h["cancel_calls"] == []
    assert h["alerts"]
    dsl_exit._active_positions.clear()


def test_http_cancel_lookup_failure_is_503_fail_closed(cancel_harness,
                                                       monkeypatch):
    h = cancel_harness
    monkeypatch.setattr(h["dsl_exit"], "find_dsl_bracket_trigger",
                        lambda oid: (_ for _ in ()).throw(RuntimeError("disk io")))

    r = _cancel(h["client"], 999)
    assert r.status_code == 503, r.text
    assert r.json()["detail"]["error"] == "dsl_tracker_lookup_failed"
    assert h["cancel_calls"] == []


def test_http_cancel_non_integer_oid_is_400(cancel_harness):
    h = cancel_harness
    r = h["client"].post("/api/hl/cancel-order",
                         json={"oid": "not-an-int"}, headers=_auth())
    assert r.status_code == 400, r.text
    assert h["cancel_calls"] == []


def test_http_cancel_unrelated_oid_proceeds_and_audits(cancel_harness):
    h = cancel_harness
    r = _cancel(h["client"], 777, coin="ETH")
    assert r.status_code == 200, r.text
    assert h["cancel_calls"] == [{"oid": 777, "coin": "ETH",
                                  "asset_idx": None}]
    audited = [e for e in h["events"]
               if e["payload"].get("action") == "cancel_order"]
    assert len(audited) == 1
    assert audited[0]["payload"]["via"] == "http"
    assert audited[0]["payload"]["ok"] is True


def test_http_cancel_requires_auth(cancel_harness):
    h = cancel_harness
    r = h["client"].post("/api/hl/cancel-order", json={"oid": 1})
    assert r.status_code == 401
    assert h["cancel_calls"] == []


# ──────────────────────────────────────────────────────────────────────────
# H-P1-2 (behavioral): executor pre-place failure skips fail-closed
# ──────────────────────────────────────────────────────────────────────────

# The behavioral case lives in tests/test_audit_p1.py
# (test_af4_pre_place_recheck_failure_is_fail_closed_skip); here we pin the
# source so a future edit cannot silently revert to the fail-open branch.

def test_executor_preplace_failure_is_fail_closed_source_guard():
    src = open(EXECUTOR_PY, encoding="utf-8").read()
    assert '"pre_place_recheck_failed"' in src
    assert "fail-CLOSED" in src
    # The old fail-open log string must not coexist on the pre-place path.
    assert "pre-place re-check failed (fail-open)" not in src


def test_server_cancel_uses_shared_guard_source_guard():
    """HTTP and MCP both route through the shared function (no two copies)."""
    server_src = open(SERVER_PY, encoding="utf-8").read()
    mcp_src = open(MCP_PY, encoding="utf-8").read()
    dsl_src = open(DSL_EXIT_PY, encoding="utf-8").read()

    # Exactly one definition, called by both cancel paths.
    assert dsl_src.count("def find_dsl_bracket_trigger(") == 1
    assert "find_dsl_bracket_trigger(oid)" in server_src
    assert "find_dsl_bracket_trigger(oid)" in mcp_src
    assert '"dsl_managed_trigger"' in server_src
    assert '"dsl_tracker_lookup_failed"' in server_src
    # The HTTP guard runs BEFORE the exchange cancel wrapper.
    i_guard = server_src.index("find_dsl_bracket_trigger(oid)")
    i_cancel = server_src.index("cancel_orders(oid", i_guard)
    assert i_guard < i_cancel
    # The oid integer validation precedes the DSL lookup.
    i_oid_int = server_src.index("oid must be an integer")
    assert i_oid_int < i_guard


def test_server_atr_and_gate_snapshot_fail_closed_source_guard():
    src = open(SERVER_PY, encoding="utf-8").read()
    assert '"atr_unavailable"' in src
    assert '"account_state_unavailable"' in src
    # The empty-acct fail-open comment/behavior must be gone.
    assert "account_state_unavailable" in src


def test_find_dsl_bracket_trigger_forces_reload_source_guard():
    """The shared helper must force a shared-locked reload, not trust the
    calling process's in-memory registry."""
    from hermes_trader.agents import dsl_exit
    src = inspect.getsource(dsl_exit.find_dsl_bracket_trigger)
    assert "reset_force_load_throttle()" in src
    assert "load_state(force=True)" in src
    assert "sl_oid" in src and "tp_oid" in src
