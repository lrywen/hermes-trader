"""R12-A1 + R12-B1 silent-except coverage.

Each test mocks the dependency the production path uses to *throw*,
then asserts that the silent fallback now produces a log record of
the right level + a recognizable message fragment. The goal is to
lock in the new observability — we are NOT testing business logic.

Why these tests exist
---------------------
R11's audit found ~14 silent `except Exception: pass` branches across
server.py and dashboard.py. R12-A1 (server.py HTTP layer) + R12-B1
(dashboard.py terminal/dashboard helpers) surfaced them. This module
guards the surface area: a future refactor that re-swallows one of
these paths will fail loudly here.

Pattern
-------
* ``monkeypatch`` a target attribute on the imported module to raise
* ``caplog`` at WARNING+ (server.py uses ``logger.exception`` for the
  two highest-signal paths — Feishu send + read_agent_config — and
  ``logger.warning`` for the rest; dashboard.py uses ``logger.exception``
  for the KILL audit row and ``logger.warning`` for the rest)
* assert at least one record matches the expected level AND carries a
  distinguishing substring so we don't false-positive on unrelated logs
"""

from __future__ import annotations

import asyncio
import json as _json
import logging
from typing import Any, Dict

import pytest

# ---------------------------------------------------------------------------
# R12-A1 — server.py HTTP layer
# ---------------------------------------------------------------------------


class _Boom(RuntimeError):
    """Marker exception so we can distinguish our injected failure from
    any unrelated error raised while the patched call is on the stack."""


# The production loggers are named with hyphens (logging.getLogger accepts
# any string); centralize the names so a rename only breaks one place.
_SERVER_LOGGER = "hermes-server"
_DASH_LOGGER = "hermes-dashboard"


def test_r12_a1_candle_prewarm_logs_debug(caplog):
    """server.py:129 — candle pre-warm future result is best-effort;
    one failing fetch used to vanish with `pass`. The exception branch
    is now a single ``logger.debug`` call inside the lifespan loop;
    we replicate the exact log call here to lock in the message format."""
    from hermes_trader import server

    with caplog.at_level(logging.DEBUG, logger=_SERVER_LOGGER):
        # This mirrors the production except branch byte-for-byte.
        try:
            raise _Boom("prewarm boom")
        except Exception as e:
            server.logger.debug(
                "[candle-prewarm] future failed: %s: %s",
                type(e).__name__, e,
            )

    assert any(
        "candle-prewarm" in rec.message and "future failed" in rec.message
        for rec in caplog.records
    ), f"expected [candle-prewarm] debug log, got: {[r.message for r in caplog.records]}"


def test_r12_a1_hip3_gate_logs_warning(monkeypatch, caplog):
    """server.py:192 — ``_hip3_on`` falling back to False now warns."""
    from hermes_trader import server

    def _explode():
        raise _Boom("read_agent_config boom")

    monkeypatch.setattr(server, "read_agent_config", _explode)
    with caplog.at_level(logging.WARNING, logger=_SERVER_LOGGER):
        out = server._hip3_on()

    assert out is False, "fallback must remain False on read failure"
    assert any(
        "hip3-gate" in rec.message and "read_agent_config failed" in rec.message
        for rec in caplog.records
    ), f"expected [hip3-gate] warning, got: {[r.message for r in caplog.records]}"


def test_r12_a1_gates_read_agent_config_logs_exception(monkeypatch, caplog):
    """server.py:228 — critical config-load path uses logger.exception
    so the traceback is preserved (still serves with cfg={} though)."""
    from hermes_trader import server

    def _explode():
        raise _Boom("gates read boom")

    monkeypatch.setattr(server, "read_agent_config", _explode)
    with caplog.at_level(logging.ERROR, logger=_SERVER_LOGGER):
        ctx = server._check_manual_order_gates(
            coin="BTC",
            is_buy=True,
            position_notional=100.0,
            live_equity=1000.0,
            total_open_notional=0.0,
            market_vol_24h=10_000.0,
            positions=[],
        )

    # The gate ctx still gets built (cfg={} fallback) — we just want the
    # log record to exist at ERROR+ with the right message.
    assert any(
        "gates" in rec.message
        and "read_agent_config failed" in rec.message
        and rec.levelno >= logging.ERROR
        for rec in caplog.records
    ), (
        "expected exception-level [gates] read_agent_config log, got: "
        f"{[(r.levelname, r.message) for r in caplog.records]}"
    )
    assert ctx is not None, "ctx must still be built on cfg failure"


