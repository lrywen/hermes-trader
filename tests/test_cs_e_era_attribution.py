"""CS-E (2026-09-09): era 记账透传与离线 era_attribution 归因分桶。

覆盖：
A. executor：
   1. _register_filled_position 落 order 时携带 config_era_id，entry_context
      携带完整 config_era dict（含 tracked 快照）。
   2. close_position_market 平仓时把 entry_context 的 config_era 经
      config_era_at_entry 透传到 close 记录。
B. scripts/era_attribution（纯离线）：
   3. build_boundaries 保留 CS-A 老审计（仅 changed_keys）为缺口边界。
   4. build_segments 逆序回放：完整链 medium，缺口边界自身段及更早 low。
   5. attribute：high（成交自带 era_id）/ medium / low / pre_instrumentation
      四级分桶与 block_reasons / pnl 聚合正确。
   6. terminal_ok=False 时所有回放段为 low（不伪造终态逆推）。
"""

from __future__ import annotations

import json
import os
import sys

from hermes_trader import event_log
from hermes_trader.agents import config_store as cs

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from scripts import era_attribution as ea

DAY = 86_400_000
_T0 = ea._iso_to_ms("2026-08-01T00:00:00+00:00")


def _iso(ms):
    import datetime as dt

    return dt.datetime.fromtimestamp(ms / 1000, tz=dt.timezone.utc).isoformat()


def _ev(day, event, payload):
    return {"timestamp": _iso(_T0 + day * DAY), "event": event, "payload": payload}


def _read(path):
    if not os.path.exists(path):
        return []
    return [json.loads(l) for l in open(path) if l.strip()]


# ── A1. order / entry_context 记账 ─────────────────────────────────────────


def _isolate_dsl(monkeypatch, tmp_path):
    from hermes_trader.agents import dsl_exit

    monkeypatch.setattr(dsl_exit, "DSL_STATE_FILE", str(tmp_path / "dsl.json"))
    dsl_exit._active_positions.clear()
    dsl_exit._loaded_from_disk = False
    return dsl_exit


def test_register_filled_position_records_order_era(monkeypatch, tmp_path):
    from hermes_trader.agents import executor

    ev_file = tmp_path / "events.jsonl"
    monkeypatch.setattr(event_log, "EVENTS_FILE", str(ev_file))
    _isolate_dsl(monkeypatch, tmp_path)

    # 隔离热路径外部依赖（纯验证记账，绝不触网 / 不通知）。
    monkeypatch.setattr(executor, "register_position", lambda *a, **k: None)
    monkeypatch.setattr("hermes_trader.agents.shadow_signals.gather_shadow_signals", lambda *a, **k: {})
    monkeypatch.setattr("hermes_trader.client.hl_client.fetch_funding_history", lambda *a, **k: [])
    monkeypatch.setattr(executor.memory, "flush", lambda: None)

    config = {"leverage": 10, "min_ai_confidence": 0.6, "dsl_exit": {}}
    expected_era = cs.compute_config_era(config)["era_id"]

    executor._register_filled_position(
        analysis={"id": "an1", "coin": "ETH"},
        config=config,
        order_res={"order_id": "o9", "avg_px": 2000.0, "total_sz": 0.1},
        coin="ETH",
        trade_side="long",
        mid_price=2000.0,
        size_in_coin=0.1,
        atr=40.0,
        leverage=10,
        user="0xU",
        override_composite=50.0,
        enf=None,
        aid="an1",
    )

    rows = _read(str(ev_file))
    orders = [r for r in rows if r["event"] == "order"]
    assert len(orders) == 1
    p = orders[0]["payload"]
    assert p["config_era_id"] == expected_era
    assert len(p["config_era_id"]) == 12

    # entry_context 缓存了完整 dict（id + tracked 快照）。
    ctx = executor.memory.pop_entry_context("ETH", "long")
    assert ctx["config_era"]["era_id"] == expected_era
    assert ctx["config_era"]["tracked"]["leverage"] == 10


