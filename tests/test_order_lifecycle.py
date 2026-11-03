"""P3 订单状态机 + 意图 WAL + 崩溃恢复测试。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_trader.execution import intent_wal
from hermes_trader.execution.order_fsm import (
    IllegalTransition, OrderIntent, OrderState)
from hermes_trader.execution import lifecycle
from hermes_trader.execution.recover import recover_pending_orders


def _intent(cloid="c1", coin="BTC") -> OrderIntent:
    return OrderIntent(cloid=cloid, coin=coin, side="long", is_buy=True,
                       size_in_coin=0.01, mid_px=70000.0, analysis_id="aid-1")


# ── FSM ────────────────────────────────────────────────────────────
def test_fsm_happy_path():
    it = _intent()
    assert it.state == OrderState.INTENT
    it.transition(OrderState.SUBMITTED)
    it.transition(OrderState.FILLED, avg_px=70100.0, total_sz=0.01)
    assert it.state == OrderState.FILLED
    assert it.is_terminal
    assert it.avg_px == 70100.0


def test_fsm_unknown_then_reconcile():
    it = _intent()
    it.transition(OrderState.SUBMITTED)
    it.transition(OrderState.UNKNOWN, reason="response_unknown")
    assert it.is_pending
    it.transition(OrderState.FILLED)
    assert it.is_terminal


def test_fsm_terminal_no_transition():
    it = _intent()
    it.transition(OrderState.SUBMITTED)
    it.transition(OrderState.FILLED)
    with pytest.raises(IllegalTransition):
        it.transition(OrderState.FAILED)


def test_fsm_illegal_jump():
    it = _intent()
    with pytest.raises(IllegalTransition):
        it.transition(OrderState.FILLED)  # INTENT 不能直接到 FILLED


def test_fsm_intent_abort():
    it = _intent()
    it.transition(OrderState.FAILED, reason="gate_blocked")
    assert it.is_terminal


def test_fsm_roundtrip_dict():
    it = _intent()
    it.transition(OrderState.SUBMITTED)
    d = it.to_dict()
    it2 = OrderIntent.from_dict(d)
    assert it2.cloid == it.cloid
    assert it2.state == OrderState.SUBMITTED
    assert it2.coin == "BTC"


# ── WAL ────────────────────────────────────────────────────────────
def test_wal_record_and_load_pending(tmp_path):
    wal = tmp_path / "wal.jsonl"
    intent_wal.set_wal_path(wal)
    it = _intent()
    intent_wal.record_intent(it)
    rec = it.transition(OrderState.SUBMITTED)
    intent_wal.record_transition(it, rec)
    pending = intent_wal.load_pending(wal)
    assert len(pending) == 1
    assert pending[0].cloid == "c1"
    assert pending[0].state == OrderState.SUBMITTED
    assert pending[0].coin == "BTC"
    assert pending[0].size_in_coin == 0.01


def test_wal_terminal_not_pending(tmp_path):
    wal = tmp_path / "wal.jsonl"
    intent_wal.set_wal_path(wal)
    it = _intent()
    intent_wal.record_intent(it)
    intent_wal.record_transition(it, it.transition(OrderState.SUBMITTED))
    intent_wal.record_transition(it, it.transition(OrderState.FILLED))
    assert intent_wal.load_pending(wal) == []
    all_i = intent_wal.load_all(wal)
    assert all_i["c1"].state == OrderState.FILLED


def test_wal_multiple_intents(tmp_path):
    wal = tmp_path / "wal.jsonl"
    intent_wal.set_wal_path(wal)
    for cid in ("c1", "c2", "c3"):
        it = _intent(cloid=cid)
        intent_wal.record_intent(it)
        intent_wal.record_transition(it, it.transition(OrderState.SUBMITTED))
    # c2 成交
    it2 = _intent(cloid="c2")
    it2.state = OrderState.SUBMITTED
    # 重放后 c1/c3 未决，c2 已终态
    pend = intent_wal.load_pending(wal)
    assert {p.cloid for p in pend} == {"c1", "c2", "c3"}


def test_wal_missing_file(tmp_path):
    assert intent_wal.load_pending(tmp_path / "nope.jsonl") == []


# ── lifecycle 桥 ───────────────────────────────────────────────────
def test_lifecycle_full_flow(tmp_path):
    intent_wal.set_wal_path(tmp_path / "wal.jsonl")
    intent = lifecycle.begin_order(
        cloid="c9", coin="ETH", side="long", is_buy=True,
        size_in_coin=0.1, mid_px=3000.0, mode="live", analysis_id="a9")
    assert intent is not None
    lifecycle.mark_submitted(intent)
    lifecycle.mark_filled(intent, {"order_id": "oid1", "avg_px": 3001.0,
                                   "total_sz": 0.1})
    assert intent.state == OrderState.FILLED
    # 终态后从 active 移除
    assert "c9" not in lifecycle.active_intents()


def test_lifecycle_none_cloid_skips(tmp_path):
    intent_wal.set_wal_path(tmp_path / "wal.jsonl")
    assert lifecycle.begin_order(
        cloid=None, coin="ETH", side="long", is_buy=True,
        size_in_coin=0.1, mid_px=3000.0, mode="live", analysis_id="a") is None


def test_lifecycle_none_intent_noop(tmp_path):
    # 所有 mark 对 None intent 都是 no-op，不抛异常
    lifecycle.mark_submitted(None)
    lifecycle.mark_unknown(None)
    lifecycle.mark_filled(None, {})
    lifecycle.mark_failed(None)


# ── 崩溃恢复 ───────────────────────────────────────────────────────
def test_recover_intent_before_place(tmp_path):
    wal = tmp_path / "wal.jsonl"
    intent_wal.set_wal_path(wal)
    it = _intent()
    intent_wal.record_intent(it)  # 只写 INTENT，未 place（崩溃）
    summary = recover_pending_orders(wal_path=str(wal), notify_fn=lambda *a, **k: None)
    assert summary["failed"] == 1
    assert intent_wal.load_all(wal)["c1"].state == OrderState.FAILED


def test_recover_submitted_filled(tmp_path, monkeypatch):
    wal = tmp_path / "wal.jsonl"
    intent_wal.set_wal_path(wal)
    it = _intent()
    intent_wal.record_intent(it)
    intent_wal.record_transition(it, it.transition(OrderState.SUBMITTED))

    def fake_reconcile(coin, cloid, is_buy, expect_size):
        return {"status": "filled", "oid": "oidX", "avg_px": 70100.0,
                "total_sz": 0.01}
    import hermes_trader.client.exchange as exch
    monkeypatch.setattr(exch, "reconcile_order_fill", fake_reconcile)
    summary = recover_pending_orders(wal_path=str(wal), notify_fn=lambda *a, **k: None)
    assert summary["filled"] == 1
    assert summary["cloids_filled"] == ["c1"]
    assert intent_wal.load_all(wal)["c1"].state == OrderState.FILLED


def test_recover_submitted_not_filled(tmp_path, monkeypatch):
    wal = tmp_path / "wal.jsonl"
    intent_wal.set_wal_path(wal)
    it = _intent()
    intent_wal.record_intent(it)
    intent_wal.record_transition(it, it.transition(OrderState.SUBMITTED))

    def fake_reconcile(coin, cloid, is_buy, expect_size):
        return {"status": "not_filled", "reason": "no fill"}
    import hermes_trader.client.exchange as exch
    monkeypatch.setattr(exch, "reconcile_order_fill", fake_reconcile)
    summary = recover_pending_orders(wal_path=str(wal), notify_fn=lambda *a, **k: None)
    assert summary["failed"] == 1


def test_recover_unresolvable_stays_unknown(tmp_path, monkeypatch):
    wal = tmp_path / "wal.jsonl"
    intent_wal.set_wal_path(wal)
    it = _intent()
    intent_wal.record_intent(it)
    intent_wal.record_transition(it, it.transition(OrderState.SUBMITTED))
    intent_wal.record_transition(it, it.transition(OrderState.UNKNOWN))

    def fake_reconcile(coin, cloid, is_buy, expect_size):
        return {"status": "unknown", "reason": "exchange deaf"}
    import hermes_trader.client.exchange as exch
    monkeypatch.setattr(exch, "reconcile_order_fill", fake_reconcile)
    summary = recover_pending_orders(wal_path=str(wal), notify_fn=lambda *a, **k: None)
    assert summary["still_unknown"] == 1
    # 仍是未决，下轮恢复可再试
    assert len(intent_wal.load_pending(wal)) == 1


def test_recover_empty_wal(tmp_path):
    summary = recover_pending_orders(wal_path=str(tmp_path / "empty.jsonl"),
                                     notify_fn=lambda *a, **k: None)
    assert summary["scanned"] == 0


def test_recover_with_explicit_wal_path_writes_back_to_same_path(tmp_path, monkeypatch):
    """B6 (2026-11-03): recover_pending_orders(wal_path=X) must read AND write
    X via parameter pass-through. Before the fix, record_transition ignored the
    argument and appended to the module-global WAL, so a drill/recovery against
    a sidecar file leaked ghost cloids into the production WAL. The global path
    here is a decoy that must stay empty; the explicit path receives the
    transition.
    """
    from hermes_trader.execution import intent_wal as iw
    side = tmp_path / "side.jsonl"
    global_decoy = tmp_path / "GLOBAL.jsonl"
    iw.set_wal_path(global_decoy)  # simulate the production global default

    it = _intent(cloid="side-1", coin="BTC")
    # write the crashed intent directly to the SIDECAR (not the global)
    iw.record_intent(it, path=str(side))
    assert side.exists() and not global_decoy.exists()

    summary = recover_pending_orders(wal_path=str(side),
                                     notify_fn=lambda *a, **k: None)
    assert summary["failed"] == 1  # INTENT-only -> crash_before_place

    # transition landed in the sidecar …
    side_rows = [json.loads(l) for l in side.read_text().splitlines()]
    assert any(r.get("to") == OrderState.FAILED.value for r in side_rows)
    # … and the global decoy was never touched (no ghost cloid leak).
    assert not global_decoy.exists()
