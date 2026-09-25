"""Characterization tests for the two post-read account floor decision leaves.

Both helpers are pure gates over already-read account state; these tests pin
their fail-closed / threshold / formatting behaviour after the verbatim
extraction from maybe_execute.
"""

import pytest

from hermes_trader.agents.executor import (
    _free_margin_floor_block,
    _min_equity_floor_block,
)

# ── C11 hard equity floor ──────────────────────────────────────────────────


def test_c11_below_floor_blocks():
    res = _min_equity_floor_block(aid="a1", mode="live",
                                  config={"min_tradable_equity_usd": 10.0},
                                  agg_equity=8.0)
    assert res == {
        "executed": False, "mode": "live", "analysis_id": "a1",
        "reason": ("below_min_tradable_equity (aggregate equity $8.00 "
                   "< floor $10.00) — fail-closed, no new entries"),
    }


def test_c11_at_floor_does_not_block():
    # Strict `<`: equality clears the gate.
    assert _min_equity_floor_block(
        aid="a1", mode="live", config={"min_tradable_equity_usd": 10.0},
        agg_equity=10.0) is None


def test_c11_above_floor_does_not_block():
    assert _min_equity_floor_block(
        aid="a1", mode="live", config={"min_tradable_equity_usd": 10.0},
        agg_equity=1000.0) is None


def test_c11_zero_threshold_disables_gate_even_for_dust():
    assert _min_equity_floor_block(
        aid="a1", mode="live", config={"min_tradable_equity_usd": 0.0},
        agg_equity=1.0) is None


def test_c11_missing_config_uses_default_10():
    # Absent key -> cfg_get default 10.0: $8 account blocks.
    res = _min_equity_floor_block(aid="a1", mode="shadow", config={}, agg_equity=8.0)
    assert res is not None
    assert res["reason"].startswith("below_min_tradable_equity")
    assert res["mode"] == "shadow"


def test_c11_non_numeric_threshold_falls_back_to_10():
    # float("abc") raises inside the helper -> conservative $10 default.
    res = _min_equity_floor_block(
        aid="a1", mode="live", config={"min_tradable_equity_usd": "abc"},
        agg_equity=5.0)
    assert res is not None
    assert "< floor $10.00" in res["reason"]


def test_c11_non_positive_aggregate_equity_never_blocks():
    # agg_equity == 0 is handled by the upstream equity_unavailable gate; the
    # floor itself stays inert (its condition requires agg_equity > 0).
    assert _min_equity_floor_block(
        aid="a1", mode="live", config={"min_tradable_equity_usd": 10.0},
        agg_equity=0.0) is None


# ── Free-margin floor ──────────────────────────────────────────────────────


def test_margin_below_floor_blocks_on_main_dex():
    res = _free_margin_floor_block(aid="a1", mode="live",
                                   config={"min_available_margin_pct": 0.10},
                                   equity=100.0, available=5.0, target_dex="")
    assert res is not None
    assert res["executed"] is False and res["mode"] == "live"
    assert res["reason"] == (
        "insufficient_free_margin on dex 'main' "
        "(available $5.00 / equity $100.00 = 5.0%, floor 10%)")


def test_margin_below_floor_blocks_names_target_dex():
    res = _free_margin_floor_block(aid="a1", mode="live",
                                   config={"min_available_margin_pct": 0.10},
                                   equity=100.0, available=5.0, target_dex="xyz")
    assert "on dex 'xyz'" in res["reason"]


def test_margin_at_floor_does_not_block():
    # Strict `<`: exactly 10% clears.
    assert _free_margin_floor_block(
        aid="a1", mode="live", config={"min_available_margin_pct": 0.10},
        equity=100.0, available=10.0, target_dex="") is None


def test_margin_above_floor_does_not_block():
    assert _free_margin_floor_block(
        aid="a1", mode="live", config={"min_available_margin_pct": 0.10},
        equity=100.0, available=50.0, target_dex="") is None


def test_margin_zero_threshold_disables_gate():
    assert _free_margin_floor_block(
        aid="a1", mode="live", config={"min_available_margin_pct": 0.0},
        equity=100.0, available=0.0, target_dex="") is None


def test_margin_missing_config_defaults_to_20pct():
    res = _free_margin_floor_block(aid="a1", mode="live", config={},
                                   equity=100.0, available=9.0, target_dex=None)
    assert res is not None
    assert "floor 20%" in res["reason"]


def test_margin_non_positive_equity_inert():
    # Caller already rejected equity <= 0; the helper never divides wrongly.
    assert _free_margin_floor_block(
        aid="a1", mode="live", config={"min_available_margin_pct": 0.10},
        equity=0.0, available=0.0, target_dex="") is None


def test_floor_helpers_require_keyword_arguments():
    with pytest.raises(TypeError):
        _min_equity_floor_block("a1", "live", {}, 8.0)
    with pytest.raises(TypeError):
        _free_margin_floor_block("a1", "live", {}, 100.0, 5.0, "")
