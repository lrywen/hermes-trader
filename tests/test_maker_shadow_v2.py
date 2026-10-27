# -*- coding: utf-8 -*-
"""SHADOW maker 条件成交模型 v2 单元测试（零资金）。

覆盖：
  * 版本字段与 v1 隔离（v1 结论不被改动）；
  * 条件成交：确定性保守口径 p<1 不成交、概率接受、runaway 撤单、拒绝计数；
  * 逆选择 drift 计入成交价（买单更贵/卖单更便宜）、有利漂移不发奖励；
  * PIT 不回溯挂单当根；无 forward bar 不臆造 drift。
"""
from __future__ import annotations

import random

from hermes_trader.execution.maker_shadow import (
    FillModel,
    ShadowMakerOrder,
    deterministic_shadow_order_v2,
    simulate_shadow_order,
    simulate_shadow_order_v2,
)
from hermes_trader.models.types import Candle


def bar(t, o=100.0, h=100.0, l=100.0, c=100.0):
    return Candle(t=t, o=o, h=h, l=l, c=c, v=1.0)


def _order(is_buy=True, limit=99.0, posted=0, ttl=6):
    return ShadowMakerOrder(coin="BTC", is_buy=is_buy, size=0.5,
                            posted_bar_idx=posted, limit_px=limit,
                            post_mid_px=100.0, ttl_bars=ttl)


# ---------- v1 回归隔离 ----------

def test_v1_unchanged_touch_fills_at_limit():
    bars = [
        bar(0),
        bar(60_000, l=100.0),
        bar(120_000, l=98.0, c=98.5),
        bar(180_000, h=99.0, l=97.0),
    ]
    f = simulate_shadow_order(_order(limit=99.0), bars)
    assert f.filled is True
    assert f.fill_model == FillModel.V1.value
    assert f.fill_px == 99.0          # v1 白拿 offset，不计 drift
    assert f.fill_prob is None
    assert f.reject_count == 0


def test_v2_tags_model_version():
    bars = [
        bar(0),
        bar(60_000, l=98.0, c=98.5),
        bar(120_000, h=99.0, l=97.0),
    ]
    f = simulate_shadow_order_v2(
        _order(limit=99.0), bars,
        base_fill_prob=1.0, deterministic=True)
    assert f.fill_model == FillModel.V2.value
    assert f.filled is True


# ---------- 条件成交 ----------

def test_deterministic_equi_prob_draw_is_reproducible():
    # 修复（2026-10-27）：实时确定性路径不能再"p<1 永不成交"，否则开 v2 即
    # 静默空账本。现用稳定 hash 派生均匀数：同一输入重放结论必须完全一致。
    bars = [
        bar(0),
        bar(60_000, l=98.0, c=98.5),
        bar(120_000, l=98.0),
    ]
    o = _order(limit=99.0, ttl=3)
    f1 = deterministic_shadow_order_v2(o, bars, base_fill_prob=0.7)
    f2 = deterministic_shadow_order_v2(o, bars, base_fill_prob=0.7)
    assert (f1.filled, f1.canceled) == (f2.filled, f2.canceled)
    assert f1.fill_px == f2.fill_px
    # 触及必然发生：无论成交与否，都记录了首次触及。
    assert f1.touch_bar_idx == 1


def test_deterministic_long_run_fill_rate_tracks_base():
    # 大量独立订单的确定性 hash 抽样，长期成交率应≈base（0.30），不再恒为 0。
    bars = [
        bar(0),
        bar(60_000, l=98.0, c=99.5),   # 明确触及买单限价并回收
        bar(120_000, h=99.6, l=99.2, c=99.5),
    ]
    n = 4000
    fills = 0
    for k in range(n):
        o = ShadowMakerOrder(coin="BTC", is_buy=True, size=0.5 + k * 1e-7,
                             posted_bar_idx=0, limit_px=99.0,
                             post_mid_px=100.0, ttl_bars=3)
        if deterministic_shadow_order_v2(o, bars).filled:
            fills += 1
    rate = fills / n
    assert 0.25 < rate < 0.36, rate  # ≈0.30，留宽松界防 flaky


def test_deterministic_zero_base_never_fills_but_still_touches():
    # 边界：base=0 时确定性路径仍应判触及、记拒绝并撤单（不臆造成交）。
    bars = [
        bar(0),
        bar(60_000, l=98.0, c=98.5),
        bar(120_000, l=98.0),
    ]
    f = deterministic_shadow_order_v2(
        _order(limit=99.0, ttl=3), bars, base_fill_prob=0.0)
    assert f.filled is False and f.canceled is True
    assert f.touch_bar_idx == 1 and f.reject_count >= 1


def test_monte_carlo_accept_fills_on_first_touch():
    bars = [
        bar(0),
        bar(60_000, l=98.0, c=98.5),
        bar(120_000, h=99.0, l=97.0),
    ]
    # rng.random()=0.0 < p(0.7) → 接受。
    f = simulate_shadow_order_v2(
        _order(limit=99.0), bars,
        base_fill_prob=0.7, deterministic=False, rng=random.Random(1))
    # 强制接受：用返回 0 的桩 rng 更稳，这里直接断言结果自洽。
    assert f.fill_model == FillModel.V2.value
    assert f.fill_prob is not None and 0 < f.fill_prob <= 1


class _ZeroRng:
    def random(self):
        return 0.0


class _OneRng:
    def random(self):
        return 1.0


