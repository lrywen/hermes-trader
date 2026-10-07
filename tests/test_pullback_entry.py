"""pullback_entry 模块测试：上升趋势内回踩支撑 + 止跌确认。"""
from __future__ import annotations

from hermes_trader.agents import pullback_entry as pe
from hermes_trader.indicators.math import atr as atr_arr


class _C:
    __slots__ = ("o", "h", "l", "c")

    def __init__(self, o, h, l, c):
        self.o, self.h, self.l, self.c = o, h, l, c


def _uptrend(n=60, start=100.0, step=1.0):
    """干净的阶梯式上涨。"""
    out = []
    p = start
    for i in range(n):
        out.append(_C(p, p + step, p - 0.2, p + step))
        p += step
    return out


def _atr(candles):
    arr = atr_arr(candles, 14)
    for v in reversed(arr):
        if v == v and v > 0 and v != float("inf"):
            return v
    return 0.0


def test_established_uptrend_true() -> None:
    c = _uptrend()
    assert pe.established_uptrend(c, trend_ema_p=50, lookback=50) is True


def test_no_uptrend_on_flat() -> None:
    flat = [_C(100, 100.5, 99.5, 100) for _ in range(70)]
    assert pe.established_uptrend(flat) is False


def test_corrected_from_high() -> None:
    c = _uptrend()
    # 末根跌回：在高点下回撤超过 1.5%
    p = c[-1].c
    c.append(_C(p, p, p - 3, p - 3))
    assert pe.corrected_from_high(c, min_pull_pct=0.015) is True


def test_composite_requires_all_conditions() -> None:
    # 只有上涨，没有回踩+止跌 → invalid
    c = _uptrend()
    sig = pe.pullback_entry(c, atr=_atr(c))
    assert sig.valid is False


def test_pullback_at_support_with_turn_up() -> None:
    # 上涨后让价格回到 EMA21 附近并重新收阳
    c = _uptrend(70)
    ema21_approx = sum(x.c for x in c[-21:]) / 21.0
    # 构造回踩：两根下行到支撑附近
    p = c[-1].c
    c.append(_C(p, p, ema21_approx - 0.5, ema21_approx - 0.3))
    c.append(_C(ema21_approx - 0.3, ema21_approx + 0.5,
                ema21_approx - 0.6, ema21_approx - 0.4))
    # 止跌 + 重新收阳
    base = ema21_approx
    c.append(_C(base - 0.1, base + 0.8, base - 0.2, base + 0.6))
    sig = pe.pullback_entry(c, atr=_atr(c), min_pull_pct=0.01)
    assert sig.valid is True
    assert sig.stop_px < sig.entry_px
    assert sig.support in ("ema", "swing_high")


def test_fresh_up_bar_detection() -> None:
    c = _uptrend(3)
    assert pe.fresh_up_bar(c) is True
    down = _uptrend(2)
    p = down[-1].c
    down.append(_C(p, p, p - 1, p - 1))
    assert pe.fresh_up_bar(down) is False


def test_crash_bar_detects_sharp_down() -> None:
    c = _uptrend(5)
    p = c[-1].c
    # 单根 -2% 急跌
    c.append(_C(p, p, p * 0.98, p * 0.98))
    assert pe.crash_bar(c, min_drop_pct=0.015) is True


def test_crash_bar_ignores_normal_pullback() -> None:
    c = _uptrend(5)
    p = c[-1].c
    # 仅 -0.5% 的普通回调，不算 crash
    c.append(_C(p, p, p * 0.995, p * 0.995))
    assert pe.crash_bar(c, min_drop_pct=0.015) is False


def test_crash_bar_lookback_window() -> None:
    c = _uptrend(5)
    p = c[-1].c
    c.append(_C(p, p, p * 0.98, p * 0.98))  # crash
    # crash 后再跟一根阳线；lookback=1 只看末根 → 无 crash
    up = c[-1].c
    c.append(_C(up, up + 1, up - 0.2, up + 1))
    assert pe.crash_bar(c, min_drop_pct=0.015, lookback=1) is False
    assert pe.crash_bar(c, min_drop_pct=0.015, lookback=2) is True