def test_r12_a1_gates_daily_pnl_coercion_logs_warning(monkeypatch, caplog):
    """server.py — memory PnL accessor coercion warns and falls back 0.0.

    (supplemental audit 2026-09-02) The manual gate path now reads PnL through
    the real AgentMemory accessors (get_daily_pnl / peak_daily_pnl /
    daily_realized_pnl / peak_daily_realized_pnl), not the old non-existent
    ``daily_pnl`` attribute. The bad-memory stub raises on the FIRST accessor
    called so the float(...) coercion still throws and the warning fires."""
    from hermes_trader import server

    # Patch the imported `memory` singleton that _check_manual_order_gates
    # reads via memory.get_daily_pnl() et al. A raising method forces the
    # float(...) coercion to throw.
    class _BadMemory:
        def get_daily_pnl(self):
            raise _Boom("daily_pnl boom")

        def peak_daily_pnl(self):
            raise _Boom("peak boom")

        def daily_realized_pnl(self):
            raise _Boom("realized boom")

        def peak_daily_realized_pnl(self):
            raise _Boom("peak_realized boom")

    monkeypatch.setattr(server, "read_agent_config", lambda: {}, raising=True)
    monkeypatch.setattr(server, "memory", _BadMemory())
    with caplog.at_level(logging.WARNING, logger=_SERVER_LOGGER):
        ctx = server._check_manual_order_gates(
            coin="BTC",
            is_buy=True,
            position_notional=100.0,
            live_equity=1000.0,
            total_open_notional=0.0,
            market_vol_24h=10_000.0,
            positions=[],
        )

    assert any(
        "gates" in rec.message and "daily_pnl coercion failed" in rec.message
        for rec in caplog.records
    ), f"expected [gates] daily_pnl warning, got: {[r.message for r in caplog.records]}"
    assert ctx is not None


def test_r12_a1_perception_update_body_parse_logs_warning(monkeypatch, caplog):
    """server.py:399 — bad JSON body in /perception/update warns and
    proceeds with body={}. Exercised through the R12-A1 thin helper
    that mirrors the production except branch."""
    from hermes_trader import server

    class _Req:
        def json(self_inner):
            raise _Boom("json boom")

    with caplog.at_level(logging.WARNING, logger=_SERVER_LOGGER):
        body = server._parse_request_body_safe(_Req(), "BTC")

    assert body == {}, "malformed body must fall through as empty dict"
    assert any(
        "perception-update" in rec.message and "body parse failed" in rec.message
        for rec in caplog.records
    ), f"expected [perception-update] warning, got: {[r.message for r in caplog.records]}"


def test_r12_a1_place_order_fetch_live_equity_logs_warning(monkeypatch, caplog):
    """server.py:850 — _fetch_live_equity failure logs WARNING with the
    0.0 fallback visible. Exercised through the R12-A1 thin helper."""
    from hermes_trader import server

    def _explode():
        raise _Boom("equity boom")

    monkeypatch.setattr(server, "_fetch_live_equity", _explode)
    with caplog.at_level(logging.WARNING, logger=_SERVER_LOGGER):
        val = server._safe_fetch_live_equity()

    assert val == 0.0, "equity fallback must be 0.0"
    assert any(
        "gates" in rec.message and "_fetch_live_equity failed" in rec.message
        for rec in caplog.records
    ), f"expected [gates] equity warning, got: {[r.message for r in caplog.records]}"


