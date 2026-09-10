"""Batch H-P3 guards — observability of fund-safety alert/audit failures.

The trading paths deliberately keep Feishu alerts and audit forks best-effort
(a notification/disk failure must never block or alter a trade decision). The
defect was that their failures were swallowed by bare ``except: pass``, so a
broken webhook or audit store could silence the ONLY signal for a naked
position / orphan fill / blind breaker with zero trace. These guards lock:

  8. MCP write-side audit helper ``_mcp_audit`` logs on append raising OR on
     append returning False, yet never raises into the tool response; the 9
     historical bare-pass audit sites in hermes-mcp-server.py are gone.
  9. Fund-safety alert/audit swallow sites across executor / server / exchange
     / memory / risk_gates / dashboard / session_log now log on failure
     (source-level), without changing best-effort semantics.

risk_gates breaker read failure stays fail-OPEN by explicit C1 decision
(covered by existing gate tests) — this batch only makes its alert/feed
failure visible; it does NOT flip the posture.
"""

from __future__ import annotations

import importlib.util
import logging
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
MCP_PY = SCRIPTS / "hermes-mcp-server.py"
EXECUTOR_PY = ROOT / "hermes_trader" / "agents" / "executor.py"
SERVER_PY = ROOT / "hermes_trader" / "server.py"
EXCHANGE_PY = ROOT / "hermes_trader" / "client" / "exchange.py"
MEMORY_PY = ROOT / "hermes_trader" / "agents" / "memory.py"
RISK_GATES_PY = ROOT / "hermes_trader" / "agents" / "risk_gates.py"
DSL_EXIT_PY = ROOT / "hermes_trader" / "agents" / "dsl_exit.py"
DASHBOARD_PY = ROOT / "hermes_trader" / "dashboard.py"
SESSION_LOG_PY = ROOT / "hermes_trader" / "session_log.py"


def _load_mcp():
    sys.path.insert(0, str(SCRIPTS))
    try:
        spec = importlib.util.spec_from_file_location(
            "hermes_mcp_server_batch_hp3", MCP_PY)
        mcp = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mcp)
        mcp._HERMES_MCP_ALLOW_WRITE = True
        return mcp
    finally:
        try:
            sys.path.remove(str(SCRIPTS))
        except ValueError:
            pass


# ── 8. MCP _mcp_audit behaviour ───────────────────────────────────────────────

def test_mcp_audit_logs_when_append_raises(monkeypatch, caplog):
    mcp = _load_mcp()
    import hermes_trader.event_log as event_log

    def _boom(*a, **k):
        raise OSError("disk full")
    monkeypatch.setattr(event_log, "append", _boom)
    with caplog.at_level(logging.ERROR, logger="hermes-mcp"):
        # Must NOT raise — audit failure can never break the tool response.
        mcp._mcp_audit("cancel_order", oid=7, ok=False)
    msgs = [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]
    assert any("audit append failed" in m and "cancel_order" in m for m in msgs), msgs


def test_mcp_audit_logs_when_append_returns_false(monkeypatch, caplog):
    mcp = _load_mcp()
    import hermes_trader.event_log as event_log
    captured = {}

    def _append(event, payload=None):
        captured["event"] = event
        captured["payload"] = payload
        return False  # undurable write
    monkeypatch.setattr(event_log, "append", _append)
    with caplog.at_level(logging.ERROR, logger="hermes-mcp"):
        mcp._mcp_audit("set_leverage", coin="ETH", ok=True)
    # action/via injected, caller payload preserved.
    assert captured["event"] == "operator_action"
    assert captured["payload"]["action"] == "set_leverage"
    assert captured["payload"]["via"] == "mcp"
    assert captured["payload"]["coin"] == "ETH"
    msgs = [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]
    assert any("returned False" in m and "set_leverage" in m for m in msgs), msgs


def test_mcp_audit_silent_on_success(monkeypatch, caplog):
    mcp = _load_mcp()
    import hermes_trader.event_log as event_log
    monkeypatch.setattr(event_log, "append", lambda *a, **k: True)
    with caplog.at_level(logging.ERROR, logger="hermes-mcp"):
        mcp._mcp_audit("close_position", coin="BTC")
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


