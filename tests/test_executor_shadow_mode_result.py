"""P1-1 step ③ — characterization for _shadow_mode_result (S11).

Pins the SHADOW-mode terminal branch extracted from maybe_execute: it records
the would-be fill into the isolated shadow ledger (best-effort, never raises)
and returns the non-executed shadow result dict. Covers the real execution
path (shadow_open actually invoked with the computed size), the non-positive
entry skip, and non-fatal ledger/regime failures.
"""
from __future__ import annotations

import types

from hermes_trader.agents import executor

shadow_result = executor._shadow_mode_result


def _patch_shadow_book(monkeypatch, captured, *, open_raises=False):
    import hermes_trader.agents as agents_pkg
    from hermes_trader.agents import market_regime

    def _shadow_open(**kwargs):
        if open_raises:
            raise RuntimeError("ledger boom")
        captured.update(kwargs)

    # shadow_book is reached via `from hermes_trader.agents import shadow_book`
    # (a package attribute), so patching the package attribute intercepts it
    # reliably whether or not it was already imported.
    monkeypatch.setattr(
        agents_pkg, "shadow_book",
        types.SimpleNamespace(shadow_open=_shadow_open), raising=False)
    # detect_regime is reached via
    # `from hermes_trader.agents.market_regime import detect_regime`, which
    # reads the real submodule's attribute — patch it there.
    monkeypatch.setattr(market_regime, "detect_regime",
                        lambda coin: "trend_up")


def test_shadow_result_records_fill_and_returns_non_executed(monkeypatch):
    captured = {}
    _patch_shadow_book(monkeypatch, captured)
    res = shadow_result(
        mode="SHADOW", analysis_id="a9", coin="ETH", trade_side="long",
        mid_price=100.0, atr=2.0, trade_notional=800.0, leverage=5.0,
        gate_results={"g": 1})
    assert res == {
        "executed": False, "mode": "SHADOW", "analysis_id": "a9",
        "reason": "shadow_mode_would_execute",
        "gate_results": {"g": 1}, "size_usd": 800.0,
    }
    # Paper-book call carries the computed atr pct (2/100*100 = 2.0) + regime.
    assert captured["coin"] == "ETH"
    assert captured["side"] == "long"
    assert captured["entry_px"] == 100.0
    assert captured["size_usd"] == 800.0
    assert captured["leverage"] == 5.0
    assert captured["entry_atr_pct"] == 2.0
    assert captured["entry_regime"] == "trend_up"
    assert captured["analysis_id"] == "a9"


def test_shadow_result_skips_ledger_when_entry_nonpositive(monkeypatch):
    captured = {}
    _patch_shadow_book(monkeypatch, captured)
    res = shadow_result(
        mode="SHADOW", analysis_id="a10", coin="ETH", trade_side="short",
        mid_price=0.0, atr=0.0, trade_notional=100.0, leverage=3.0,
        gate_results={})
    assert res["reason"] == "shadow_mode_would_execute"
    assert captured == {}  # shadow_open never called without a valid entry


def test_shadow_result_swallows_ledger_failure(monkeypatch):
    captured = {}
    _patch_shadow_book(monkeypatch, captured, open_raises=True)
    # Must not raise; still returns the shadow result dict.
    res = shadow_result(
        mode="SHADOW", analysis_id="a11", coin="ETH", trade_side="long",
        mid_price=50.0, atr=1.0, trade_notional=200.0, leverage=2.0,
        gate_results={})
    assert res["executed"] is False
    assert res["reason"] == "shadow_mode_would_execute"


def test_shadow_result_swallows_regime_failure(monkeypatch):
    from hermes_trader.agents import market_regime

    captured = {}
    _patch_shadow_book(monkeypatch, captured)

    def _boom(coin):
        raise RuntimeError("regime fetch failed")

    monkeypatch.setattr(market_regime, "detect_regime", _boom)
    res = shadow_result(
        mode="SHADOW", analysis_id="a12", coin="BTC", trade_side="long",
        mid_price=50_000.0, atr=500.0, trade_notional=300.0, leverage=4.0,
        gate_results={})
    assert res["reason"] == "shadow_mode_would_execute"
    # Regime failure degrades to "" but the fill is still paper-booked.
    assert captured["entry_regime"] == ""
    assert captured["coin"] == "BTC"
