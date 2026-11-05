"""S5 operator intervention integration tests; no network or real orders."""
from __future__ import annotations

import math
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hermes_trader.agents import dsl_exit, manual_control, manual_execution, shadow_book
from hermes_trader.dashboard_routes.operator import register_operator_routes


@pytest.fixture(autouse=True)
def _clear_dsl_trackers():
    dsl_exit._active_positions.clear()
    yield
    dsl_exit._active_positions.clear()


@pytest.fixture()
def manual_state(tmp_path, monkeypatch):
    state_path = str(tmp_path / "manual.json")
    audit_path = str(tmp_path / "manual-audit.jsonl")
    monkeypatch.setattr(manual_control, "_state_path", lambda: state_path)
    monkeypatch.setattr(manual_control, "_audit_path", lambda: audit_path)
    return tmp_path


def _analysis(**overrides):
    analysis = {
        "id": "a1", "coin": "BTC", "verdict": "LONG", "side": "long",
        "confidence": 0.70, "composite_score": 30, "entry_px": 100,
        "stop_px": 95, "tp_px": 110, "news_context": "no news",
    }
    analysis.update(overrides)
    return analysis


def _patch_executor(monkeypatch, cfg_overrides=None, state_overrides=None):
    from hermes_trader.agents import executor

    cfg = {
        "mode": "SHADOW", "enable_crypto": True, "enable_hip3": True,
        "equity_fraction_per_trade": 0.10, "leverage": 10,
        "max_trade_notional_usd": 100000, "max_concurrent": 18,
        "max_total_notional_pct": 40.0, "max_daily_loss_usd": -1000,
        "min_available_margin_pct": 0.10, "cooldown_min": 60,
        "min_ai_confidence": 0.30, "counter_regime_min_conf": 0.65,
        "max_crypto_long_correlated": 5, "min_market_volume_usd": 5_000_000,
        "min_hip3_volume_usd": 500_000, "conviction_sizing": True,
        "dsl_exit": {"max_loss_pct": 2.0, "max_loss_roe_pct": 30.0,
                     "protect_pct": 0.5, "retrace_threshold": 0.3,
                     "hard_timeout_minutes": 180.0},
        "debate_gate": {"enabled": False},
        "daily_extension_cap": {"mode": "off"},
    }
    cfg.update(cfg_overrides or {})
    state = {"equity": 1000.0, "available": 500.0, "total_ntl": 0.0,
             "asset_positions": []}
    state.update(state_overrides or {})

    monkeypatch.setattr(executor, "read_agent_config", lambda: cfg)
    monkeypatch.setattr(executor, "resolve_user_address", lambda: "0xMASTER")
    monkeypatch.setattr(executor, "fetch_account_state", lambda u, **kw: state)
    monkeypatch.setattr(executor, "get_hl_price", lambda c: 100.0)
    monkeypatch.setattr(executor, "get_hl_atr", lambda *a, **k: 2.0)
    monkeypatch.setattr(executor, "get_max_leverage", lambda c: 40)
    monkeypatch.setattr(executor, "min_entry_notional_usd", lambda c, mid: 10.5)
    monkeypatch.setattr(executor, "entry_size_for_notional", lambda c, n, mid: n / mid)
    monkeypatch.setattr(executor, "set_leverage", lambda c, l: {"ok": True})
    monkeypatch.setattr(executor, "place_hl_trigger_order", lambda *a, **k: {"ok": True})
    monkeypatch.setattr(executor, "place_hl_order",
                        lambda *a, **k: {"ok": True, "order_id": "OID1", "avg_px": 100.0})
    monkeypatch.setattr("hermes_trader.client.hl_client._http_post",
                        lambda p, pl: {"marginSummary": {"accountValue": "500"}})
    monkeypatch.setattr("hermes_trader.agents.market_regime.detect_regime_with_score",
                        lambda c, force=False: ("neutral", 0.0))
    monkeypatch.setattr("hermes_trader.agents.hyperfeed.market_get_funding_regime",
                        lambda: {"regime": "NEUTRAL", "regimes_by_class": {}})
    monkeypatch.setattr("hermes_trader.client.price_crosscheck.crosscheck_price",
                        lambda coin, px: {"ok": True, "checked": False, "reason": "test_stub"})
    monkeypatch.setattr(executor, "register_position", lambda *a, **k: None)
    monkeypatch.setattr(executor.memory, "track_daily_pnl", lambda *a, **k: None)
    monkeypatch.setattr(executor.memory, "get_daily_pnl", lambda: 0.0)
    monkeypatch.setattr(executor.memory, "peak_equity", lambda: 0.0)
    monkeypatch.setattr(executor.memory, "rolling_peak_equity", lambda window_days=14.0: 0.0)
    monkeypatch.setattr(executor.memory, "consecutive_losses", lambda coin: 0)
    monkeypatch.setattr(executor.memory, "coin_daily_realized_pnl_pct", lambda coin, sod: 0.0)
    monkeypatch.setattr(executor.memory, "get_recent_trades", lambda n=10: [])
    monkeypatch.setattr(executor.memory, "record_trade", lambda t: None)
    monkeypatch.setenv("HYPERLIQUID_PRIVATE_KEY", "0xabc")
    return executor, cfg


