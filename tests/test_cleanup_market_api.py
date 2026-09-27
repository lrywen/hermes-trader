"""Offline tests for the hermes-trader codebase.

Covers the pure/refactored logic — no network or Hyperliquid credentials
required. Run from the repo root: ``pytest`` (or ``python3 -m pytest``).

Network-dependent paths (live order placement, account-state fetches, the
OpenRouter research call, the full market scan) are not covered here — they
can only be exercised against the real exchange.
"""
import math
import pathlib

import pytest

from hermes_trader.models.types import Candle

ROOT = pathlib.Path(__file__).resolve().parents[1]
MCP_SCRIPT = str(ROOT / "scripts" / "hermes-mcp-server.py")


@pytest.fixture(autouse=True)
def _clear_dsl_trackers():
    """Isolate the DSL tracker registry between tests. The re-entry backstop in
    maybe_execute now reads dsl_exit._active_positions, so a tracker leaked by an
    earlier test would inject a phantom held-coin and block unrelated trades."""
    try:
        from hermes_trader.agents import dsl_exit
        dsl_exit._active_positions.clear()
    except Exception:
        pass
    yield
    try:
        from hermes_trader.agents import dsl_exit
        dsl_exit._active_positions.clear()
    except Exception:
        pass


def _candles(n=150):
    return [
        Candle(t=i, o=100 + i * 0.1, h=101 + i * 0.1, l=99 + i * 0.1,
               c=100 + i * 0.1 + math.sin(i) * 0.5, v=1000.0 + i)
        for i in range(n)
    ]


# ── models ──────────────────────────────────────────────────────────────


def test_hyperfeed_safe_float_default():
    from hermes_trader.agents.hyperfeed import _safe_float
    assert _safe_float("not-a-number", 7.0) == 7.0
    assert _safe_float(None) == 0.0
    assert _safe_float("3.5") == 3.5


def test_leaderboard_get_markets_ranks_by_volume(monkeypatch):
    from hermes_trader.agents import hyperfeed
    monkeypatch.setattr(hyperfeed, "get_universe", lambda: [
        {"coin": "SMALL", "type": "perp", "dayNtlVlm": 1e6, "openInterest": 1},
        {"coin": "BIG", "type": "perp", "dayNtlVlm": 1e9, "openInterest": 9},
        {"coin": "@SPOT", "type": "spot", "dayNtlVlm": 1e12},  # excluded
    ])
    out = hyperfeed.leaderboard_get_markets()["markets"]
    assert [m["asset"] for m in out] == ["BIG", "SMALL"]
    assert out[0]["rank"] == 1


def test_leaderboard_get_trader_positions_unwraps_and_coerces(monkeypatch):
    """Nested position unwrap, string-leverage coercion, szi==0 skip."""
    from hermes_trader.agents import hyperfeed
    monkeypatch.setattr(hyperfeed, "_http_post", lambda path, body: {
        "assetPositions": [
            {"position": {"coin": "BTC", "szi": "2.0", "entryPx": "60000",
                          "leverage": "10", "unrealizedPnl": "500"}},
            {"position": {"coin": "ETH", "szi": "0"}},  # skipped
        ],
    })
    out = hyperfeed.leaderboard_get_trader_positions("0xABC")["positions"]
    assert len(out) == 1
    assert out[0]["side"] == "long"
    assert out[0]["leverage"] == {"value": "10"}  # str coerced to obj


def test_leaderboard_get_trader_positions_empty_when_no_state(monkeypatch):
    from hermes_trader.agents import hyperfeed
    monkeypatch.setattr(hyperfeed, "_http_post", lambda path, body: None)
    assert hyperfeed.leaderboard_get_trader_positions("0xABC") == {"positions": []}


