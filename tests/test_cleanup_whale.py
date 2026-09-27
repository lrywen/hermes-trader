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


def test_whale_scan_bypass_surfaces_subgate_accumulation(monkeypatch):
    """A whale-flagged coin that scores BELOW the composite gate must be:
      - dropped when whale_scan_bypass is OFF (default), and
      - surfaced (with whale_signal attached) when whale_scan_bypass is ON.

    Regression for the dead-path bug: oi_funding_anomaly fires on FLAT price,
    which scores ~0 on momentum/breakout triggers, so without the bypass the
    coin never reaches the executor where the whale override lives.
    """
    from hermes_trader.agents import perception
    from hermes_trader.agents.config import get_config

    cfg = get_config()
    gate = cfg["scan"]["minCompositeScore"]
    # Flat candles → no momentum/breakout/trend triggers fire → score below gate.
    flat = [Candle(t=i, o=100.0, h=100.0, l=100.0, c=100.0, v=10.0) for i in range(120)]
    monkeypatch.setattr(perception, "_fetch_candles_sync",
                        lambda coin, interval, count, ttl, **kwargs: flat)
    market = {"coin": "TRX", "type": "perp", "dex": None}
    whale_signals = {"TRX": {"signal": "oi_funding_anomaly", "score": 0.8}}

    # OFF → dropped at the gate (result is None)
    ok, res = perception._scan_single_market(market, 100.0, cfg, gate, whale_signals,
                                             False)
    assert ok and res is None, f"expected drop with bypass OFF, got {res}"

    # ON → surfaced with the whale_signal attached so the executor can act
    ok, res = perception._scan_single_market(market, 100.0, cfg, gate, whale_signals,
                                             True)
    assert ok and isinstance(res, dict), f"expected surfaced result with bypass ON, got {res}"
    assert res["coin"] == "TRX"
    assert res["whale_signal"] == whale_signals["TRX"]
    assert res["composite_score"] < gate  # confirms it was genuinely sub-gate


def test_smart_money_concentration_accumulation_signal(monkeypatch):
    """oi>0 + negative funding → 'accumulation'; confidence scales w/ |funding|."""
    from hermes_trader.agents import whale_index
    monkeypatch.setattr(whale_index, "get_universe", lambda **_: [
        {"coin": "BTC", "type": "perp", "openInterest": 5e7,
         "dayNtlVlm": 1e9, "funding": -0.0002, "midPx": 60000},
    ])
    out = whale_index.smart_money_concentration()
    acc = [s for s in out if s["signal"] == "accumulation"]
    assert acc and acc[0]["coin"] == "BTC"
    assert acc[0]["confidence"] == 1.0  # |−0.0002| / 0.0001 capped at 1.0


def test_smart_money_concentration_volume_floor_filters(monkeypatch):
    """Markets under min_volume_usd are skipped entirely."""
    from hermes_trader.agents import whale_index
    monkeypatch.setattr(whale_index, "get_universe", lambda **_: [
        {"coin": "TINY", "type": "perp", "openInterest": 5e7,
         "dayNtlVlm": 100.0, "funding": -0.0002, "midPx": 1},
    ])
    assert whale_index.smart_money_concentration(min_volume_usd=1e6) == []


def test_smart_money_concentration_high_oi_branch(monkeypatch):
    """OI > 10× daily-volume-in-millions → 'high_oi_concentration'."""
    from hermes_trader.agents import whale_index
    # vol = $2M (→ 2.0 in millions), oi = 30 → ratio 15 > 10.
    monkeypatch.setattr(whale_index, "get_universe", lambda **_: [
        {"coin": "ETH", "type": "perp", "openInterest": 30,
         "dayNtlVlm": 2e6, "funding": 0.0001, "midPx": 3000},
    ])
    out = whale_index.smart_money_concentration()
    sigs = {s["signal"] for s in out}
    assert "high_oi_concentration" in sigs


def test_get_whale_signals_merges_by_coin(monkeypatch):
    """Concentration + anomaly signals for the same coin collapse into one
    entry whose max_confidence is the higher of the two."""
    from hermes_trader.agents import whale_index
    monkeypatch.setattr(whale_index, "smart_money_concentration",
                        lambda **k: [{"coin": "SOL", "confidence": 0.3}])
    monkeypatch.setattr(whale_index, "oi_funding_anomaly",
                        lambda **k: [{"coin": "SOL", "confidence": 0.8}])
    out = whale_index.get_whale_signals(min_confidence=0.1)
    assert len(out) == 1
    assert out[0]["coin"] == "SOL"
    assert out[0]["max_confidence"] == 0.8
    assert len(out[0]["signals"]) == 2


def test_get_whale_signals_filters_below_min_conf(monkeypatch):
    from hermes_trader.agents import whale_index
    monkeypatch.setattr(whale_index, "smart_money_concentration",
                        lambda **k: [{"coin": "LOW", "confidence": 0.05}])
    monkeypatch.setattr(whale_index, "oi_funding_anomaly", lambda **k: [])
    assert whale_index.get_whale_signals(min_confidence=0.1) == []


def test_whale_accumulation_map_keys_by_coin(monkeypatch):
    """whale_accumulation_map → {coin: signal} for anomalies above min_conf."""
    from hermes_trader.agents import whale_index
    monkeypatch.setattr(whale_index, "oi_funding_anomaly", lambda: [
        {"coin": "ARB", "confidence": 0.9},
        {"coin": "OP", "confidence": 0.01},  # below floor
    ])
    monkeypatch.setattr(whale_index, "oi_surge_accumulation", lambda: [])
    m = whale_index.whale_accumulation_map(min_confidence=0.05)
    assert set(m) == {"ARB"}
    assert m["ARB"]["confidence"] == 0.9


def test_oi_funding_anomaly_requires_flat_price(monkeypatch):
    """A 24h move ≥10% disqualifies the accumulation signal even with deep
    negative funding + high OI."""
    from hermes_trader.agents import whale_index
    monkeypatch.setattr(whale_index, "get_universe", lambda **_: [
        {"coin": "PUMP", "type": "perp", "openInterest": 5e7,
         "funding": -0.0006, "midPx": 130, "prevDayPx": 100},  # +30%
        {"coin": "FLAT", "type": "perp", "openInterest": 5e7,
         "funding": -0.0006, "midPx": 101, "prevDayPx": 100},  # +1%
    ])
    out = whale_index.oi_funding_anomaly()
    coins = {s["coin"] for s in out}
    assert coins == {"FLAT"}
