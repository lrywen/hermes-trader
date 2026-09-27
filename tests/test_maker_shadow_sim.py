# -*- coding: utf-8 -*-
"""SHADOW maker 成交模拟器单元测试（零资金）。"""
from __future__ import annotations

from hermes_trader.execution.maker_shadow import (
    FillTimePolicy,
    ShadowMakerOrder,
    locate_posted_bar_idx,
    simulate_shadow_order,
    would_fill_on_bar,
)
from hermes_trader.models.types import Candle


def bar(t: int, o=100.0, h=100.0, l=100.0, c=100.0) -> Candle:
    return Candle(t=t, o=o, h=h, l=l, c=c, v=1.0)


def _order(is_buy=True, limit=99.0, posted=0, ttl=5):
    return ShadowMakerOrder(coin="BTC", is_buy=is_buy, size=0.5,
                            posted_bar_idx=posted, limit_px=limit,
                            post_mid_px=100.0, ttl_bars=ttl)


# ---------- would_fill_on_bar 触及边界 ----------

def test_buy_touches_when_low_at_limit():
    assert would_fill_on_bar(_order(True, 99.0), bar(1, l=99.0)) is True


def test_buy_no_fill_when_low_above_limit():
    assert would_fill_on_bar(_order(True, 99.0), bar(1, l=99.1)) is False


def test_sell_touches_when_high_at_limit():
    o = _order(False, limit=101.0)
    assert would_fill_on_bar(o, bar(1, h=101.0)) is True


def test_sell_no_fill_when_high_below_limit():
    o = _order(False, limit=101.0)
    assert would_fill_on_bar(o, bar(1, h=100.9)) is False


# ---------- PIT：不回溯挂单当根 ----------

def test_does_not_fill_on_posted_bar_even_if_it_touched():
    # 挂单 bar(idx0) 的 low 已深穿 99，若回溯会"成交"；正确实现必须从 idx1 起
    bars = [bar(0, l=90.0, c=95.0), bar(1, l=100.0), bar(2, l=100.0)]
    f = simulate_shadow_order(_order(True, limit=99.0, posted=0, ttl=3), bars)
    # idx1/idx2 的 low=100 > 99 → 不成交，撤销
    assert f.filled is False
    assert f.canceled is True


# ---------- 成交 + 派生指标 ----------

def test_buy_fills_on_first_touching_bar_and_post_mid():
    bars = [
        bar(0),                       # 挂单 bar
        bar(60_000, l=100.0),         # 不触
        bar(120_000, l=98.0, c=98.5), # 触及 99 → 成交
        bar(180_000, h=99.0, l=97.0), # 成交后 mid=(99+97)/2=98
    ]
    f = simulate_shadow_order(_order(True, limit=99.0, posted=0, ttl=5), bars)
    assert f.filled is True
    assert f.fill_bar_idx == 2
    assert f.fill_ms == 120_000
    assert f.resting_bars == 2
    assert f.fill_px == 99.0  # BAR_START 用挂单价
    assert f.post_fill_mid_px == 98.0
    # 买单成交后 mid 跌到 98 < fill 99 → 不利漂移为正（逆向选择）
    assert f.post_fill_mid_drift_bps > 0
    # maker edge：99 < post_mid 100 → 正
    assert f.maker_edge_bps > 0


def test_bar_end_policy_uses_close_price():
    bars = [
        bar(0),
        bar(60_000, l=98.0, c=98.2),  # 触及；BAR_END 成交价=close
    ]
    f = simulate_shadow_order(
        _order(True, limit=99.0, posted=0, ttl=2), bars,
        fill_time_policy=FillTimePolicy.BAR_END)
    assert f.filled is True
    assert f.fill_px == 98.2
    # 无后续 bar → 漂移不可计算
    assert f.post_fill_mid_px is None
    assert f.post_fill_mid_drift_bps is None


def test_ttl_expiry_cancels():
    # TTL=2，只有 idx3 才触价，但已超 TTL（deadline=2）
    bars = [bar(0), bar(60_000, l=100), bar(120_000, l=100),
            bar(180_000, l=95)]
    f = simulate_shadow_order(_order(True, limit=99.0, posted=0, ttl=2), bars)
    assert f.filled is False
    assert f.canceled is True
    assert f.resting_bars is None
    assert f.maker_edge_bps is None


# ---------- locate_posted_bar_idx：按真实 posted_at 锚定，防前视 ----------

def test_locate_picks_last_bar_closed_at_post_time():
    bars = [bar(0), bar(60_000), bar(120_000), bar(180_000)]
    # posted 2:30 (150k): t=60k closed at 120k; t=120k closes at 180k
    # (still forming) → posted idx = 1
    assert locate_posted_bar_idx(bars, 150_000) == 1


def test_locate_excludes_bar_still_forming_at_post():
    bars = [bar(0), bar(60_000)]
    # posted 1:00 sharp → bar t=0 just closed; t=60k is the forming bar
    assert locate_posted_bar_idx(bars, 60_000) == 0


def test_locate_none_when_no_closed_bar_precedes_post():
    bars = [bar(60_000)]
    assert locate_posted_bar_idx(bars, 90_000) is None


def test_fill_never_precedes_real_post_time():
    # Regression for SUI 2026-09-27: old code hard-coded posted idx 0 and
    # filled on an early bar before the order existed. Anchor the post bar by
    # timestamp; the touch must only occur on a LATER bar.
    bars = [
        bar(0, l=90.0),        # long before post — would "fill" if idx0 used
        bar(60_000, l=91.0),
        bar(120_000, l=100.0), # real post bar (closed at 180k)
        bar(180_000, l=100.0), # first bar after posting — does not touch 99
        bar(240_000, l=98.0),  # touches 99 → real fill
        bar(300_000, l=97.0),
    ]
    posted = 185_000
    pidx = locate_posted_bar_idx(bars, posted)
    assert pidx == 2
    f = simulate_shadow_order(
        _order(True, limit=99.0, posted=pidx, ttl=5), bars)
    assert f.filled is True
    assert f.fill_bar_idx == 4
    assert f.fill_ms == 240_000          # strictly after the post time
    assert f.resting_bars == 2
