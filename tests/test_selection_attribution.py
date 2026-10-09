"""Tests for selection -> forward-outcome attribution (A1).

Covers the loop that lets a shadow ranking/selection arm be promoted or
refuted: registration (chosen + deferred, idempotent, skips coins without a
base price), settlement (forward pct vs registered base price), and the
selected-vs-deferred summary with paired significance. Network access is
stubbed by monkeypatching the candleSnapshot fetch.
"""
from __future__ import annotations

import json

import pytest

from hermes_trader.agents import selection_attribution as sa


@pytest.fixture()
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_DATA_DIR", str(tmp_path))
    return tmp_path


def _register_cycle(data_dir, cycle, prices, horizon=72):
    return sa.register_pending(
        "signal_ranking",
        selections=[("AAA", 80.0), ("BBB", 60.0)],
        deferrals=[("CCC", 20.0)],
        base_prices=prices,
        cycle_ms=cycle,
        horizon_hours=horizon,
    )


def test_register_writes_pending_items(data_dir):
    n = _register_cycle(data_dir, 1_000, {"AAA": "100", "BBB": "50",
                                          "CCC": "10"})
    assert n == 3
    state = json.loads((data_dir / "selection_attribution.json").read_text())
    assert len(state["items"]) == 3
    by_coin = {it["coin"]: it for it in state["items"]}
    assert by_coin["AAA"]["bucket"] == "selected"
    assert by_coin["CCC"]["bucket"] == "deferred"
    assert by_coin["AAA"]["base_px"] == 100.0


def test_register_is_idempotent_same_cycle(data_dir):
    prices = {"AAA": "100", "BBB": "50", "CCC": "10"}
    assert _register_cycle(data_dir, 1_000, prices) == 3
    # Re-register the identical (source, coin, cycle): nothing new.
    assert _register_cycle(data_dir, 1_000, prices) == 0
    state = json.loads((data_dir / "selection_attribution.json").read_text())
    assert len(state["items"]) == 3


def test_register_skips_coins_without_base_price(data_dir):
    n = sa.register_pending(
        "signal_ranking",
        selections=[("AAA", 80.0), ("ZZZ", 10.0)],
        deferrals=[],
        base_prices={"AAA": "100"},  # ZZZ has no price
        cycle_ms=1_000,
    )
    assert n == 1


def test_register_strips_dex_prefix_for_price(data_dir):
    n = sa.register_pending(
        "signal_ranking",
        selections=[("xyz:IREN", 80.0)],
        deferrals=[],
        base_prices={"IREN": "42"},
        cycle_ms=1_000,
    )
    assert n == 1
    state = json.loads((data_dir / "selection_attribution.json").read_text())
    assert state["items"][0]["coin"] == "xyz:IREN"
    assert state["items"][0]["base_px"] == 42.0


def _state(data_dir):
    return json.loads((data_dir / "selection_attribution.json").read_text())


def _due_now(data_dir):
    """A now_ms just past every item's due_ms."""
    state = _state(data_dir)
    return max(it["due_ms"] for it in state["items"]) + 1


def test_settle_computes_forward_return(data_dir, monkeypatch):
    prices = {"AAA": "100", "BBB": "50", "CCC": "10"}
    _register_cycle(data_dir, 1_000, prices)

    # Forward close for the +72h bar. Returned price maps by bar-open ms.
    def fake_forward_close(coin, start_ms, horizon_hours):
        return {"AAA": 110.0, "BBB": 45.0, "CCC": 12.0}[coin]

    monkeypatch.setattr(sa, "_forward_close", fake_forward_close)

    settled = sa.settle_due(now_ms=_due_now(data_dir))
    assert settled == 3
    by_coin = {it["coin"]: it for it in _state(data_dir)["items"]}
    assert by_coin["AAA"]["fwd_pct"] == 10.0       # (110-100)/100
    assert by_coin["BBB"]["fwd_pct"] == -10.0      # (45-50)/50
    assert by_coin["CCC"]["fwd_pct"] == 20.0       # (12-10)/10


def test_settle_waits_for_horizon(data_dir, monkeypatch):
    prices = {"AAA": "100", "BBB": "50", "CCC": "10"}
    _register_cycle(data_dir, 1_000, prices)
    monkeypatch.setattr(
        sa, "_forward_close",
        lambda coin, start_ms, horizon_hours: 110.0)
    # Well before the earliest due_ms -> nothing settled.
    assert sa.settle_due(now_ms=1_000) == 0


def test_settle_retries_when_forward_bar_missing(data_dir, monkeypatch):
    prices = {"AAA": "100", "BBB": "50", "CCC": "10"}
    _register_cycle(data_dir, 1_000, prices)
    # Bar not printed yet -> left pending, no crash.
    monkeypatch.setattr(sa, "_forward_close",
                        lambda *a, **k: None)
    assert sa.settle_due(now_ms=_due_now(data_dir)) == 0
    assert all(it["fwd_pct"] is None for it in _state(data_dir)["items"])


def test_summary_selected_minus_deferred(data_dir, monkeypatch):
    prices = {"AAA": "100", "BBB": "50", "CCC": "10"}
    _register_cycle(data_dir, 1_000, prices)
    monkeypatch.setattr(
        sa, "_forward_close",
        lambda coin, start_ms, horizon_hours:
        {"AAA": 110.0, "BBB": 110.0, "CCC": 9.0}[coin])
    sa.settle_due(now_ms=_due_now(data_dir))

    summ = sa.attribution_summary("signal_ranking", horizon_hours=72)
    assert summ["n_selected"] == 2
    assert summ["n_deferred"] == 1
    # selected: AAA +10%, BBB +120% -> mean 65 ; deferred CCC -10 ; diff 75
    assert summ["mean_selected_pct"] == 65.0
    assert summ["mean_deferred_pct"] == -10.0
    assert summ["mean_diff_pct"] == 75.0
    assert summ["n_pairs"] == 1
    assert summ["p_value"] is not None
    assert 0.0 <= summ["p_value"] <= 1.0


def test_summary_empty_source(data_dir):
    summ = sa.attribution_summary("nonexistent")
    assert summ["n_selected"] == 0
    assert summ["p_value"] is None


def test_bare_coin_helper():
    assert sa.bare_coin("xyz:IREN") == "IREN"
    assert sa.bare_coin("BTC") == "BTC"
