"""M-1 OFI 因子的离线单测（Cont et al. 2014 公式逐情形核对）。"""
from __future__ import annotations

import pytest

from hermes_trader.agents.ofi import (
    aggregate_ofi,
    aggressor_count_imbalance,
    best_ofi_two_sided,
    cvd_momentum,
    cvd_series,
    depth_imbalance,
    depth_ofi,
    large_trade_flow_imbalance,
    mid,
    ofi_mid_divergence,
    quantile_sorted,
    queue_buildup_slope,
    queue_withdrawal,
    signed_trade,
    trade_flow_imbalance,
)


def _snap(bids, asks):
    return {"t": 0, "b": [[p, q] for p, q in bids],
            "a": [[p, q] for p, q in asks]}


# ── best-level OFI ──────────────────────────────────────────────────────────

def test_best_bid_price_up():
    # bid 价升: 1[new>=old]*Qnew = 10
    prev = _snap([(100, 5)], [(101, 5)])
    cur = _snap([(100.5, 10)], [(101, 5)])
    assert best_ofi_two_sided(prev, cur) == pytest.approx(10.0)


def test_best_bid_price_down():
    # bid 价跌: -1[new<=old]*Qold = -5
    prev = _snap([(100, 5)], [(101, 5)])
    cur = _snap([(99.5, 8)], [(101, 5)])
    assert best_ofi_two_sided(prev, cur) == pytest.approx(-5.0)


def test_best_ask_price_down_buy_pressure():
    # ask 价跌（卖压被吃）: -1[new<=old]*Qnew = -(-8)=? ask: -1[Pa<=Pa_]*Qa
    prev = _snap([(100, 5)], [(101, 5)])
    cur = _snap([(100, 5)], [(100.5, 8)])
    # ask new<=old -> -Qnew = -8
    assert best_ofi_two_sided(prev, cur) == pytest.approx(-8.0)


def test_best_ask_price_up():
    # ask 价升（卖单上移/成交）: +1[new>=old]*Qold = +5
    prev = _snap([(100, 5)], [(101, 5)])
    cur = _snap([(100, 5)], [(101.5, 8)])
    assert best_ofi_two_sided(prev, cur) == pytest.approx(5.0)


def test_unchanged_price_size_change_bid():
    # 价不变 bid: ΔQ = 8-5 = +3
    prev = _snap([(100, 5)], [(101, 5)])
    cur = _snap([(100, 8)], [(101, 5)])
    assert best_ofi_two_sided(prev, cur) == pytest.approx(3.0)


def test_missing_side_returns_zero():
    prev = _snap([(100, 5)], [(101, 5)])
    cur = {"t": 1, "b": [], "a": []}
    assert best_ofi_two_sided(prev, cur) == 0.0


# ── depth OFI ───────────────────────────────────────────────────────────────

def test_depth_ofi_size_addition_same_prices():
    # 所有价位不变，best bid 加量 3，权重 1.0（第1档）
    prev = _snap([(100, 5), (99, 4)], [(101, 5), (102, 4)])
    cur = _snap([(100, 8), (99, 4)], [(101, 5), (102, 4)])
    assert depth_ofi(prev, cur) == pytest.approx(3.0)


def test_depth_ofi_new_bid_level_weighted():
    # 新增第 3 档 bid=98 量 10，权重 1/3
    prev = _snap([(100, 5), (99, 4)], [(101, 5)])
    cur = _snap([(100, 5), (99, 4), (98, 10)], [(101, 5)])
    assert depth_ofi(prev, cur) == pytest.approx(10.0 / 3.0)


def test_depth_ofi_ask_exit_is_positive():
    # ask 第2档退出（供给减少，买压方向 +）：+qp 权重 1/2 = 4/2=2
    prev = _snap([(100, 5)], [(101, 5), (102, 4)])
    cur = _snap([(100, 5)], [(101, 5)])
    assert depth_ofi(prev, cur) == pytest.approx(2.0)


# ── mid / aggregate ─────────────────────────────────────────────────────────

def test_mid_basic():
    assert mid(_snap([(100, 5)], [(102, 5)])) == 101.0


def test_mid_missing_side_none():
    assert mid(_snap([(100, 5)], [])) is None


def test_aggregate_sum():
    assert aggregate_ofi([1.0, -2.0, 4.0]) == 3.0


# ── 扩展因子族 ───────────────────────────────────────────────────────────────

def test_depth_imbalance_basic():
    s = _snap([(100, 8), (99, 2)], [(101, 6), (102, 4)])
    # Qb=10, Qa=10 -> 0
    assert depth_imbalance(s) == pytest.approx(0.0)


def test_depth_imbalance_bid_heavy():
    s = _snap([(100, 30)], [(101, 10)])
    assert depth_imbalance(s) == pytest.approx(0.5)


def test_depth_imbalance_empty_none():
    s = _snap([], [])
    assert depth_imbalance(s) is None


def test_queue_withdrawal_bid_removed():
    # 同价位 bid 挂量 10->4（买盘撤离，-6），价位不动
    prev = _snap([(100, 10)], [(101, 10)])
    cur = _snap([(100, 4)], [(101, 10)])
    assert queue_withdrawal(cur=cur, prev=prev) == pytest.approx(-6.0)


