"""P1-1 step ③ — characterization for _price_atr_guard (S9 entry).

Pins the fail-closed fresh-mid + 4h-ATR gate extracted from maybe_execute:
both positive returns (mid, atr, None); a non-positive live mid returns
invalid_price_for_<coin> without consulting ATR; a non-positive ATR returns
no_atr_no_stop. The trade is skipped (cost $0) rather than sized unstopped.
"""
from __future__ import annotations

from hermes_trader.agents import executor

guard = executor._price_atr_guard


def test_valid_price_and_atr_pass(monkeypatch):
    monkeypatch.setattr(executor, "get_hl_price", lambda coin: 1800.5)
    monkeypatch.setattr(executor, "get_hl_atr", lambda tf, n, coin: 12.75)
    mid, atr, reason = guard("ETH")
    assert (mid, atr, reason) == (1800.5, 12.75, None)


def test_zero_price_fails_closed_and_skips_atr(monkeypatch):
    atr_called = {"n": 0}

    def _atr(tf, n, coin):
        atr_called["n"] += 1
        return 10.0

    monkeypatch.setattr(executor, "get_hl_price", lambda coin: 0.0)
    monkeypatch.setattr(executor, "get_hl_atr", _atr)
    mid, atr, reason = guard("ETH")
    assert mid == 0.0 and atr == 0.0
    assert reason == "invalid_price_for_ETH"
    assert atr_called["n"] == 0          # ATR not consulted after bad price


def test_negative_price_fails_closed(monkeypatch):
    monkeypatch.setattr(executor, "get_hl_price", lambda coin: -1.0)
    _, _, reason = guard("BTC")
    assert reason == "invalid_price_for_BTC"


def test_zero_atr_fails_closed(monkeypatch):
    monkeypatch.setattr(executor, "get_hl_price", lambda coin: 50_000.0)
    monkeypatch.setattr(executor, "get_hl_atr", lambda tf, n, coin: 0.0)
    mid, atr, reason = guard("BTC")
    assert mid == 0.0 and atr == 0.0
    assert reason is not None
    assert reason.startswith("no_atr_no_stop (BTC:")
