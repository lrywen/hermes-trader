"""Characterization tests for the pre-trade ATR volatility gate leaf.

_atr_volatility_block is a pure decision over already-fetched 4h ATR(14) and
mid price against the resolved max_atr_pct ceiling; these tests pin its
strict greater-than rule, non-positive-mid fallback and executable-style
block payload after the verbatim extraction from maybe_execute.
"""

import pytest

from hermes_trader.agents.executor import _atr_volatility_block


def test_atr_above_ceiling_blocks_with_hype_style_reason():
    block = _atr_volatility_block(
        aid="a1", mode="live", atr=28.75, mid_price=100.0, max_atr_pct=15.0)
    assert block == {
        "executed": False,
        "mode": "live",
        "analysis_id": "a1",
        "reason": "atr_too_high (28.75% > 15.0%)",
    }


def test_atr_exactly_at_ceiling_clears_strict_greater_than():
    # 1.5 / 100 * 100 == 15.0: equality must NOT block.
    assert _atr_volatility_block(
        aid="a1", mode="live", atr=1.5, mid_price=100.0, max_atr_pct=15.0
    ) is None


def test_atr_just_above_ceiling_blocks():
    block = _atr_volatility_block(
        aid="a1", mode="shadow", atr=15.01, mid_price=100.0, max_atr_pct=15.0)
    assert block is not None
    assert block["reason"] == "atr_too_high (15.01% > 15.0%)"
    assert block["mode"] == "shadow"
    assert block["analysis_id"] == "a1"


def test_atr_below_ceiling_clears():
    assert _atr_volatility_block(
        aid="a1", mode="live", atr=10.0, mid_price=100.0, max_atr_pct=15.0
    ) is None


def test_zero_atr_clears():
    assert _atr_volatility_block(
        aid="a1", mode="live", atr=0.0, mid_price=100.0, max_atr_pct=15.0
    ) is None


def test_non_positive_mid_reads_as_zero_percent_and_clears():
    # An unreadable price is rejected elsewhere as invalid_price; the gate
    # itself must not block on a divide-by-zero style reading.
    assert _atr_volatility_block(
        aid="a1", mode="live", atr=999.0, mid_price=0.0, max_atr_pct=15.0
    ) is None
    assert _atr_volatility_block(
        aid="a1", mode="live", atr=999.0, mid_price=-1.0, max_atr_pct=15.0
    ) is None


def test_custom_ceiling_is_honored():
    assert _atr_volatility_block(
        aid="a1", mode="live", atr=5.0, mid_price=100.0, max_atr_pct=3.0
    ) is not None
    assert _atr_volatility_block(
        aid="a1", mode="live", atr=2.0, mid_price=100.0, max_atr_pct=3.0
    ) is None


def test_reason_percentage_is_scaled_from_spot():
    block = _atr_volatility_block(
        aid="a1", mode="live", atr=12.0, mid_price=48.0, max_atr_pct=20.0)
    # 12 / 48 * 100 == 25.0%
    assert block["reason"] == "atr_too_high (25.00% > 20.0%)"


def test_requires_keyword_arguments():
    with pytest.raises(TypeError):
        _atr_volatility_block("a1", "live", 28.75, 100.0, 15.0)