def test_mcp_handlers_route_through_audit_helper_no_bare_pass():
    src = MCP_PY.read_text(encoding="utf-8")
    # All 9 historical audit sites now funnel through the helper.
    assert src.count("_mcp_audit(") >= 9
    # No write-side operator_action append is left wrapped in a bare pass.
    # Any remaining 'except Exception:' immediately followed by 'pass' in the
    # file would be a swallowed failure — there should be none in audit blocks.
    lines = src.splitlines()
    bad = []
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("except Exception") and i + 1 < len(lines) \
                and lines[i + 1].strip() == "pass":
            bad.append(i + 1)
    assert bad == [], f"bare except/pass remains in MCP at lines {bad}"


# ── 9. fund-safety alert/audit sites log on failure ───────────────────────────

def test_executor_fund_safety_alerts_log_on_failure():
    src = EXECUTOR_PY.read_text(encoding="utf-8")
    # 16 risk alerts + 4 breaker metric guards converted in this batch.
    assert src.count("fund-safety risk alert failed") == 16
    assert src.count("TRADE_CIRCUIT_TRIPS metric failed") == 4


def test_server_cancel_and_place_alerts_log_on_failure():
    src = SERVER_PY.read_text(encoding="utf-8")
    # H-P3 follow-up: the two cancel audit sites now funnel through the shared
    # _http_operator_audit helper (which logs itself); the Feishu risk-alert
    # except sites remain inline and still log.
    assert src.count("def _http_operator_audit(") == 1
    assert src.count("_http_operator_audit(") >= 3  # 1 def + 2 cancel call sites
    assert src.count("[cancel-order] blocked-cancel risk alert failed") == 1
    assert src.count("[place-order] orphan-fill risk alert failed") == 1
    assert src.count("[place-order] unresolved-order risk alert failed") == 1


def test_exchange_memory_riskgates_dashboard_session_log_sites_log():
    exchange = EXCHANGE_PY.read_text(encoding="utf-8")
    assert "residual-trigger risk alert failed" in exchange

    mem = MEMORY_PY.read_text(encoding="utf-8")
    assert "corrupt-memory danger card failed" in mem
    assert "MEMORY_CORRUPT_ISOLATIONS metric inc failed" in mem

    rg = RISK_GATES_PY.read_text(encoding="utf-8")
    assert "blind-gate Feishu alert failed" in rg
    assert "blind-gate session_log mirror failed" in rg
    assert "MEMORY_GATE_READ_ERRORS metric failed" in rg
    # The C1 fail-open posture is unchanged (not flipped to fail-closed).
    assert 'return {"pass": True}' in rg

    dsl = DSL_EXIT_PY.read_text(encoding="utf-8")
    assert "backfill missing-SL risk alert failed" in dsl
    assert "corrupt-state danger card failed" in dsl
    assert "state-save-failed danger card failed" in dsl
    assert "DSL_STATE_CORRUPT_ISOLATIONS metric inc failed" in dsl

    dash = DASHBOARD_PY.read_text(encoding="utf-8")
    assert "operator_action append failed" in dash

    sl = SESSION_LOG_PY.read_text(encoding="utf-8")
    assert "fork_from_session failed" in sl


def test_risk_gates_blind_alert_failure_does_not_change_gate_result(monkeypatch, caplog):
    """Behavioural: even if both the Feishu card and the SSE mirror throw,
    _alert_memory_gate_blind must not raise (the gate result is decided by the
    caller) and the failures are logged."""
    from hermes_trader.agents import risk_gates

    class _Ctx:
        coin = "DOGE"

    import hermes_trader.notify as notify
    import hermes_trader.session_log as session_log

    def _boom_card(*a, **k):
        raise RuntimeError("feishu down")

    def _boom_append(*a, **k):
        raise OSError("session log down")

    monkeypatch.setattr(notify, "send_card", _boom_card)
    monkeypatch.setattr(session_log, "append", _boom_append)

    with caplog.at_level(logging.ERROR):
        # Must return None / not raise.
        assert risk_gates._alert_memory_gate_blind("coin_circuit", _Ctx(), RuntimeError("x")) is None
    joined = " ".join(r.getMessage() for r in caplog.records)
    assert "blind-gate Feishu alert failed" in joined
    assert "blind-gate session_log mirror failed" in joined