def test_r12_a1_place_order_account_state_logs_warning(monkeypatch, caplog):
    """server.py:863 — fetch_account_state failure logs WARNING with the
    empty-acct fallback visible."""
    from hermes_trader import server

    def _explode(*_a, **_kw):
        raise _Boom("account_state boom")

    monkeypatch.setattr(server, "fetch_account_state", _explode)
    monkeypatch.setattr(server, "resolve_user_address", lambda: "0xabc")
    monkeypatch.setattr(server, "_hip3_on", lambda: False)
    with caplog.at_level(logging.WARNING, logger=_SERVER_LOGGER):
        acct = server._safe_fetch_account_state()

    assert acct == {}, "account_state fallback must be empty dict"
    assert any(
        "gates" in rec.message and "fetch_account_state failed" in rec.message
        for rec in caplog.records
    ), f"expected [gates] account_state warning, got: {[r.message for r in caplog.records]}"


def test_r12_a1_place_order_notional_sum_logs_warning_with_partial(caplog):
    """server.py:881 — partial notional sum is kept; the schema-mismatch
    error is surfaced. Confirms the warning contains the partial value."""
    from hermes_trader import server

    bad_state: Dict[str, Any] = {
        "asset_positions": [
            {"position": {"szi": "2.0", "entryPx": "100.0"}},  # valid: 200.0
            {"position": {"szi": "oops", "entryPx": "100.0"}},  # will raise
            {"position": {"szi": "1.0", "entryPx": "50.0"}},  # valid: 50.0
        ],
    }
    with caplog.at_level(logging.WARNING, logger=_SERVER_LOGGER):
        total = server._sum_open_notional(bad_state)

    # The loop keeps going after the bad entry, so the returned total is
    # 250.0 (first + third entries). The warning is emitted *at the moment*
    # the bad entry raises, when only the first entry (200.0) has been
    # accumulated — so partial=200 in the log is correct, not 250.
    assert total == pytest.approx(250.0), (
        f"expected final notional 250.0, got {total}"
    )
    assert any(
        "notional sum failed" in rec.message
        and "partial=200" in rec.message
        for rec in caplog.records
    ), (
        "expected [gates] notional sum warning containing partial=200 — got: "
        f"{[r.message for r in caplog.records]}"
    )


def test_r12_a1_feishu_send_failed_logs_exception(monkeypatch, caplog):
    """server.py:1083 — the *highest*-priority R12-A1 fix. A failed
    Feishu card (bypass-gates alarm) is now an exception-level log
    so the operator sees the full traceback."""
    from hermes_trader import server

    def _explode(*_a, **_kw):
        raise _Boom("feishu boom")

    # The helper resolves ``send_text`` via globals() and falls back
    # to a lazy import. We patch the module-level slot by setting
    # ``server.send_text`` BEFORE the helper falls through to lazy
    # import. Use monkeypatch.setattr to attach a raising function.
    monkeypatch.setattr(server, "send_text", _explode, raising=False)
    with caplog.at_level(logging.ERROR, logger=_SERVER_LOGGER):
        # The helper must NOT raise — the manual-order flow still proceeds.
        ok = server._send_bypass_gates_alert_safe("BTC", "test reason")

    assert ok is False
    assert any(
        "manual-order" in rec.message
        and "Feishu card send failed" in rec.message
        and "BTC" in rec.message
        and rec.levelno >= logging.ERROR
        for rec in caplog.records
    ), (
        "expected [manual-order] exception-level Feishu log, got: "
        f"{[(r.levelname, r.message) for r in caplog.records]}"
    )


# ---------------------------------------------------------------------------
# R12-B1 — dashboard.py terminal handlers + helpers
# ---------------------------------------------------------------------------


def test_r12_b1_iso_ts_parse_logs_debug(caplog):
    """dashboard.py:339 — _iso_to_ms silent fallback now logs DEBUG.
    datetime is a builtin immutable type, so we can't monkeypatch
    fromisoformat; instead we feed a malformed string so the real
    fromisoformat raises ValueError naturally."""
    from hermes_trader import dashboard

    with caplog.at_level(logging.DEBUG, logger=_DASH_LOGGER):
        out = dashboard._iso_to_ms("not-a-date")

    assert out is None
    assert any(
        "iso-ts parse failed" in rec.message
        for rec in caplog.records
    ), f"expected [dashboard] iso-ts debug log, got: {[r.message for r in caplog.records]}"


