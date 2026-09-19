"""Characterization tests for the pre-trade order-book spread gate leaf.

_spread_width_block is a pure decision over the spread read from a
successfully fetched order book against the resolved max_spread_pct ceiling;
these tests pin its strict greater-than rule and executable-style block
payload after the verbatim extraction from maybe_execute. The unreadable-book
fail-closed/fail-open (H3) branch lives at the call site and is not this leaf.
"""

import pytest

from hermes_trader.agents.executor import _spread_width_block


def test_spread_above_ceiling_blocks():
    block = _spread_width_block(
        aid="a1", mode="live", spread_pct=1.5, max_spread_pct=1.0)
    assert block == {
        "executed": False,
        "mode": "live",
        "analysis_id": "a1",
        "reason": "spread_too_wide (1.50% > 1.0%)",
    }


def test_spread_exactly_at_ceiling_clears_strict_greater_than():
    assert _spread_width_block(
        aid="a1", mode="live", spread_pct=1.0, max_spread_pct=1.0
    ) is None


def test_spread_just_above_ceiling_blocks():
    block = _spread_width_block(
        aid="a1", mode="shadow", spread_pct=1.01, max_spread_pct=1.0)
    assert block is not None
    assert block["reason"] == "spread_too_wide (1.01% > 1.0%)"
    assert block["mode"] == "shadow"
    assert block["analysis_id"] == "a1"


def test_tight_spread_clears():
    assert _spread_width_block(
        aid="a1", mode="live", spread_pct=0.3, max_spread_pct=1.0
    ) is None


def test_zero_spread_clears():
    assert _spread_width_block(
        aid="a1", mode="live", spread_pct=0.0, max_spread_pct=1.0
    ) is None


def test_custom_ceiling_is_honored():
    assert _spread_width_block(
        aid="a1", mode="live", spread_pct=0.5, max_spread_pct=0.25
    ) is not None
    assert _spread_width_block(
        aid="a1", mode="live", spread_pct=0.2, max_spread_pct=0.25
    ) is None


def test_requires_keyword_arguments():
    with pytest.raises(TypeError):
        _spread_width_block("a1", "live", 1.5, 1.0)
