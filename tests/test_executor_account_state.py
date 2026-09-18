"""P1-1 step ③ — characterization for _read_account_state (S6/S7).

Pins the per-target-dex clearinghouse account read extracted from
maybe_execute: HIP-3 coins read their own dex equity/available, main crypto
coins fall back to the top-level state equity/available, the read retries up
to twice on a $0 selected-dex equity (per-dex endpoint burst flake), and the
aggregated exposure-gate fields (agg_equity / total_open_notional) come from
the top-level state regardless of selected dex.
"""
from __future__ import annotations

from hermes_trader.agents import executor

read_account = executor._read_account_state


def _install_state(monkeypatch, states):
    """``states`` is a list of account-state dicts returned on successive reads."""
    calls = {"n": 0}
    # Avoid the 0.4s retry sleep slowing the test.
    monkeypatch.setattr(executor.time, "sleep", lambda _s: None)

    def _fetch(user, include_hip3=True):
        i = min(calls["n"], len(states) - 1)
        calls["n"] += 1
        return states[i]

    monkeypatch.setattr(executor, "fetch_account_state", _fetch)
    return calls


def test_main_dex_uses_top_level_equity_and_available(monkeypatch):
    _install_state(monkeypatch, [{
        "equity": 100.0, "available": 80.0, "total_ntl": 250.0,
        "dex_equity": {}, "dex_available": {},
    }])
    r = read_account("0xuser", "ETH")
    assert r.target_dex == ""
    assert r.equity == 100.0
    assert r.available == 80.0
    assert r.agg_equity == 100.0
    assert r.total_open_notional == 250.0


def test_hip3_dex_uses_its_own_clearinghouse_values(monkeypatch):
    _install_state(monkeypatch, [{
        "equity": 39.9, "available": 5.0, "total_ntl": 100.0,
        "dex_equity": {"xyz": 59.04, "": 39.9},
        "dex_available": {"xyz": 55.0, "": 5.0},
    }])
    r = read_account("0xuser", "xyz:DRAM")
    assert r.target_dex == "xyz"
    assert r.equity == 59.04       # xyz dex, not main 39.9
    assert r.available == 55.0
    # Aggregated gate inputs remain the top-level book values.
    assert r.agg_equity == 39.9
    assert r.total_open_notional == 100.0


def test_zero_equity_retries_twice_and_then_believes_zero(monkeypatch):
    calls = _install_state(monkeypatch, [
        {"equity": 0, "available": 0, "total_ntl": 0,
         "dex_equity": {}, "dex_available": {}},
        {"equity": 0, "available": 0, "total_ntl": 0,
         "dex_equity": {}, "dex_available": {}},
        {"equity": 0, "available": 0, "total_ntl": 0,
         "dex_equity": {}, "dex_available": {}},
    ])
    r = read_account("0xuser", "ETH")
    assert r.equity == 0
    assert calls["n"] == 3          # initial + 2 retries


def test_transient_zero_recovers_on_retry(monkeypatch):
    calls = _install_state(monkeypatch, [
        {"equity": 0, "available": 0, "total_ntl": 0,
         "dex_equity": {}, "dex_available": {}},
        {"equity": 120.0, "available": 90.0, "total_ntl": 300.0,
         "dex_equity": {}, "dex_available": {}},
    ])
    r = read_account("0xuser", "ETH")
    assert r.equity == 120.0
    assert r.available == 90.0
    assert calls["n"] == 2          # initial + one retry, then break


def test_positive_equity_does_not_retry(monkeypatch):
    calls = _install_state(monkeypatch, [
        {"equity": 50.0, "available": 40.0, "total_ntl": 0,
         "dex_equity": {}, "dex_available": {}},
    ])
    r = read_account("0xuser", "BTC")
    assert r.equity == 50.0
    assert calls["n"] == 1


def test_none_state_degrades_to_zeros(monkeypatch):
    _install_state(monkeypatch, [None])
    r = read_account("0xuser", "ETH")
    assert r.state == {}
    assert r.equity == 0
    assert r.available == 0
    assert r.agg_equity == 0
    assert r.total_open_notional == 0