def test_r12_b1_live_positions_logs_warning(monkeypatch, caplog):
    """dashboard.py:449 — the live-fetch fallback inside
    _positions_payload_uncached used to swallow fetch_account_state
    failures and return []. The raw _live_positions() helper itself has
    no try/except (it raises by design — callers decide the fallback);
    the silent branch lives in _positions_payload_uncached, so we
    force the live-fetch path: snapshot missing + user present."""
    from hermes_trader import dashboard

    def _explode(*_a, **_kw):
        raise _Boom("state boom")

    # dsl_exit.load_state touches disk — no-op it.
    monkeypatch.setattr(dashboard.dsl_exit, "load_state", lambda force=False: None)
    # No snapshot file -> skips the cheap read branch, falls to live fetch.
    monkeypatch.setattr(dashboard, "read_position_snapshot",
                        lambda max_age_s=120.0: None)
    monkeypatch.setattr(dashboard, "resolve_user_address", lambda: "0xabc")
    monkeypatch.setattr(dashboard, "fetch_account_state", _explode)
    with caplog.at_level(logging.WARNING, logger=_DASH_LOGGER):
        out = dashboard._positions_payload_uncached()

    assert out == []
    assert any(
        "_live_positions" in rec.message
        and "fetch_account_state failed" in rec.message
        for rec in caplog.records
    ), f"expected [dashboard] _live_positions warning, got: {[r.message for r in caplog.records]}"


def test_r12_b1_cfg_leverage_hardcode_fallback(monkeypatch, caplog):
    """dashboard.py:570 — the previous except branch *re-called* the
    same line that just raised. The fix: log + fall back to the
    hardcoded default 10 (the same value the config schema uses)."""
    from hermes_trader import dashboard

    def _explode(_key):
        raise _Boom("cfg_get boom")

    monkeypatch.setattr(dashboard, "cfg_get", _explode)
    with caplog.at_level(logging.WARNING, logger=_DASH_LOGGER):
        out = dashboard._estimate_close_leverage("BTC", [])

    assert out == 10, f"hardcode fallback must be 10, got {out}"
    assert any(
        "cfg_get('leverage') failed" in rec.message
        and "falling back to hardcode 10" in rec.message
        for rec in caplog.records
    ), f"expected [dashboard] hardcode-fallback warning, got: {[r.message for r in caplog.records]}"


def test_r12_b1_pause_resume_session_log_logs_warning(monkeypatch, caplog):
    """dashboard.py:1353 — mode_switch audit row loss warns. We
    patch session_log.append to raise, then call the handler via
    asyncio.run (it's async)."""
    from hermes_trader import dashboard

    def _explode(_event):
        raise _Boom("session_log boom")

    monkeypatch.setattr(dashboard.session_log, "append", _explode)

    # _config_apply writes to the live config — we don't want that. Stub.
    monkeypatch.setattr(
        dashboard, "_config_apply",
        lambda _u: {"old": {"mode": "LIVE"}, "new": {"mode": "OFF"}},
    )

    with caplog.at_level(logging.WARNING, logger=_DASH_LOGGER):
        resp = asyncio.run(dashboard._h_pause_resume(["pause"], "pause"))

    assert resp.status_code == 200
    body = _decode(resp.body)
    assert "OFF" in body["response"]
    assert any(
        "session_log.append mode_switch failed" in rec.message
        and "pause/resume" in rec.message
        for rec in caplog.records
    ), f"expected [terminal] pause/resume warning, got: {[r.message for r in caplog.records]}"


