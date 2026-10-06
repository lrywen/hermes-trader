"""M-1 OFI 因子的离线单测（Cont et al. 2014 公式逐情形核对）。"""
from __future__ import annotations

import pytest

from hermes_trader.agents.ofi import (
    aggregate_ofi,
    best_ofi_two_sided,
    depth_ofi,
    mid,
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