def test_queue_withdrawal_ask_removed_positive():
    # 同价位 ask 挂量 10->4（卖盘撤离，+6，利好）
    prev = _snap([(100, 10)], [(101, 10)])
    cur = _snap([(100, 10)], [(101, 4)])
    assert queue_withdrawal(cur=cur, prev=prev) == pytest.approx(6.0)


def test_queue_withdrawal_ignores_level_change():
    # 价格移动（非同价位）不计入 -> 0
    prev = _snap([(100, 10)], [(101, 10)])
    cur = _snap([(99.5, 10)], [(100.5, 10)])
    assert queue_withdrawal(cur=cur, prev=prev) == pytest.approx(0.0)


def test_buildup_slope_bid_heavy_far_levels():
    # bid 远端厚：bid 各档量随档位增大；ask 均匀
    prev_bids = [(100, 1), (99, 5), (98, 9)]
    asks = [(101, 5), (102, 5), (103, 5)]
    s = _snap(prev_bids, asks)
    v = queue_buildup_slope(s)
    assert v is not None and v > 0


def test_buildup_slope_insufficient_levels_none():
    s = _snap([(100, 5)], [(101, 5)])
    assert queue_buildup_slope(s) is None


def test_ofi_mid_divergence_aligned():
    # bid 价升量增 -> OFI>0 且 mid 升 -> +1
    prev = _snap([(100, 5)], [(102, 5)])
    cur = _snap([(101, 10)], [(103, 10)])
    assert ofi_mid_divergence(cur=cur, prev=prev) == pytest.approx(1.0)


def test_ofi_mid_divergence_missing_none():
    prev = _snap([(100, 5)], [])
    cur = _snap([(101, 5)], [])
    assert ofi_mid_divergence(cur=cur, prev=prev) is None


# ── 成交流因子（CVD / 主动成交失衡）──────────────────────────────────────────

def _tr(side, sz):
    return {"side": side, "sz": sz, "px": 100.0, "time": 0}


def test_signed_trade_buy_sell_bad():
    assert signed_trade(_tr("B", 3)) == 3.0
    assert signed_trade(_tr("A", 3)) == -3.0
    assert signed_trade(_tr("B", 0)) == 0.0
    assert signed_trade(_tr("B", "x")) == 0.0
    # 非 B 一律按卖（taker sell）处理
    assert signed_trade({"side": "X", "sz": 2}) == -2.0


def test_cvd_series_cumulative():
    ws = [_tr("B", 3), _tr("A", 5), _tr("B", 2)]
    # 0, +3, -2, 0
    assert cvd_series(ws) == [0.0, 3.0, -2.0, 0.0]
    assert cvd_series([]) == [0.0]


def test_trade_flow_imbalance_basic():
    # 净 +4，gross 8 -> 0.5
    ws = [_tr("B", 6), _tr("A", 2)]
    assert trade_flow_imbalance(ws) == pytest.approx(0.5)


def test_trade_flow_imbalance_empty_none():
    assert trade_flow_imbalance([]) is None
    assert trade_flow_imbalance([_tr("B", 0)]) is None


def test_aggressor_count_imbalance():
    ws = [_tr("B", 1), _tr("B", 1), _tr("A", 1)]
    # (2-1)/3
    assert aggressor_count_imbalance(ws) == pytest.approx(1 / 3)
    assert aggressor_count_imbalance([]) is None


def test_large_trade_flow_requires_enough_prints():
    assert large_trade_flow_imbalance([_tr("B", 1)] * 4) is None


def test_large_trade_flow_top_quantile():
    # 5 笔；0.9 分位阈值线性插值，最大两笔（10）落在阈值以上 -> >=3? 只有2笔
    ws = [_tr("B", 1), _tr("A", 2), _tr("B", 3), _tr("B", 10), _tr("B", 10)]
    # 阈值= q0.9: pos=3.6 -> sorted[3]*0.4+sorted[4]*0.6 = 3*0.4+10*0.6=7.2
    # 仅两笔 10 >= 7.2 -> cnt=2 <3 -> None
    assert large_trade_flow_imbalance(ws) is None
    # 三笔 10：cnt=3，净 +30 gross 30 -> 1.0
    ws2 = [_tr("A", 1), _tr("A", 2), _tr("B", 3),
           _tr("B", 10), _tr("B", 10), _tr("B", 10)]
    assert large_trade_flow_imbalance(ws2) == pytest.approx(1.0)


def test_cvd_momentum_accelerating():
    # 6 笔，m=3：前半全卖净-3，后半全买净+3，gross 6 -> (3-(-3))/6=1
    ws = [_tr("A", 1), _tr("A", 1), _tr("A", 1),
          _tr("B", 1), _tr("B", 1), _tr("B", 1)]
    assert cvd_momentum(ws) == pytest.approx(1.0)


def test_cvd_momentum_empty_none():
    assert cvd_momentum([_tr("B", 1)]) is None


def test_quantile_sorted_interpolation():
    assert quantile_sorted([1, 2, 3, 4], 0.5) == pytest.approx(2.5)
    assert quantile_sorted([], 0.5) == 0.0
