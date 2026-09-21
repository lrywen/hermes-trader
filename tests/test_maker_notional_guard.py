# -*- coding: utf-8 -*-
"""方案 A：post-only maker 下单 + $100 名义护栏单元测试。"""
from __future__ import annotations

import pytest

from hermes_trader.execution.maker import (
    HL_MIN_NOTIONAL_USD,
    MAKER_SAMPLE_MAX_NOTIONAL_USD,
    MakerNotionalError,
    validate_maker_notional,
)


def test_notional_within_bounds():
    n = validate_maker_notional(100.0, 0.5)  # $50
    assert n == pytest.approx(50.0)


def test_notional_at_exact_cap_ok():
    n = validate_maker_notional(100.0, 1.0)  # 恰好 $100
    assert n == pytest.approx(100.0)


def test_notional_above_cap_rejected():
    with pytest.raises(MakerNotionalError) as ei:
        validate_maker_notional(100.0, 1.5)  # $150
    assert "授权上限" in str(ei.value)


def test_notional_below_min_rejected():
    with pytest.raises(MakerNotionalError) as ei:
        validate_maker_notional(100.0, 0.05)  # $5
    assert "最小" in str(ei.value)


def test_nonpositive_inputs_rejected():
    with pytest.raises(MakerNotionalError):
        validate_maker_notional(0, 1)
    with pytest.raises(MakerNotionalError):
        validate_maker_notional(100, 0)


def test_custom_max_notional():
    n = validate_maker_notional(10.0, 2.0, max_notional=25.0)
    assert n == pytest.approx(20.0)
    with pytest.raises(MakerNotionalError):
        validate_maker_notional(10.0, 3.0, max_notional=25.0)


def test_constants_are_expected():
    assert MAKER_SAMPLE_MAX_NOTIONAL_USD == 100.0
    assert HL_MIN_NOTIONAL_USD == 10.0
