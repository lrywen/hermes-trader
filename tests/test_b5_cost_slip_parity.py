"""B-5: pin the P5-1bd cost/slippage constants and flat-mode parity.

Locks the upstream research-script changes that P5-1bd shipped (measured cost
constants replacing the old assumed 5/5/15/10 bps) so they cannot silently
regress:

* round-trip fee 8.64 bps, entry/exit half-spread 0.31 bps, stop-delay 0.0;
* ``--slip-mode flat`` must reproduce the OLD single-constant regime EXACTLY
  (per-coin table ignored) — P5-1bd's 5,453-trade self-check was 0 deviation;
* per-coin mode returns the table value for listed coins and falls back to the
  flat constant for unlisted ones (case-insensitive).
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def btx():
    path = _REPO / "scripts" / "bt_ra_exch.py"
    spec = importlib.util.spec_from_file_location("bt_ra_exch_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod  # dataclasses resolves cls.__module__ via this
    spec.loader.exec_module(mod)
    return mod


def test_measured_cost_constants(btx):
    # P5-1bd: assumed 5/5/15/10 → measured 8.64 fee / 0.31 / 0.31 / 0.0.
    assert btx.ROUND_TRIP_FEE_BPS == pytest.approx(8.64)
    assert btx.DEFAULT_ENTRY_SLIP_BPS == pytest.approx(0.31)
    assert btx.DEFAULT_EXIT_SLIP_BPS == pytest.approx(0.31)
    assert btx.DEFAULT_STOP_DELAY_SLIP_BPS == pytest.approx(0.0)


def test_majors_table_covers_the_eight_known_coins(btx):
    for coin in ("BTC", "ETH", "SOL", "BNB", "XRP", "DOGE", "ADA", "AVAX"):
        assert coin in btx.PER_COIN_SLIP_BPS


def test_flat_mode_ignores_per_coin_table(btx):
    # Every coin resolves to the single flat constant → exact old-regime parity.
    flat = 0.31
    for coin in ("BTC", "ETH", "AVAX", "ZZZ", "MINA"):
        assert btx._slip_for(coin, flat, per_coin=False) == flat


def test_per_coin_mode_uses_table_then_falls_back(btx):
    assert btx._slip_for("btc", 0.31, per_coin=True) == btx.PER_COIN_SLIP_BPS["BTC"]
    # Unlisted coin falls back to the supplied flat constant.
    assert btx._slip_for("ZZZUNKNOWN", 0.31, per_coin=True) == 0.31