def test_session_log_fork_failure_is_logged_not_swallowed(monkeypatch, tmp_path, caplog):
    from hermes_trader import session_log

    monkeypatch.setenv("SESSION_LOG_ROTATE_DISABLED", "1")
    monkeypatch.setattr(session_log, "_SESSION_LOG_ROTATE_DISABLED", True)
    monkeypatch.setattr(session_log, "SESSION_LOG_FILE", str(tmp_path / "sess.jsonl"))
    # Make the events fork raise.
    import hermes_trader.event_log as event_log
    monkeypatch.setattr(event_log, "fork_from_session", lambda rec: (_ for _ in ()).throw(OSError("x")))
    # Neutralise dispatch so only the fork path matters.
    import hermes_trader.notify_dispatch as nd
    monkeypatch.setattr(nd, "dispatch", lambda rec: None)

    with caplog.at_level(logging.ERROR):
        session_log.append({"event": "execute", "coin": "BTC"})  # must not raise
    assert any("fork_from_session failed" in r.getMessage() for r in caplog.records)


# ── H-P3 follow-up: HTTP audit helper + cross-ingress error_code ──────────────

def test_http_audit_logs_when_append_raises(monkeypatch, caplog):
    from hermes_trader import server
    import hermes_trader.event_log as event_log

    def _boom(*a, **k):
        raise OSError("disk full")
    monkeypatch.setattr(event_log, "append", _boom)
    with caplog.at_level(logging.ERROR, logger="hermes-server"):
        # Must NOT raise — audit failure can never break the API response.
        server._http_operator_audit("cancel_order", oid=9)
    msgs = [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]
    assert any("audit append failed" in m and "cancel_order" in m for m in msgs), msgs


def test_http_audit_logs_when_append_returns_false(monkeypatch, caplog):
    from hermes_trader import server
    import hermes_trader.event_log as event_log
    captured = {}

    def _append(event, payload=None):
        captured["event"] = event
        captured["payload"] = payload
        return False
    monkeypatch.setattr(event_log, "append", _append)
    with caplog.at_level(logging.ERROR, logger="hermes-server"):
        server._http_operator_audit("cancel_order_blocked", oid=5, bracket="sl")
    assert captured["event"] == "operator_action"
    # action/via injected, caller payload preserved.
    assert captured["payload"]["action"] == "cancel_order_blocked"
    assert captured["payload"]["via"] == "http"
    assert captured["payload"]["bracket"] == "sl"
    msgs = [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]
    assert any("returned False" in m and "cancel_order_blocked" in m for m in msgs), msgs


def test_http_audit_silent_on_success(monkeypatch, caplog):
    from hermes_trader import server
    import hermes_trader.event_log as event_log
    monkeypatch.setattr(event_log, "append", lambda *a, **k: True)
    with caplog.at_level(logging.ERROR, logger="hermes-server"):
        server._http_operator_audit("cancel_order", oid=3)
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


def test_executor_reason_to_error_code_mapping():
    from hermes_trader.agents.executor import error_code_for_reason as f
    assert f("pre_place_recheck_failed") == "pre_place_recheck_failed"
    assert f("no_atr_no_stop (ETH: insufficient candles)") == "atr_unavailable"
    assert f("equity_unavailable (live account state returned 0)") == \
        "account_state_unavailable"
    # Normal skips / non-strings map to nothing.
    assert f("mode_off") is None
    assert f("already_executed") is None
    assert f(None) is None
    assert f(123) is None
    # Prefix must not match a similarly-prefixed unrelated reason.
    assert f("no_atr_no_stop_extra") is None


def test_mcp_execute_annotates_fund_safety_refusal_with_error_code(monkeypatch):
    """MCP execute must surface the SAME stable code the HTTP API uses for an
    executor fund-safety refusal, without dropping the original reason."""
    mcp = _load_mcp()
    from hermes_trader.agents import memory

    _analysis = {"id": "ec-1", "coin": "ETH", "verdict": "LONG"}

    class _Mem:
        def get_recent_analyses(self, n):
            return [_analysis]

    monkeypatch.setattr(memory, "memory", _Mem())

    import hermes_trader.agents.executor as executor
    monkeypatch.setattr(
        executor, "maybe_execute",
        lambda a: {"executed": False, "mode": "live", "analysis_id": "ec-1",
                   "reason": "no_atr_no_stop (ETH: insufficient candles)"})
    import json
    out = json.loads(mcp.handle_execute({"analysisId": "ec-1"}))
    assert out["error_code"] == "atr_unavailable"
    assert out["reason"].startswith("no_atr_no_stop")

    # An executed / normal-skip outcome carries no error_code.
    monkeypatch.setattr(
        executor, "maybe_execute",
        lambda a: {"executed": False, "mode": "live", "reason": "mode_off"})
    out2 = json.loads(mcp.handle_execute({"analysisId": "ec-1"}))
    assert "error_code" not in out2