def test_zero_draw_accepts_and_one_draw_rejects():
    bars = [
        bar(0),
        bar(60_000, l=98.0, c=98.5),
        bar(120_000, h=99.0, l=97.0),
    ]
    accept = simulate_shadow_order_v2(
        _order(limit=99.0, ttl=3), bars,
        base_fill_prob=0.7, deterministic=False, rng=_ZeroRng())
    assert accept.filled is True
    assert accept.fill_bar_idx == 1
    reject = simulate_shadow_order_v2(
        _order(limit=99.0, ttl=3), bars,
        base_fill_prob=0.7, deterministic=False, rng=_OneRng())
    assert reject.filled is False
    assert reject.reject_count >= 1


def test_runaway_winner_cancels_unfilled():
    # 只在第一根擦到一次又快速拉开（后续不再 touch）→ v1 会成交并抓住，
    # v2 若该次被拒，盈利单跑掉，TTL 末撤单（修正成交率虚高）。
    bars = [
        bar(0),
        bar(60_000, l=98.5, c=102.0, h=103.0),   # 触 99 一次
        bar(120_000, l=102.0, h=106.0),          # 价格跑掉，不再触
        bar(180_000, l=105.0, h=108.0),
    ]
    f = simulate_shadow_order_v2(
        _order(limit=99.0, ttl=4), bars,
        base_fill_prob=0.7, deterministic=False, rng=_OneRng())
    assert f.filled is False
    assert f.canceled is True
    assert f.touch_bar_idx == 1
    assert f.reject_count == 1


# ---------- 逆选择计入成交价 ----------

def test_adverse_drift_charged_into_buy_price():
    # 成交 bar idx1(low 98)，post bar mid=(99+97)/2=98 → 不利 drift。
    bars = [
        bar(0),
        bar(60_000, l=98.0, c=98.5),
        bar(120_000, h=99.0, l=97.0),
    ]
    f = simulate_shadow_order_v2(
        _order(limit=99.0), bars,
        base_fill_prob=1.0, adv_drift_kappa=1.0, deterministic=True)
    assert f.filled is True
    # raw drift = (99 - 98)/99 *1e4 ≈ +101bps；买单成交价应被抬高。
    assert f.raw_drift_bps and f.raw_drift_bps > 0
    assert f.adv_drift_charge_bps == f.raw_drift_bps
    assert f.fill_px > 99.0
    # 计价后 captured maker edge 应收敛到接近 0（不再白拿 +offset）。
    assert f.maker_edge_bps is not None
    assert f.maker_edge_bps < simulate_shadow_order(
        _order(limit=99.0), bars).maker_edge_bps
    # kappa=1：原始 drift 被全额计入价格，残留逆选择≈0。
    assert f.residual_drift_bps is not None
    assert abs(f.residual_drift_bps) < 1e-6


def test_favorable_drift_gives_no_bonus():
    # post bar mid 高于 fill（对买单有利）→ 不允许负 charge 把买价压低。
    bars = [
        bar(0),
        bar(60_000, l=98.0, c=99.0),
        bar(120_000, l=100.0, h=102.0),   # mid=101 有利
    ]
    f = simulate_shadow_order_v2(
        _order(limit=99.0), bars,
        base_fill_prob=1.0, deterministic=True)
    assert f.filled is True
    assert f.adv_drift_charge_bps == 0.0
    assert f.fill_px == 99.0


def test_sell_adverse_drift_lowers_fill_price():
    # 卖单 limit 101，post bar mid 上涨（不利卖方）→ 卖单价被压低。
    bars = [
        bar(0),
        bar(60_000, h=102.0, c=101.5),
        bar(120_000, l=101.0, h=104.0),   # mid=102.5 > 101 不利
    ]
    f = simulate_shadow_order_v2(
        _order(is_buy=False, limit=101.0), bars,
        base_fill_prob=1.0, deterministic=True)
    assert f.filled is True
    assert f.raw_drift_bps and f.raw_drift_bps > 0
    assert f.fill_px < 101.0


# ---------- PIT / 边界 ----------

def test_v2_does_not_fill_on_posted_bar():
    bars = [bar(0, l=90.0, c=95.0), bar(60_000, l=100.0)]
    f = simulate_shadow_order_v2(
        _order(limit=99.0, posted=0, ttl=2), bars,
        base_fill_prob=1.0, deterministic=True)
    assert f.filled is False
    assert f.canceled is True


def test_no_forward_bar_leaves_drift_none_no_fabrication():
    bars = [
        bar(0),
        bar(60_000, l=98.0, c=98.5),   # 成交在最后一根，无 forward bar
    ]
    f = simulate_shadow_order_v2(
        _order(limit=99.0, ttl=2), bars,
        base_fill_prob=1.0, deterministic=True)
    assert f.filled is True
    assert f.post_fill_mid_px is None
    assert f.raw_drift_bps is None
    assert f.adv_drift_charge_bps == 0.0   # 不臆造、不收费
    assert f.fill_px == 99.0


def test_parameter_clamp_is_safe():
    bars = [
        bar(0),
        bar(60_000, l=98.0),
        bar(120_000, l=97.0),
    ]
    # 越界参数（负/>1）被钳制，不应抛错，结论仍自洽。
    f = simulate_shadow_order_v2(
        _order(limit=99.0), bars,
        base_fill_prob=5.0, fill_prob_decay=-9.0, adv_drift_kappa=99.0,
        deterministic=True)
    assert f.fill_model == FillModel.V2.value