# ── A2. close 透传 config_era_at_entry ─────────────────────────────────────


def test_close_carries_config_era_at_entry(monkeypatch, tmp_path):
    from hermes_trader.agents import executor

    _isolate_dsl(monkeypatch, tmp_path).register_position("ARB", "short", 0.10522, leverage=10)
    ev_file = tmp_path / "events.jsonl"
    monkeypatch.setattr(event_log, "EVENTS_FILE", str(ev_file))
    monkeypatch.setattr(executor, "resolve_user_address", lambda: "0xUSER")
    monkeypatch.setattr(
        executor,
        "fetch_account_state",
        lambda u, **kw: {
            "asset_positions": [{"position": {"coin": "ARB", "szi": "-1000", "entryPx": "0.10522"}}],
        },
    )
    monkeypatch.setattr(executor, "get_hl_price", lambda c: 0.095)
    monkeypatch.setattr(
        executor, "place_hl_order", lambda **kw: {"ok": True, "order_id": "x1", "avg_px": 0.095, "total_sz": 1000.0}
    )
    monkeypatch.setattr(executor, "cancel_open_orders_for_coin", lambda c: None)

    era = {"era_id": "eraENTRY12345", "tracked": {"leverage": 10}}
    monkeypatch.setattr(executor.memory, "pop_entry_context", lambda coin, side: {"config_era": era})
    monkeypatch.setattr(executor.memory, "record_loss_outcome", lambda *a: None)
    monkeypatch.setattr(executor.memory, "get_start_of_day_equity", lambda: 0.0)
    monkeypatch.setattr(executor.memory, "get_daily_pnl", lambda: 0.0)

    res = executor.close_position_market("ARB")
    assert res["ok"] is True
    rows = _read(str(ev_file))
    closes = [r for r in rows if r["event"] == "close"]
    assert len(closes) == 1
    assert closes[0]["payload"]["config_era_at_entry"] == era


# ── B. 离线归因 ────────────────────────────────────────────────────────────


def _sample_events():
    return [
        _ev(0, "order", {"coin": "BTC", "executed_at": _T0}),  # pre
        _ev(9, "risk_gate", {"coin": "ETH", "verdict": "block", "block_reasons": ["x"]}),  # pre
        _ev(14, "config_write", {"changed_keys": ["market_circuit"]}),  # 老审计缺口
        _ev(19, "risk_gate", {"coin": "ETH", "verdict": "block", "block_reasons": ["daily_loss"]}),  # low 段
        _ev(
            25,
            "close",
            {
                "coin": "ETH",
                "entry_time": _T0 + 25 * DAY,
                "closed_at": _T0 + 26 * DAY,
                "realized_pnl_usd": 5.0,
                "hold_minutes": 120,
                "regime_at_entry": "neutral",
            },
        ),  # low 段
        _ev(
            31,
            "config_write",
            {"changed_keys": ["leverage"], "old": {"leverage": 10}, "new": {"leverage": 20}, "era_id": "eraNEW"},
        ),  # 完整边界
        _ev(32, "risk_gate", {"coin": "SOL", "verdict": "block", "block_reasons": ["cooldown"]}),  # medium 段
        _ev(33, "order", {"coin": "SOL", "executed_at": _T0 + 33 * DAY, "config_era_id": "eraX"}),  # high
    ]


def test_build_boundaries_keeps_legacy_gap():
    bounds = ea.build_boundaries(_sample_events())
    assert len(bounds) == 2
    legacy, full = bounds
    assert legacy["new"] == {"market_circuit": None} and legacy["old"] == {}
    assert full["new"] == {"leverage": 20} and full["old"] == {"leverage": 10}
    assert full["era_id"] == "eraNEW"