def _shadow_fill(**kwargs):
    return {"opened": True, "type": "open", **kwargs}


def _confirmation(confirmation_id):
    return next(r for r in manual_control.list_confirmations()
                if r.get("id") == confirmation_id)


def test_shadow_confirmation_approval_books_once(manual_state, monkeypatch):
    executor, cfg = _patch_executor(monkeypatch)
    opens = []
    monkeypatch.setattr(manual_execution.shadow_book, "shadow_open",
                        lambda **kw: opens.append(kw) or _shadow_fill(**kw))
    manual_control.set_mode("manual_confirm", actor="test", reason="confirm flow")

    pending = executor.maybe_execute(_analysis())
    assert pending["pending_confirmation"] is True
    assert opens == []
    confirmation = _confirmation(pending["confirmation_id"])
    assert confirmation["status"] == "pending"

    approved = manual_control.decide_confirmation(
        confirmation["id"], approve=True, actor="operator", reason="approved")
    assert approved["status"] == "approved"
    outcomes = manual_execution.process_pending_commands(cfg)

    assert len(outcomes) == 1
    assert outcomes[0]["status"] == "completed", outcomes[0]["result"]
    assert _confirmation(confirmation["id"])["status"] == "completed"
    assert len(opens) == 1
    assert opens[0]["size_usd"] == pytest.approx(1000.0)
    assert manual_execution.process_pending_commands(cfg) == []
    assert len(opens) == 1


def test_approved_confirmation_reruns_hard_gates(manual_state, monkeypatch):
    executor, cfg = _patch_executor(monkeypatch)
    opens = []
    monkeypatch.setattr(manual_execution.shadow_book, "shadow_open",
                        lambda **kw: opens.append(kw) or _shadow_fill(**kw))
    manual_control.set_mode("manual_confirm", actor="test", reason="confirm flow")
    pending = executor.maybe_execute(_analysis())
    cfg["min_ai_confidence"] = 0.99
    manual_control.decide_confirmation(
        pending["confirmation_id"], approve=True, actor="operator", reason="retry gates")

    outcomes = manual_execution.process_pending_commands(cfg)
    assert outcomes[0]["status"] == "failed"
    assert outcomes[0]["result"]["executed"] is False
    assert _confirmation(pending["confirmation_id"])["status"] == "failed"
    assert opens == []


def test_reject_and_execution_time_expiry_are_fail_closed(manual_state, monkeypatch):
    rejected = manual_control.request_confirmation(_analysis(id="reject"), now=100.0)
    manual_control.decide_confirmation(
        rejected["id"], approve=False, actor="operator", reason="no", now=101.0)
    assert _confirmation(rejected["id"])["status"] == "rejected"
    with pytest.raises(ValueError):
        manual_control.decide_confirmation(
            rejected["id"], approve=True, actor="operator", reason="again", now=102.0)

    expired = manual_control.request_confirmation(
        _analysis(id="expire"), ttl_s=5, now=100.0)
    manual_control.decide_confirmation(
        expired["id"], approve=True, actor="operator", reason="late", now=104.0)
    monkeypatch.setattr(manual_execution, "time", SimpleNamespace(time=lambda: 106.0))
    monkeypatch.setattr("hermes_trader.agents.executor.maybe_execute",
                        lambda analysis: pytest.fail("expired confirmation reached executor"))

    outcomes = manual_execution.process_pending_commands({"mode": "SHADOW"})
    assert outcomes[0]["status"] == "failed"
    assert outcomes[0]["result"]["reason"] == "confirmation_expired"
    assert _confirmation(expired["id"])["status"] == "expired"


def test_manual_only_pause_and_corrupt_state_block_before_shadow(manual_state, monkeypatch):
    executor, _ = _patch_executor(monkeypatch)
    opens = []
    monkeypatch.setattr(manual_execution.shadow_book, "shadow_open",
                        lambda **kw: opens.append(kw) or _shadow_fill(**kw))

    manual_control.set_mode("manual_only", actor="test", reason="freeze")
    assert executor.maybe_execute(_analysis())["reason"] == "manual_only_mode"
    manual_control.set_mode("auto", actor="test", reason="resume")
    manual_control.pause_coin("BTC", actor="test", reason="pause")
    assert executor.maybe_execute(_analysis())["reason"] == "coin_paused:BTC"
    manual_control.resume_coin("BTC", actor="test", reason="resume")
    with open(manual_control._state_path(), "w") as f:
        f.write("{broken")
    assert executor.maybe_execute(_analysis())["reason"] == "manual_control_unreadable"
    assert opens == []


