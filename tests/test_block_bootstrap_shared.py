# -*- coding: utf-8 -*-
"""共享 block_bootstrap_ci 单一事实实现的守卫测试。"""
from __future__ import annotations

import pytest

from hermes_trader.validation import block_bootstrap_ci


def test_negative_series_ci_below_zero():
    s = [-5.0 - (i % 5) * 0.4 for i in range(120)]
    lo, hi = block_bootstrap_ci(s, boot=1000, seed=7)
    assert hi < 0
    assert lo < hi


def test_positive_series_ci_above_zero():
    s = [5.0 + (i % 7) * 0.2 for i in range(120)]
    lo, hi = block_bootstrap_ci(s, boot=1000, seed=7)
    assert lo > 0


def test_deterministic_for_same_seed():
    s = [(i % 9) - 4 for i in range(90)]
    a = block_bootstrap_ci(s, boot=500, seed=123)
    b = block_bootstrap_ci(s, boot=500, seed=123)
    assert a == b


def test_seed_changes_are_close():
    s = [(i % 9) - 4 for i in range(90)]
    a = block_bootstrap_ci(s, boot=2000, seed=1)
    b = block_bootstrap_ci(s, boot=2000, seed=2)
    assert abs(a[0] - b[0]) < 3
    assert abs(a[1] - b[1]) < 3


def test_rejects_too_short():
    with pytest.raises(ValueError):
        block_bootstrap_ci([1.0], boot=100)
