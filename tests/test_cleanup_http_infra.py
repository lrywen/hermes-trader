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


def test_token_bucket_deducts_and_blocks_on_exhaustion():
    from hermes_trader.client.rate_limit import TokenBucket
    # Capacity 40, refill 0 (no recovery) → 2× 20-weight acquires then fail.
    b = TokenBucket(capacity=40, refill_per_sec=0.0)
    assert b.acquire(20, max_wait=0.1) is True
    assert b.acquire(20, max_wait=0.1) is True
    assert b.acquire(20, max_wait=0.1) is False   # drained, no refill


def test_token_bucket_refills_over_time():
    import time

    from hermes_trader.client.rate_limit import TokenBucket
    # 20 tokens / 20 per sec → a full refill needs ~1.0s, so the "not enough
    # yet" acquire with max_wait=0.05 stays False even under heavy CPU
    # contention (the old 100/s version refilled within ~0.2s and went flaky
    # in the parallel full-suite run when >0.15s passed between statements).
    b = TokenBucket(capacity=20, refill_per_sec=20.0)
    assert b.acquire(20, max_wait=0.5) is True        # drains to 0
    assert b.acquire(20, max_wait=0.05) is False       # needs ~1s to refill
    time.sleep(1.5)                                    # ~30 tokens, margin for load
    # Generous max_wait: under a fully loaded -n 6 worker the process can be
    # descheduled well past 0.1s right here, and a tight wait then loses the
    # race against its own refill. The assertion is "refilled past 20", which
    # a 2s wait makes independent of scheduler latency.
    assert b.acquire(20, max_wait=2.0) is True         # refilled past 20


def test_endpoint_weight_mapping():
    from hermes_trader.client.rate_limit import endpoint_weight
    assert endpoint_weight("candleSnapshot") == 20
    assert endpoint_weight("allMids") == 2
    assert endpoint_weight("clearinghouseState") == 2
    assert endpoint_weight("userNonFundingLedgerUpdates") == 2
    assert endpoint_weight(None) == 20         # unknown → expensive bucket
    assert endpoint_weight("madeUpType") == 20


def test_ttl_cache_serves_within_ttl_and_refreshes_after():
    import time

    import hermes_trader.dashboard as d
    d._TTL_CACHE.clear()
    calls = {"n": 0}
    def producer():
        calls["n"] += 1
        return {"v": calls["n"]}

    # First call computes; second within TTL serves cache (no recompute).
    # TTL 1.0s: adjacent-statement scheduling delays under parallel test load
    # stay far below the expiry boundary (the old 0.5s TTL was flaky there).
    assert d._ttl_cached("k", 1.0, producer) == {"v": 1}
    assert d._ttl_cached("k", 1.0, producer) == {"v": 1}
    assert calls["n"] == 1

    # After TTL expires, recomputes. Sleep well past the TTL: under a fully
    # loaded parallel suite time.sleep can under-serve relative to the wall
    # clock only by over-sleeping, so 2x TTL keeps us unambiguously expired
    # even with scheduling jitter (1.1x flaked under -n 6).
    time.sleep(2.0)
    assert d._ttl_cached("k", 0.5, producer) == {"v": 2}
    assert calls["n"] == 2


def test_ttl_cache_keys_are_independent():
    import hermes_trader.dashboard as d
    d._TTL_CACHE.clear()
    assert d._ttl_cached("a", 5.0, lambda: 1) == 1
    assert d._ttl_cached("b", 5.0, lambda: 2) == 2
    # different keys don't collide
    assert d._ttl_cached("a", 5.0, lambda: 99) == 1


def test_ttl_cached_serves_then_refetches(monkeypatch):
    from hermes_trader import dashboard
    calls = {"n": 0}
    def fn():
        calls["n"] += 1
        return calls["n"]
    t = [1000.0]
    monkeypatch.setattr(dashboard.time, "time", lambda: t[0])
    dashboard._TTL_CACHE.pop("k", None)
    assert dashboard._ttl_cached("k", 5.0, fn) == 1
    assert dashboard._ttl_cached("k", 5.0, fn) == 1  # cache hit
    t[0] += 6.0
    assert dashboard._ttl_cached("k", 5.0, fn) == 2  # expired → refetch
    assert calls["n"] == 2