@pytest.mark.parametrize("requested,expected", [(20.0, 20.0), (10000.0, 1000.0)])
def test_manual_entry_notional_only_reduces_strategy_size(
        manual_state, monkeypatch, requested, expected):
    _, cfg = _patch_executor(monkeypatch)
    opens = []
    monkeypatch.setattr(manual_execution.shadow_book, "shadow_open",
                        lambda **kw: opens.append(kw) or _shadow_fill(**kw))
    manual_control.enqueue_command("manual_enter", {
        "coin": "BTC", "side": "long", "confidence": 0.7,
        "entry_px": 100.0, "notional_usd": requested,
    }, actor="operator", reason="size cap")

    outcomes = manual_execution.process_pending_commands(cfg)
    assert outcomes[0]["status"] == "completed", outcomes[0]["result"]
    assert opens[0]["size_usd"] == pytest.approx(expected)


def test_invalid_manual_entry_is_rejected_before_executor(manual_state, monkeypatch):
    executor, cfg = _patch_executor(monkeypatch)
    called = []
    monkeypatch.setattr(executor, "maybe_execute", lambda analysis: called.append(analysis))
    manual_control.enqueue_command("manual_enter", {
        "coin": "BTC", "side": "long", "confidence": "not-a-number",
        "entry_px": 100.0, "notional_usd": 20.0,
    }, actor="operator", reason="invalid")

    outcomes = manual_execution.process_pending_commands(cfg)
    assert outcomes[0]["status"] == "failed"
    assert called == []


def test_command_exception_closes_confirmation_and_preserves_next_command(
        manual_state, monkeypatch):
    confirmation = manual_control.request_confirmation(_analysis())
    manual_control.decide_confirmation(
        confirmation["id"], approve=True, actor="operator", reason="approve")
    manual_control.enqueue_command("close", {"coin": "ETH"}, actor="operator", reason="close")
    monkeypatch.setattr("hermes_trader.agents.executor.maybe_execute",
                        lambda analysis: (_ for _ in ()).throw(RuntimeError("executor boom")))
    monkeypatch.setattr(manual_execution.shadow_book, "close_now",
                        lambda *a, **k: {"type": "close", "coin": "ETH"})

    outcomes = manual_execution.process_pending_commands({"mode": "SHADOW"})
    assert [o["status"] for o in outcomes] == ["failed", "completed"]
    assert _confirmation(confirmation["id"])["status"] == "failed"


def _patch_shadow_cfg(monkeypatch):
    monkeypatch.setattr(shadow_book, "_shadow_cfg", lambda: {
        "enabled": True, "maker_shadow_enabled": False,
        "starting_balance": 10000.0, "taker_fee_pct": 0.0,
        "taker_slippage_pct": 0.0, "maker_fee_pct": 0.0,
    })


@pytest.mark.parametrize("side,mark,reason", [
    ("long", 94.0, "manual_sl"),
    ("long", 111.0, "manual_tp"),
    ("short", 106.0, "manual_sl"),
    ("short", 89.0, "manual_tp"),
])
def test_shadow_bracket_persists_rehydrates_and_triggers(
        tmp_path, monkeypatch, side, mark, reason):
    _patch_shadow_cfg(monkeypatch)
    path = str(tmp_path / "book.json")
    book = shadow_book.ShadowBook(path=path)
    book.shadow_open(coin="BTC", side=side, entry_px=100.0,
                     size_usd=100.0, leverage=1, analysis_id=f"{side}-{reason}")
    adjusted = book.adjust_bracket("BTC", side=side, sl_pct=5.0, tp_pct=10.0)
    assert adjusted["ok"] is True

    restored = shadow_book.ShadowBook(path=path)
    tracker = restored._trackers[("taker", f"BTC_{side}")]
    expected_sl = 95.0 if side == "long" else 105.0
    expected_tp = 110.0 if side == "long" else 90.0
    assert tracker.sl_px == pytest.approx(expected_sl)
    assert tracker.tp_px == pytest.approx(expected_tp)
    closes = restored.mark_to_market({"BTC": mark})
    assert [c["reason"] for c in closes] == [reason]


