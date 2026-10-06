"""outcome_compare maker 踏空/机会成本口径的离线单测。"""
from __future__ import annotations

from scripts.outcome_compare import analyse_execution


def _close(aid, pnl):
    return {"type": "close", "analysis_id": aid, "realized_pnl_usd": pnl}


def _cancel(aid):
    return {"type": "cancel", "analysis_id": aid}


def test_paired_filled_only():
    taker = [_close("a", 10.0), _close("b", -4.0)]
    maker = [_close("a", 12.0), _close("b", -2.0)]
    r = analyse_execution(taker, maker, [])
    assert r["paired_count"] == 2
    assert r["maker_miss_count"] == 0
    assert r["maker_miss_rate_pct"] == 0.0
    # 配对改善：(12-10)+(-2+4)=4
    assert r["maker_net_edge_vs_taker_usd"] == 4.0


def test_miss_opportunity_cost_subtracted():
    # a 双方成交（maker 多赚 2）；c maker 踏空，taker 在 c 赚 8（maker 错过）
    taker = [_close("a", 10.0), _close("c", 8.0)]
    maker = [_close("a", 12.0)]
    cancels = [_cancel("c")]
    r = analyse_execution(taker, maker, cancels)
    assert r["maker_miss_count"] == 1
    assert r["maker_miss_rate_pct"] == 50.0
    assert r["maker_missed_opportunity_cost_usd"] == 8.0
    # 净 edge = 配对改善 2 − 踏空机会成本 8 = -6
    assert r["maker_net_edge_vs_taker_usd"] == -6.0


def test_cancel_without_taker_fill_not_counted():
    # maker 撤单但同信号 taker 也没成交记录：无法计机会成本，不算踏空
    taker = [_close("a", 10.0)]
    maker = [_close("a", 11.0)]
    r = analyse_execution(taker, maker, [_cancel("z")])
    assert r["maker_miss_count"] == 0
