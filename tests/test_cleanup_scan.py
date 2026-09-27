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


def _scan_with_config(monkeypatch, cfg):
    """Scaffolding: run perception.scan_once with a fake universe + config,
    return the list of coins that actually got candle-fetched."""
    from hermes_trader.agents import perception
    universe = [
        {"coin": "BTC", "type": "perp", "dex": None, "dayNtlVlm": 9e9},
        {"coin": "ETH", "type": "perp", "dex": None, "dayNtlVlm": 5e9},
        {"coin": "xyz:MU", "type": "perp", "dex": "xyz", "dayNtlVlm": 2.7e8},
        {"coin": "xyz:CRCL", "type": "perp", "dex": "xyz", "dayNtlVlm": 3.4e7},
    ]
    mids = {m["coin"]: "100" for m in universe}
    monkeypatch.setenv("HERMES_MAX_MARKETS", "10")
    monkeypatch.setenv("HERMES_MAX_MARKETS_HIP3", "5")
    monkeypatch.setenv("HERMES_MAX_MARKETS_MOVERS", "0")  # tested separately
    monkeypatch.setenv("HERMES_UNIVERSE_SWEEP", "0")
    monkeypatch.setattr(perception, "fetch_all_mids", lambda include_hip3=False: mids)
    monkeypatch.setattr(perception, "get_universe", lambda include_hip3=False: universe)
    monkeypatch.setattr("hermes_trader.agents.config_store.read_agent_config",
                        lambda: cfg)
    seen = []
    monkeypatch.setattr(perception, "_scan_single_market",
                        lambda m, mid, c, ms, ws=None, wsb=False, tse=True: (seen.append(m["coin"]), (True, None))[1])
    perception.scan_once(min_score=0)
    return seen


def test_scan_bucket_split_keeps_hip3_slice(monkeypatch):
    """With include_hip3=True the scanner reserves HERMES_MAX_MARKETS_HIP3
    slots for HIP-3 markets so high-volume crypto doesn't crowd them out."""
    from hermes_trader.agents import perception

    # 100 fake crypto markets (higher volume) + 10 HIP-3 markets.
    # HIP-3 entries get prevDayPx + midPx so the mover sub-bucket has
    # qualifying candidates (with the new vol+mover split for HIP-3).
    universe = [
        {"coin": f"C{i}", "type": "perp", "dex": None, "dayNtlVlm": 1_000_000_000 - i}
        for i in range(100)
    ] + [
        {"coin": f"xyz:H{i}", "type": "perp", "dex": "xyz",
         "dayNtlVlm": 50_000_000 - i,
         "prevDayPx": 100.0, "midPx": 105.0 + i * 0.1}
        for i in range(10)
    ]
    # mids = the LIVE current price (production reads the 24h move from fresh
    # mids, not the cached universe midPx) — mirror each coin's midPx here.
    mids = {m["coin"]: str(m.get("midPx", 100)) for m in universe}

    monkeypatch.setenv("HERMES_MAX_MARKETS", "10")
    monkeypatch.setenv("HERMES_MAX_MARKETS_HIP3", "3")
    monkeypatch.setenv("HERMES_MAX_MARKETS_MOVERS", "0")  # tested separately
    monkeypatch.setenv("HERMES_UNIVERSE_SWEEP", "0")
    monkeypatch.setattr(perception, "fetch_all_mids", lambda include_hip3=False: mids)
    monkeypatch.setattr(perception, "get_universe", lambda include_hip3=False: universe)
    monkeypatch.setattr(perception, "_scan_single_market", lambda m, mid, cfg, ms, ws=None, wsb=False, tse=True: (True, None))
    # Force include_hip3=True via the runtime config
    monkeypatch.setattr("hermes_trader.agents.config_store.read_agent_config",
                        lambda: {"enable_hip3": True})

    seen = []
    real_scan = perception._scan_single_market
    def _capture(m, mid, cfg, ms, ws=None, wsb=False, tse=True):
        seen.append(m["coin"])
        return (True, None)
    monkeypatch.setattr(perception, "_scan_single_market", _capture)

    perception.scan_once(min_score=0)
    crypto_seen = [c for c in seen if not c.startswith("xyz:")]
    hip3_seen = [c for c in seen if c.startswith("xyz:")]
    # Crypto budget = 10 - 3 = 7; HIP-3 budget = 3
    assert len(crypto_seen) == 7, f"crypto picked: {crypto_seen}"
    assert len(hip3_seen) == 3, f"hip3 picked: {hip3_seen}"


def test_scan_crypto_only_skips_hip3(monkeypatch):
    """enable_crypto=True, enable_hip3=False → only native HL markets scanned."""
    seen = _scan_with_config(monkeypatch, {"enable_crypto": True, "enable_hip3": False})
    assert "BTC" in seen and "ETH" in seen
    assert not any(c.startswith("xyz:") for c in seen), seen