def test_shadow_multi_account_bracket_validation_is_atomic(tmp_path, monkeypatch):
    _patch_shadow_cfg(monkeypatch)
    book = shadow_book.ShadowBook(path=str(tmp_path / "book.json"))
    book.shadow_open(coin="BTC", side="long", entry_px=100.0,
                     size_usd=100.0, leverage=1, analysis_id="atomic")
    taker_tracker = book._trackers[("taker", "BTC_long")]
    taker_tracker.sl_px = 94.0
    maker_pos = dict(book.state["accounts"]["taker"]["positions"][0])
    maker_pos["id"] = "maker-pos"
    book.state["accounts"]["maker_shadow"]["positions"].append(maker_pos)
    maker_tracker = dsl_exit.DSLTracker("BTC", "long", 100.0, 100.0)
    maker_tracker.sl_px = 96.0
    book._trackers[("maker_shadow", "BTC_long")] = maker_tracker

    with pytest.raises(ValueError, match="only tighten"):
        book.adjust_bracket("BTC", side="long", sl_pct=5.0)
    assert taker_tracker.sl_px == 94.0
    assert maker_tracker.sl_px == 96.0


def test_live_bracket_uses_executor_and_order_facade(manual_state, monkeypatch):
    calls = []
    monkeypatch.setattr(dsl_exit, "active_tracker_snapshots", lambda: [{
        "coin": "BTC", "side": "long", "entry_px": 100.0,
        "sl_oid": "11", "sl_px": 90.0, "tp_oid": "12", "tp_px": 120.0,
    }])
    monkeypatch.setattr("hermes_trader.agents.executor._live_abs_szi", lambda coin: 1.0)
    monkeypatch.setattr("hermes_trader.agents.executor.modify_sl_trigger",
                        lambda **kw: calls.append(("modify_sl", kw))
                        or {"ok": True, "order_id": 101})
    monkeypatch.setattr("hermes_trader.execution.orders.cancel_orders",
                        lambda oid, coin=None: calls.append(("cancel", oid, coin)) or {"ok": True})
    monkeypatch.setattr("hermes_trader.execution.orders.place_hl_trigger_order",
                        lambda **kw: calls.append(("tp", kw)) or {"ok": True, "order_id": 102})
    monkeypatch.setattr(dsl_exit, "set_bracket",
                        lambda *args, **kw: calls.append(("tracker", kw)) or {"ok": True})

    result = manual_execution.adjust_exit(
        {"mode": "LIVE"}, {"coin": "BTC", "sl_pct": 5.0, "tp_pct": 10.0})
    assert result["ok"] is True
    assert calls[0][1]["new_trigger_px"] == pytest.approx(95.0)
    assert calls[0][1]["oid"] == 11
    tp_call = next(call for call in calls if call[0] == "tp")
    assert tp_call[1]["trigger_px"] == pytest.approx(110.0)
    assert tp_call[1]["kind"] == "tp"
    assert ("cancel", 12, "BTC") in calls
    assert manual_control.get_manual_exit("BTC")["sl_px"] == pytest.approx(95.0)


def test_live_manual_sl_cannot_loosen(manual_state, monkeypatch):
    monkeypatch.setattr(dsl_exit, "active_tracker_snapshots", lambda: [{
        "coin": "BTC", "side": "long", "entry_px": 100.0,
        "sl_oid": "old-sl", "sl_px": 96.0, "tp_oid": None, "tp_px": None,
    }])
    monkeypatch.setattr("hermes_trader.agents.executor._live_abs_szi", lambda coin: 1.0)
    result = manual_execution.adjust_exit(
        {"mode": "LIVE"}, {"coin": "BTC", "sl_pct": 5.0})
    assert result == {"ok": False, "error": "manual SL can only tighten"}


@pytest.fixture()
def operator_client(manual_state, monkeypatch):
    monkeypatch.setenv("HERMES_OPERATOR_TOKEN", "operator-test-token")
    app = FastAPI()
    register_operator_routes(app)
    return TestClient(app)


def test_operator_close_is_queued_and_requires_auth(operator_client):
    assert operator_client.get("/api/dashboard/operator/interventions").status_code == 401
    headers = {"Authorization": "Bearer operator-test-token", "x-portal-user": "tester"}
    response = operator_client.post(
        "/api/dashboard/operator/close", json={"coin": "BTC", "reason": "manual"}, headers=headers)
    assert response.status_code == 200
    assert response.json()["queued"] is True
    commands = manual_control.list_commands()
    assert len(commands) == 1
    assert commands[0]["action"] == "close"
    assert commands[0]["actor"] == "tester"


@pytest.mark.parametrize("path,payload", [
    ("/api/dashboard/operator/interventions/adjust-exit", {"coin": "BTC", "sl_pct": "NaN"}),
    ("/api/dashboard/operator/interventions/enter", {
        "coin": "BTC", "side": "long", "confidence": 0.7, "notional_usd": 20.0}),
])
def test_operator_rejects_invalid_commands_before_queue(operator_client, path, payload):
    headers = {"Authorization": "Bearer operator-test-token"}
    response = operator_client.post(path, json=payload, headers=headers)
    assert response.status_code == 400
    assert manual_control.list_commands() == []
