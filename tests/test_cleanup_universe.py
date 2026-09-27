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


def test_classify_asset():
    from hermes_trader.agents.market_regime import classify_asset
    # crypto default
    assert classify_asset("BTC") == "crypto"
    assert classify_asset("PEPE") == "crypto"
    assert classify_asset("randomcoin42") == "crypto"
    # equity perps
    assert classify_asset("TSLA") == "equity"
    assert classify_asset("nvda") == "equity"   # case-insensitive
    assert classify_asset("MSTR") == "equity"
    # commodity perps
    assert classify_asset("NATGAS") == "commodity"
    assert classify_asset("SILVER") == "commodity"


def test_classify_asset_hip3_namespaced(monkeypatch):
    """HIP-3 venues are mixed: unknown tokenized stocks default to equity (not
    the BTC-trend crypto default), but crypto names listed on a HIP-3 dex still
    resolve to crypto via the native-perp ticker set."""
    from hermes_trader.agents import market_regime as mr
    # Pretend the native HL dex lists these crypto majors.
    monkeypatch.setattr(mr, "_crypto_tickers_cache",
                        frozenset({"BTC", "ETH", "LINK", "FARTCOIN", "XMR"}))
    # Unknown tokenized stock (not in the allowlist) → equity, NOT crypto.
    assert mr.classify_asset("xyz:SNDK") == "equity"
    assert mr.classify_asset("xyz:CBRS") == "equity"
    # Known equity / commodity allowlist entries still win.
    assert mr.classify_asset("xyz:NVDA") == "equity"
    assert mr.classify_asset("xyz:GOLD") == "commodity"
    assert mr.classify_asset("km:USOIL") == "commodity"
    # Crypto names on a HIP-3 dex resolve to crypto via the native set.
    assert mr.classify_asset("hyna:BTC") == "crypto"
    assert mr.classify_asset("hyna:LINK") == "crypto"
    assert mr.classify_asset("cash:ETH") == "crypto"
    assert mr.classify_asset("flx:XMR") == "crypto"


def test_native_crypto_tickers_skips_namespaced_and_caches(monkeypatch):
    """_native_crypto_tickers pulls only main-dex perps (no ':') and caches."""
    from hermes_trader.agents import market_regime as mr
    mr._crypto_tickers_cache = None
    calls = {"n": 0}
    def fake_universe(**kw):
        calls["n"] += 1
        return [
            {"coin": "BTC", "type": "perp"},
            {"coin": "ETH", "type": "perp"},
            {"coin": "xyz:NVDA", "type": "perp"},  # namespaced → excluded
            {"coin": "@107", "type": "spot"},      # spot → excluded
        ]
    monkeypatch.setattr("hermes_trader.client.universe.get_universe", fake_universe)
    out = mr._native_crypto_tickers()
    assert out == frozenset({"BTC", "ETH"})
    mr._native_crypto_tickers()  # second call served from cache
    assert calls["n"] == 1