def test_scan_hip3_only_skips_crypto(monkeypatch):
    """enable_crypto=False, enable_hip3=True → only HIP-3 markets scanned."""
    seen = _scan_with_config(monkeypatch, {"enable_crypto": False, "enable_hip3": True})
    assert set(seen) == {"xyz:MU", "xyz:CRCL"}, seen


def test_scan_both_disabled_returns_empty(monkeypatch):
    """Both flags off → no-op scan, no candles fetched."""
    seen = _scan_with_config(monkeypatch, {"enable_crypto": False, "enable_hip3": False})
    assert seen == []


def test_scan_default_config_runs_crypto_only(monkeypatch):
    """Missing/empty config defaults to crypto enabled, HIP-3 disabled —
    backwards-compatible with deployments predating the toggle."""
    seen = _scan_with_config(monkeypatch, {})
    assert "BTC" in seen
    assert not any(c.startswith("xyz:") for c in seen)


def test_executor_blocks_hip3_when_disabled(monkeypatch):
    """A stale HIP-3 analysis must not execute when enable_hip3 is False."""
    from hermes_trader.agents import executor
    monkeypatch.setattr(executor, "read_agent_config",
                        lambda: {"mode": "LIVE", "enable_crypto": True, "enable_hip3": False})
    res = executor.maybe_execute({"id": "a1", "coin": "xyz:MU"})
    assert res["executed"] is False
    assert "hip3_disabled" in res["reason"]


def test_executor_blocks_crypto_when_disabled(monkeypatch):
    """A stale crypto analysis must not execute when enable_crypto is False."""
    from hermes_trader.agents import executor
    monkeypatch.setattr(executor, "read_agent_config",
                        lambda: {"mode": "LIVE", "enable_crypto": False, "enable_hip3": True})
    res = executor.maybe_execute({"id": "a2", "coin": "BTC"})
    assert res["executed"] is False
    assert "crypto_disabled" in res["reason"]


def test_scan_picks_low_volume_big_movers(monkeypatch):
    """The movers sub-bucket fetches candles for high-%-move markets that
    don't crack the volume cut — fixing the gap where IO +17%, HMSTR +9.6%,
    DYDX +8.5% were going unscanned because BTC/ETH/SOL dominated the top.

    Setup: 5 quiet high-volume coins (the volume budget happily takes them)
    + 5 low-volume coins with big swings (must end up in the movers slot).
    """
    from hermes_trader.agents import perception

    universe = (
        # 5 quiet crypto majors, sorted by volume
        [{"coin": f"MAJOR{i}", "type": "perp", "dex": None,
          "dayNtlVlm": 1e9 - i, "prevDayPx": 100.0, "midPx": 100.1}  # +0.1% — quiet
         for i in range(5)]
        # 5 low-volume big movers; volume ABOVE the floor so they're eligible
        + [{"coin": f"MOVER{i}", "type": "perp", "dex": None,
            "dayNtlVlm": 2_000_000 - i*1000, "prevDayPx": 100.0, "midPx": 100.0 + (10 + i)}
           for i in range(5)]
        # 1 micro-cap with insane move BUT below the floor — must be excluded
        + [{"coin": "PICO", "type": "perp", "dex": None,
            "dayNtlVlm": 50_000, "prevDayPx": 1.0, "midPx": 1.5}]  # +50% but $50k vol
    )
    # mids = the LIVE current price (production reads the 24h move from fresh
    # mids, not the cached universe midPx) — mirror each coin's midPx here.
    mids = {m["coin"]: str(m.get("midPx", 100)) for m in universe}

    monkeypatch.setenv("HERMES_MAX_MARKETS", "10")
    monkeypatch.setenv("HERMES_MAX_MARKETS_MOVERS", "3")
    monkeypatch.setenv("HERMES_MOVERS_VOL_FLOOR_USD", "1000000")
    monkeypatch.setenv("HERMES_UNIVERSE_SWEEP", "0")
    monkeypatch.setattr(perception, "fetch_all_mids", lambda include_hip3=False: mids)
    monkeypatch.setattr(perception, "get_universe", lambda include_hip3=False: universe)
    monkeypatch.setattr("hermes_trader.agents.config_store.read_agent_config",
                        lambda: {"enable_crypto": True, "enable_hip3": False})

    seen = []
    monkeypatch.setattr(perception, "_scan_single_market",
                        lambda m, mid, c, ms, ws=None, wsb=False, tse=True: (seen.append(m["coin"]), (True, None))[1])
    perception.scan_once(min_score=0)

    # Volume budget = 10 - 3 = 7 → all 5 MAJORs + 2 of the 5 MOVERs by volume
    # Then movers slot adds top-3 by |24h%| among the remaining MOVERs.
    assert any(c == "MAJOR0" for c in seen), seen
    movers_picked = [c for c in seen if c.startswith("MOVER")]
    # At least 3 movers should be picked total (some via volume, top remainder via momentum)
    assert len(movers_picked) >= 3, f"expected >=3 movers, got {movers_picked}"
    # Pico-cap below the volume floor must NEVER be scanned (noise filter)
    assert "PICO" not in seen, f"pico-cap leaked through floor: {seen}"
