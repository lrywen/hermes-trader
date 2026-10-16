"""Tests for net_exposure (Audit 2026-10-16 / problem #6)."""
from hermes_trader.agents.net_exposure import compute_net_exposure


def _hl(coin, szi, positionValue):
    return {"position": {"coin": coin, "szi": szi, "positionValue": positionValue}}


def _sb(coin, side, size_usd):
    return {"coin": coin, "side": side, "size_usd": size_usd}


def test_empty_positions_zero():
    r = compute_net_exposure([], 1000.0)
    assert r["n_positions"] == 0
    assert r["net_notional"] == 0.0
    assert r["net_frac"] == 0.0


def test_single_long_hl():
    r = compute_net_exposure([_hl("BTC", 0.01, 1000.0)], 5000.0)
    assert r["n_positions"] == 1
    assert r["net_notional"] == 1000.0
    assert r["long_notional"] == 1000.0
    assert r["short_notional"] == 0.0
    assert abs(r["net_frac"] - 0.2) < 1e-9


def test_netting_long_short_hl():
    # +2000 long, -1200 short => net +800 (not gross 3200)
    rows = [_hl("BTC", 0.02, 2000.0), _hl("SOL", -10.0, 1200.0)]
    r = compute_net_exposure(rows, 10000.0)
    assert r["n_positions"] == 2
    assert r["net_notional"] == 800.0
    assert r["gross_notional"] == 3200.0
    assert abs(r["net_frac"] - 0.08) < 1e-9
    assert abs(r["gross_frac"] - 0.32) < 1e-9


def test_shadow_rows_net():
    rows = [_sb("BTC", "long", 500.0), _sb("ETH", "short", 300.0)]
    r = compute_net_exposure(rows, 5000.0)
    assert r["n_positions"] == 2
    assert r["net_notional"] == 200.0
    assert abs(r["net_frac"] - 0.04) < 1e-9


def test_zero_szi_and_closed_skipped():
    rows = [_hl("BTC", 0, 0.0), _sb("ETH", "long", 0.0), _hl("SOL", -1.0, 100.0)]
    r = compute_net_exposure(rows, 1000.0)
    assert r["n_positions"] == 1
    assert r["net_notional"] == -100.0


def test_zero_equity_safe():
    r = compute_net_exposure([_hl("BTC", 0.01, 1000.0)], 0.0)
    assert r["net_frac"] == 0.0
    assert r["max_dir_units"] == 0.0