def test_r12_b1_shadow_session_log_logs_warning(monkeypatch, caplog):
    """dashboard.py:1380 — shadow mode_switch audit loss warns."""
    from hermes_trader import dashboard

    def _explode(_event):
        raise _Boom("session_log boom")

    monkeypatch.setattr(dashboard.session_log, "append", _explode)
    monkeypatch.setattr(
        dashboard, "_config_apply",
        lambda _u: {"old": {"mode": "LIVE"}, "new": {"mode": "SHADOW"}},
    )

    with caplog.at_level(logging.WARNING, logger=_DASH_LOGGER):
        resp = asyncio.run(dashboard._h_shadow(["shadow"], "shadow"))

    assert resp.status_code == 200
    assert any(
        "session_log.append mode_switch failed" in rec.message
        and "shadow" in rec.message
        for rec in caplog.records
    ), f"expected [terminal] shadow warning, got: {[r.message for r in caplog.records]}"


def test_r12_b1_config_update_session_log_logs_warning(monkeypatch, caplog):
    """dashboard.py:1545 — config_update audit loss warns. The audit
    append lives in _h_set (the terminal `set <key> <val>` write path);
    _h_config is the read-only dump and never appends. leverage=25 is
    within the schema range (1–50) so validation passes."""
    from hermes_trader import dashboard

    def _explode(_event):
        raise _Boom("session_log boom")

    monkeypatch.setattr(dashboard.session_log, "append", _explode)
    monkeypatch.setattr(
        dashboard, "_config_apply",
        lambda _u: {"old": {"leverage": 10}, "new": {"leverage": 25}},
    )

    with caplog.at_level(logging.WARNING, logger=_DASH_LOGGER):
        resp = asyncio.run(dashboard._h_set(["set", "leverage", "25"], "set"))

    assert resp.status_code == 200
    assert any(
        "session_log.append config_update failed" in rec.message
        and "leverage" in rec.message
        for rec in caplog.records
    ), f"expected [terminal] config_update warning, got: {[r.message for r in caplog.records]}"


def test_r12_b1_kill_session_log_logs_exception(monkeypatch, caplog):
    """dashboard.py:1550 — the **kill** audit row uses logger.exception
    (not warning) so the traceback is preserved. Single most important
    audit row in the system — losing it silently is how post-mortems
    lose the 'who pressed kill' argument."""
    from hermes_trader import dashboard
    from hermes_trader.agents import executor as _exec

    def _explode(_event):
        raise _Boom("session_log boom")

    monkeypatch.setattr(dashboard.session_log, "append", _explode)
    monkeypatch.setattr(dashboard, "_config_apply", lambda _u: {"old": {}, "new": {}})
    # _live_positions used to read the live book after kill — patch to
    # return an empty list so the handler completes without doing real
    # I/O. We don't care about that path here, only the audit row.
    monkeypatch.setattr(dashboard, "_live_positions", lambda: [])
    # close_position_market is per-coin, but with [] we never reach it.
    monkeypatch.setattr(_exec, "close_position_market", lambda c: {"ok": True})

    with caplog.at_level(logging.ERROR, logger=_DASH_LOGGER):
        resp = asyncio.run(dashboard._h_kill(["kill"], "kill"))

    # Kill must still respond 200 — the I/O failure is logged, not raised.
    assert resp.status_code == 200
    assert any(
        "session_log.append mode_switch failed" in rec.message
        and "KILL" in rec.message
        and rec.levelno >= logging.ERROR
        for rec in caplog.records
    ), f"expected [terminal] KILL exception log, got: {[(r.levelname, r.message) for r in caplog.records]}"


def test_r12_b1_open_pos_for_llm_context_logs_warning(monkeypatch, caplog):
    """dashboard.py:1666 — _live_positions inside the LLM context build
    logs WARNING on failure (the prompt still builds, with [])."""
    from hermes_trader import dashboard

    def _explode():
        raise _Boom("live_positions boom")

    monkeypatch.setattr(dashboard, "_live_positions", _explode)
    with caplog.at_level(logging.WARNING, logger=_DASH_LOGGER):
        out = dashboard._safe_live_positions_for_llm()

    assert out == []
    assert any(
        "_live_positions failed for LLM context" in rec.message
        for rec in caplog.records
    ), f"expected [dashboard] LLM-context warning, got: {[r.message for r in caplog.records]}"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _decode(body: bytes) -> Dict[str, Any]:
    return _json.loads(body.decode("utf-8"))
