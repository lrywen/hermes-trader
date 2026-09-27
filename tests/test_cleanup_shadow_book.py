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


def test_shadow_book_load_tolerates_malformed_rows(tmp_path):
    """P0-2c: one malformed position row must not abort tracker rehydration for
    the whole book — valid rows rehydrate, bad rows are evicted, wrong-typed
    list fields degrade to empty."""
    import json as _json
    import time as _time

    from hermes_trader.agents import shadow_book as sb

    path = tmp_path / "shadow.json"
    now_ms = int(_time.time() * 1000)
    good_btc = {"coin": "BTC", "side": "long", "entry_px": 50000.0,
                "leverage": 2, "opened_at": now_ms, "size_usd": 1000.0}
    good_eth = {"coin": "ETH", "side": "short", "entry_px": 2500.0,
                "leverage": 1, "opened_at": now_ms, "size_usd": 500.0}
    state = {
        "version": 1,
        "created_at": now_ms,
        "starting_balance": 10000.0,
        "wallet_balance": 10000.0,
        "positions": [
            good_btc,
            {"side": "long", "entry_px": 100.0},          # missing coin
            {"coin": "SOL", "side": "long"},               # missing entry_px
            "not-a-dict",                                  # malformed row
            good_eth,
        ],
        "fills": "junk",        # wrong type → degrades to []
        "equity_curve": [{"t": now_ms, "equity": 10001.0}],
        "closed_count": 3,
    }
    path.write_text(_json.dumps(state))

    book = sb.ShadowBook(path=str(path))
    # v1 flat state migrates under the taker account. Both good positions
    # survived; the three bad rows were evicted.
    taker = book.state["accounts"]["taker"]
    coins_sides = {(p["coin"], p["side"]) for p in taker["positions"]}
    assert coins_sides == {("BTC", "long"), ("ETH", "short")}
    # Both trackers rehydrated (previously one bad row killed ALL trackers).
    assert ("taker", book._key("BTC", "long")) in book._trackers
    assert ("taker", book._key("ETH", "short")) in book._trackers
    # Wrong-typed list field degraded, equity curve preserved.
    assert taker["fills"] == []
    assert len(taker["equity_curve"]) == 1
    assert taker["closed_count"] == 3
