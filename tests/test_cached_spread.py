"""Tests for the observation-only cached spread reader (audit §spread_bps).

get_cached_orderbook_spread must read ONLY the L2 positive-result cache:
cold cache -> {"ok": False} with ZERO network fetch; warm cache -> spread_pct
from the cached book. It must never call _l2_snapshot_bounded.
"""
from __future__ import annotations

from hermes_trader.client import exchange as ex


def _book(bid: float, ask: float) -> dict:
    return {"levels": [[{"px": str(bid), "sz": "10"}],
                       [{"px": str(ask), "sz": "10"}]]}


def test_cold_cache_no_fetch(monkeypatch):
    ex._L2_CACHE.clear()

    def _boom(coin):
        raise AssertionError("must not fetch on a cold cache")

    monkeypatch.setattr(ex, "_l2_snapshot_bounded", _boom)
    out = ex.get_cached_orderbook_spread("ETH")
    assert out["ok"] is False
    assert out["error"] == "cold_cache"


def test_warm_cache_returns_spread(monkeypatch):
    ex._L2_CACHE["ETH"] = (ex._time.monotonic(), _book(100.0, 100.1))

    def _boom(coin):
        raise AssertionError("must not fetch when cache is warm")

    monkeypatch.setattr(ex, "_l2_snapshot_bounded", _boom)
    out = ex.get_cached_orderbook_spread("ETH")
    assert out["ok"] is True
    assert out["source"] == "l2_cache"
    # spread_pct = 0.1 / 100.05 * 100
    assert out["spread_pct"] > 0
    assert out["best_bid"] == 100.0 and out["best_ask"] == 100.1


def test_expired_cache_is_cold(monkeypatch):
    # Timestamp older than the TTL -> treated as cold, no fetch.
    ex._L2_CACHE["ETH"] = (
        ex._time.monotonic() - ex._L2_CACHE_TTL_S - 1.0, _book(100.0, 100.1))
    monkeypatch.setattr(
        ex, "_l2_snapshot_bounded",
        lambda coin: (_ for _ in ()).throw(AssertionError("no fetch")))
    out = ex.get_cached_orderbook_spread("ETH")
    assert out["ok"] is False and out["error"] == "cold_cache"
    ex._L2_CACHE.clear()


def test_empty_book_in_cache():
    ex._L2_CACHE["ETH"] = (ex._time.monotonic(), {"levels": []})
    out = ex.get_cached_orderbook_spread("ETH")
    assert out["ok"] is False and out["error"] == "empty_orderbook"
    ex._L2_CACHE.clear()
