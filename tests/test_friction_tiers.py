"""Tests for scripts/friction_tiers.py (P1-6 realized-friction tier replay).

These pin the accounting conventions, not any particular production numbers:
bp decomposition is recomputed from recorded prices; dollars scale linearly
with the post-rounding notional; the $10.5 floor flag only fires when the
MINIMUM (not tick residual) forces the size above target.
"""

import importlib.util
import pathlib
import sys

import pytest

_SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "friction_tiers.py"
_spec = importlib.util.spec_from_file_location("friction_tiers", _SCRIPT)
ft = importlib.util.module_from_spec(_spec)
sys.modules["friction_tiers"] = ft
_spec.loader.exec_module(ft)  # type: ignore[union-attr]


def test_min_floor_binds_only_when_minimum_forces_size():
    # Integer-size coin, cheap price: $30 target wants 300 coins, min needs
    # 125 → target dominates, floor NOT bound.
    real, up, bound = ft.round_trip_notional("MEGA", 30.0, 0.10, {"MEGA": 0})
    assert bound is False and real == pytest.approx(30.0, abs=0.01) and up == pytest.approx(1.0)
    # Tiny $10 target on a cheap integer coin: min 125 coins @0.084 = $10.5 binds.
    real, up, bound = ft.round_trip_notional("MEGA", 10.0, 0.084, {"MEGA": 0})
    assert bound is True
    assert real == pytest.approx(10.5, abs=0.01) and up > 1.0


def test_ceil_to_tick_residual_does_not_flag_floor():
    # $1000 on a high-priced integer coin: target dwarfs the minimum; the tiny
    # ceil-to-tick residual must NOT be reported as a minimum-order uplift.
    real, up, bound = ft.round_trip_notional("XYZ", 1000.0, 50.0, {"XYZ": 0})
    assert bound is False
    assert up == pytest.approx(1.0, abs=0.02)


def test_missing_meta_returns_continuous_target():
    real, up, bound = ft.round_trip_notional("UNKNOWN", 30.0, 1.23, {})
    assert (real, up, bound) == (30.0, 1.0, False)


def test_decompose_long_winner_net_is_gross_minus_fee():
    close = {
        "side": "long", "notional_usd": 100.0, "entry_px": 100.0, "price": 101.0,
        "mid_px": 101.0, "gross_pnl_usd": 1.0, "fee_usd": 0.09, "realized_pnl_usd": 0.91,
    }
    open_fill = {"mid_px": 100.0}
    c = ft.decompose_fill(close, open_fill, "taker")
    assert c["gross_bps"] == pytest.approx(100.0)
    assert c["fee_bps"] == pytest.approx(9.0)
    assert c["net_bps"] == pytest.approx(91.0)
    assert c["mid_gross_bps"] == pytest.approx(100.0)
    assert c["fill_friction_bps"] == pytest.approx(0.0)


def test_decompose_short_direction_and_friction_sign():
    # Short: entry mid 100, exit mid 99 (+100bp), but fills 10bp worse.
    close = {
        "side": "short", "notional_usd": 100.0, "entry_px": 100.05, "price": 99.05,
        "mid_px": 99.0, "gross_pnl_usd": 0.9, "fee_usd": 0.09, "realized_pnl_usd": 0.81,
    }
    open_fill = {"mid_px": 100.0}
    c = ft.decompose_fill(close, open_fill, "taker")
    # decision-mid gross = +100bp (price fell); actual fill gross = +90bp.
    assert c["mid_gross_bps"] == pytest.approx(100.0)
    assert c["gross_bps"] == pytest.approx(90.0)
    assert c["fill_friction_bps"] == pytest.approx(-10.0)


def test_decompose_zero_notional_is_safe():
    c = ft.decompose_fill({"side": "long"}, None, "maker_shadow")
    assert c["net_bps"] == 0.0 and c["fee_bps"] == 0.0
    assert c["mid_gross_bps"] != c["mid_gross_bps"]  # NaN for maker (no mid path)


def test_dollars_scale_linearly_across_tiers(tmp_path):
    """End-to-end: same two fills replayed at 30/100/1000 — bp medians are
    size-invariant; sums scale 1:10:100; win rate is unchanged."""
    book = {
        "accounts": {
            "taker": {"fills": [
                {"type": "open", "position_id": "p1", "mid_px": 100.0},
                {"type": "close", "position_id": "p1", "coin": "AAA", "side": "long",
                 "ts": ft.SLIP_ERA_MS + 1, "entry_px": 100.0, "price": 101.0, "mid_px": 101.0,
                 "notional_usd": 30.0, "gross_pnl_usd": 0.30, "fee_usd": 0.027,
                 "realized_pnl_usd": 0.273, "reason": "max_loss", "mfe_pct": 0.1},
            ]},
        }
    }
    a = ft.analyze_account(book, "taker", (30.0, 100.0, 1000.0), meta={})
    t30, t100, t1k = (a["tiers"][t] for t in (30.0, 100.0, 1000.0))
    assert t30["sum_net_usd"] == pytest.approx(0.273, abs=1e-6)
    assert t100["sum_net_usd"] == pytest.approx(0.91, abs=1e-6)
    assert t1k["sum_net_usd"] == pytest.approx(9.1, abs=1e-6)
    assert t30["win_rate_pct"] == t100["win_rate_pct"] == t1k["win_rate_pct"] == 100.0
    # fees 9bp round trip at each tier.
    assert t30["sum_fee_usd"] == pytest.approx(0.027, abs=1e-6)
    assert t1k["sum_fee_usd"] == pytest.approx(0.9, abs=1e-6)


def test_load_meta_accepts_universe_shape(tmp_path):
    p = tmp_path / "meta.json"
    p.write_text('{"universe":[{"name":"BTC","szDecimals":5},{"name":"X","szDecimals":0}]}')
    assert ft.load_meta(str(p)) == {"BTC": 5, "X": 0}
    p2 = tmp_path / "meta2.json"
    p2.write_text('{"ETH": 4}')
    assert ft.load_meta(str(p2)) == {"ETH": 4}
    assert ft.load_meta(None) == {}
