"""P1-1 step ③ — characterization for _reconstruct_live_positions (S7).

Pins the restart-safe live-position reconstruction extracted from
maybe_execute: malformed asset_position rows are skipped (never abort the
pipeline), valid rows normalize side/size_usd, and coins the DSL registry
tracks but the live read missed are appended as $0 held positions (re-entry
backstop against pyramiding during a restart rehydration window).
"""
from __future__ import annotations

from hermes_trader.agents import executor

reconstruct = executor._reconstruct_live_positions


def _row(coin, szi):
    return {"position": {"coin": coin, "szi": szi}}


def test_valid_rows_normalize_side_and_size(monkeypatch):
    monkeypatch.setattr(executor, "active_position_coins", lambda: {})
    state = {"asset_positions": [_row("ETH", "2.0"), _row("BTC", "-1.5")]}
    out = reconstruct(state, entry_px=100.0)
    assert out == [
        {"coin": "ETH", "side": "long", "size_usd": 200.0},
        {"coin": "BTC", "side": "short", "size_usd": 150.0},
    ]


def test_malformed_rows_are_skipped_not_raised(monkeypatch):
    monkeypatch.setattr(executor, "active_position_coins", lambda: {})
    state = {"asset_positions": [
        "not-a-dict",
        {"no_position_key": 1},
        {"position": "not-a-dict"},
        {"position": {"coin": None, "szi": "1"}},   # missing coin
        {"position": {"coin": "ETH", "szi": "junk"}},  # bad szi
        _row("SOL", "1"),
    ]}
    out = reconstruct(state, entry_px=10.0)
    assert out == [{"coin": "SOL", "side": "long", "size_usd": 10.0}]


def test_missing_and_empty_asset_positions(monkeypatch):
    monkeypatch.setattr(executor, "active_position_coins", lambda: {})
    assert reconstruct({}, entry_px=100.0) == []
    assert reconstruct({"asset_positions": []}, entry_px=100.0) == []
    assert reconstruct({"asset_positions": None}, entry_px=100.0) == []


def test_dsl_tracked_coin_missing_from_live_read_is_backfilled(monkeypatch):
    # Live read knows only ETH; DSL registry still tracks xyz:SP500 (the
    # restart/pyramid scenario) — it must be appended as a held $0 position.
    monkeypatch.setattr(
        executor, "active_position_coins",
        lambda: {"ETH": "long", "xyz:SP500": "short"})
    out = reconstruct({"asset_positions": [_row("ETH", "1")]}, entry_px=50.0)
    by_coin = {p["coin"]: p for p in out}
    assert by_coin["ETH"] == {"coin": "ETH", "side": "long", "size_usd": 50.0}
    assert by_coin["xyz:SP500"] == {
        "coin": "xyz:SP500", "side": "short", "size_usd": 0}


def test_dsl_tracked_coin_already_live_is_not_duplicated(monkeypatch):
    monkeypatch.setattr(executor, "active_position_coins",
                        lambda: {"ETH": "long"})
    out = reconstruct({"asset_positions": [_row("ETH", "3")]}, entry_px=10.0)
    eth = [p for p in out if p["coin"] == "ETH"]
    assert len(eth) == 1
    assert eth[0]["size_usd"] == 30.0