def test_market_get_asset_data_collects_candles_and_context(monkeypatch):
    from hermes_trader.agents import hyperfeed
    fake_candles = [Candle(t=1, o=1, h=2, l=0.5, c=1.5, v=100)]
    monkeypatch.setattr(hyperfeed, "fetch_hl_candles",
                        lambda asset, interval, n: fake_candles)
    monkeypatch.setattr(hyperfeed, "get_universe", lambda: [
        {"coin": "BTC", "funding": -0.0001, "openInterest": 5e7,
         "prevDayPx": 59000, "midPx": 60000, "dayNtlVlm": 1e9},
    ])
    out = hyperfeed.market_get_asset_data("BTC", intervals=["1h"])["data"]
    assert out["asset"] == "BTC"
    assert len(out["candles"]["1h"]) == 1
    assert out["funding_rate"] == -0.0001
    assert out["mid_px"] == 60000


def test_market_get_asset_data_candle_error_yields_empty(monkeypatch):
    from hermes_trader.agents import hyperfeed
    def boom(asset, interval, n):
        raise RuntimeError("rate limited")
    monkeypatch.setattr(hyperfeed, "fetch_hl_candles", boom)
    monkeypatch.setattr(hyperfeed, "get_universe", lambda: [])
    out = hyperfeed.market_get_asset_data("BTC", intervals=["5m"])["data"]
    assert out["candles"]["5m"] == []


def test_market_list_instruments_counts_and_strips(monkeypatch):
    from hermes_trader.agents import hyperfeed
    monkeypatch.setattr(hyperfeed, "get_universe", lambda: [
        {"coin": "BTC", "type": "perp", "maxLeverage": 40},
        {"coin": "@107", "type": "spot", "maxLeverage": 0},
    ])
    out = hyperfeed.market_list_instruments()
    assert out["counts"] == {"perps": 1, "spot": 1, "total": 2}
    symbols = {i["symbol"] for i in out["instruments"]}
    assert "107" in symbols  # @ stripped


def test_market_get_mids_passthrough(monkeypatch):
    from hermes_trader.agents import hyperfeed
    monkeypatch.setattr(hyperfeed, "fetch_all_mids", lambda: {"BTC": "60000"})
    assert hyperfeed.market_get_mids() == {"BTC": "60000"}


def test_discovery_get_trader_state_win_rate_is_percentage(monkeypatch):
    """win_rate is a 0-100 percentage; positions unwrapped; ROI computed."""
    from hermes_trader.agents import hyperfeed
    calls = {"clearinghouse": {
        "marginSummary": {"accountValue": "8000", "totalNtlPos": "4000"},
        "assetPositions": [
            {"position": {"coin": "BTC", "szi": "1", "entryPx": "60000",
                          "unrealizedPnl": "100", "leverage": {"value": "5"}}},
        ],
    }}
    def fake_post(path, body):
        if body["type"] == "clearinghouseState":
            return calls["clearinghouse"]
        if body["type"] == "userFills":
            return [{"closedPnl": "5"}, {"closedPnl": "-2"}, {"closedPnl": "3"}]
        return {}
    monkeypatch.setattr(hyperfeed, "_http_post", fake_post)
    out = hyperfeed.discovery_get_trader_state(["0xABC"])["data"]["traders"]
    assert len(out) == 1
    t = out[0]
    assert t["total_trades"] == 3
    assert abs(t["win_rate"] - (2 / 3 * 100)) < 1e-6  # 2 of 3 winners
    assert t["open_positions"] == 1


def test_fetch_funding_rate_formats_percent(monkeypatch):
    from hermes_trader.agents import research
    monkeypatch.setattr(research, "fetch_funding_history",
                        lambda coin, start: [{"fundingRate": "0.0001"}])
    assert research._fetch_funding_rate("BTC") == "0.0100%/hr"


def test_fetch_funding_rate_na_when_empty(monkeypatch):
    from hermes_trader.agents import research
    monkeypatch.setattr(research, "fetch_funding_history", lambda coin, start: [])
    assert research._fetch_funding_rate("BTC") == "N/A"
