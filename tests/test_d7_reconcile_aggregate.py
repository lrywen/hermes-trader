# -*- coding: utf-8 -*-
"""D-7 / A-6：外部平仓回填的「聚合多笔减仓 fill」守卫测试。

修复的缺口：交易所侧分批 TP（tp_scale_fraction=0.4 → 先 40% 再 60% 两笔减仓
fill）时，旧回填只取 newest-first 的最近一笔，漏记前一批已实现 PnL、size 只算
最后一批。这里钉死三件事：
  1. aggregate_close_fills 的财务聚合数学（∑sz/∑closedPnl/∑fee、sz 加权 px、
     time 取最旧一笔=仓位完全平完时刻）；
  2. _collect_reducing_fills 收集全部减仓腿（newest-first 顺序保留）；
  3. resolve_close_fills = 收集 + 聚合的端到端（mock _http_post，离线）。
"""
from __future__ import annotations

import pytest

from hermes_trader.agents import dsl_exit

# ── 聚合数学 ───────────────────────────────────────────────────────────────

def _leg(px, sz, closed_pnl, fee, oid, t_ms):
    """一条减仓 fill（HL userFills 原始字段）。"""
    return {"coin": "ETH", "side": "A", "px": px, "sz": sz,
            "closedPnl": closed_pnl, "fee": fee, "oid": oid, "time": t_ms}


def test_aggregate_single_leg_is_identity():
    f = _leg(100.0, 1.0, 5.0, 0.02, "o1", 1000)
    agg = dsl_exit.aggregate_close_fills([f])
    assert agg["sz"] == 1.0
    assert agg["closedPnl"] == 5.0
    assert agg["fee"] == 0.02
    assert agg["px"] == 100.0
    assert agg["time"] == 1000
    assert agg["legs"] == 1


def test_aggregate_scaled_tp_sums_pnl_fee_size_and_weights_price():
    # newest-first：60% 腿（@110）在前，40% 腿（@105）在后。
    leg_60 = _leg(110.0, 0.6, 6.0, 0.012, "o60", 2000)
    leg_40 = _leg(105.0, 0.4, 2.0, 0.008, "o40", 1000)
    agg = dsl_exit.aggregate_close_fills([leg_60, leg_40])

    assert agg["sz"] == pytest.approx(1.0)
    assert agg["closedPnl"] == pytest.approx(8.0)   # ∑ 两批已实现 PnL
    assert agg["fee"] == pytest.approx(0.020)
    # sz 加权均价 = 110*0.6 + 105*0.4 = 108
    assert agg["px"] == pytest.approx(108.0)
    # time 取最旧一笔（仓位完全平完）。
    assert agg["time"] == 1000
    assert agg["legs"] == 2
    assert agg["oids"] == ["o60", "o40"]


def test_aggregate_rejects_empty():
    with pytest.raises(ValueError):
        dsl_exit.aggregate_close_fills([])


# ── 收集 + 端到端 ──────────────────────────────────────────────────────────

def test_collect_gathers_all_reducing_legs(monkeypatch):
    # 构造 userFills：newest-first，含两笔 ETH 减仓 + 一笔无关 BTC + 一笔 ETH 开仓。
    t0 = 1_000_000.0  # 秒
    fills = [
        {"coin": "ETH", "side": "A", "px": 110, "sz": 0.6, "closedPnl": 6.0,
         "fee": 0.012, "oid": "o60", "time": int((t0 + 20) * 1000)},
        {"coin": "BTC", "side": "A", "px": 50000, "sz": 0.001, "closedPnl": 0,
         "fee": 0.01, "oid": "obtc", "time": int((t0 + 15) * 1000)},
        {"coin": "ETH", "side": "A", "px": 105, "sz": 0.4, "closedPnl": 2.0,
         "fee": 0.008, "oid": "o40", "time": int((t0 + 10) * 1000)},
        {"coin": "ETH", "side": "B", "px": 100, "sz": 1.0, "closedPnl": 0,
         "fee": 0.02, "oid": "oopen", "time": int(t0 * 1000)},
    ]
    monkeypatch.setattr(dsl_exit, "_http_post",
                        lambda path, payload, timeout=8: fills)

    matched = dsl_exit._collect_reducing_fills("0xU", "ETH", "long", t0 - 1)
    assert matched is not None
    assert len(matched) == 2  # 两笔减仓，开仓/BTC 被剔除
    assert [m["oid"] for m in matched] == ["o60", "o40"]  # newest-first


def test_resolve_close_fills_end_to_end(monkeypatch):
    t0 = 2_000_000.0
    fills = [
        {"coin": "SOL", "side": "A", "px": 150, "sz": 0.5, "closedPnl": 10.0,
         "fee": 0.01, "oid": "s2", "time": int((t0 + 30) * 1000)},
        {"coin": "SOL", "side": "A", "px": 140, "sz": 0.5, "closedPnl": 5.0,
         "fee": 0.01, "oid": "s1", "time": int((t0 + 20) * 1000)},
    ]
    monkeypatch.setattr(dsl_exit, "_http_post",
                        lambda path, payload, timeout=8: fills)

    agg = dsl_exit.resolve_close_fills("0xU", "SOL", "long", t0 - 1)
    assert agg is not None
    assert agg["sz"] == pytest.approx(1.0)
    assert agg["closedPnl"] == pytest.approx(15.0)  # 不再漏记第一批
    assert agg["px"] == pytest.approx(145.0)
    assert agg["legs"] == 2


def test_resolve_close_fills_none_when_no_reducing(monkeypatch):
    monkeypatch.setattr(dsl_exit, "_http_post",
                        lambda path, payload, timeout=8: [])
    assert dsl_exit.resolve_close_fills("0xU", "ETH", "long", 1.0) is None


def test_resolve_close_fills_none_on_lookup_failure(monkeypatch):
    def _boom(path, payload, timeout=8):
        raise RuntimeError("network down")
    monkeypatch.setattr(dsl_exit, "_http_post", _boom)
    assert dsl_exit.resolve_close_fills("0xU", "ETH", "long", 1.0) is None