def test_build_segments_gap_segment_is_low():
    bounds = ea.build_boundaries(_sample_events())
    terminal = {"leverage": 20, "market_circuit": {"daily_loss_limit": 0.05}}
    segs = ea.build_segments(bounds, terminal, True)
    assert len(segs) == 2
    # 逆序回放：老缺口边界自身段 low；其后（更晚）完整段 medium。
    assert segs[0]["start_ts"] == bounds[0]["ts"]
    assert segs[0]["confidence"] == ea.CONF_LOW
    assert segs[1]["confidence"] == ea.CONF_MEDIUM
    assert segs[1]["era_id"] == "eraNEW"
    assert segs[0]["end_ts"] == bounds[1]["ts"]
    assert segs[1]["end_ts"] is None


def test_attribute_four_level_buckets():
    events = _sample_events()
    bounds = ea.build_boundaries(events)
    terminal = {"leverage": 20, "market_circuit": {"daily_loss_limit": 0.05}}
    segs = ea.build_segments(bounds, terminal, True)
    eras = ea.attribute(events, segs)

    # high：成交自带 era_id。
    assert eras["eraX"]["confidence"] == ea.CONF_HIGH
    assert eras["eraX"]["orders"] == 1

    # pre_instrumentation：早于首条边界的 order + risk_gate。
    pre = eras[ea.PRE_ERA]
    assert pre["confidence"] == ea.CONF_PRE
    assert pre["orders"] == 1 and pre["risk_blocks"] == 1
    assert pre["block_reasons"] == {"x": 1}

    # low 段：缺口后的 risk_gate + close。
    low_id = segs[0]["era_id"]
    low = eras[low_id]
    assert low["confidence"] == ea.CONF_LOW
    assert low["risk_blocks"] == 1
    assert low["block_reasons"] == {"daily_loss": 1}
    assert low["closes"] == 1 and low["wins"] == 1
    assert low["pnl_usd_sum"] == 5.0

    # medium 段：完整边界之后、无自带 era 的 risk_gate。
    med = eras[segs[1]["era_id"]]
    assert med["confidence"] == ea.CONF_MEDIUM
    assert med["risk_blocks"] == 1
    assert med["block_reasons"] == {"cooldown": 1}

    report = ea.build_report(eras, True)
    assert report["terminal_config_used"] is True
    assert len(report["eras"]) == 4


def test_attribute_pre_when_no_boundaries():
    # 无任何配置边界 -> 全部成交归 pre_instrumentation。
    events = [_ev(1, "order", {"coin": "BTC", "executed_at": _T0 + DAY})]
    eras = ea.attribute(events, [])
    assert eras[ea.PRE_ERA]["confidence"] == ea.CONF_PRE


def test_terminal_missing_forces_low_not_medium():
    # 终态配置缺失（terminal_ok=False）：逆序回放无锚点，全部段 low，
    # 绝不把当前猜测配置伪装成可信重建。
    events = [
        _ev(
            30,
            "config_write",
            {"changed_keys": ["leverage"], "old": {"leverage": 10}, "new": {"leverage": 20}, "era_id": "eraNEW"},
        ),
        _ev(32, "risk_gate", {"coin": "SOL", "block_reasons": ["c"]}),
    ]
    bounds = ea.build_boundaries(events)
    segs = ea.build_segments(bounds, {}, False)
    assert all(s["confidence"] == ea.CONF_LOW for s in segs)
    eras = ea.attribute(events, segs)
    assert eras["eraNEW"]["confidence"] == ea.CONF_LOW


def test_load_events_skips_bad_lines(tmp_path):
    p = tmp_path / "events.jsonl"
    p.write_text(
        json.dumps({"event": "order", "timestamp": _iso(_T0), "payload": {"coin": "BTC", "executed_at": _T0}})
        + "\n"
        + "{not json\n"
        + "\n"
    )
    recs = ea.load_events(str(p))
    assert len(recs) == 1 and recs[0]["event"] == "order"
    # 文件缺失不抛异常。
    assert ea.load_events(str(tmp_path / "nope.jsonl")) == []
