"""P0-a (audit 2026-10-20): rotating universe sweep must be CARVED INSIDE the
max_markets cap, not appended on top of the bucket picks.

Previously ``markets = markets + added`` appended the sweep window with no
ceiling, so universe_sweep=8 produced a 68-market scan and exceeded HL's
~1200 weight/min budget. The sweep slots are now reserved out of the static
crypto-by-volume residual, and the total is hard-capped at max_markets.
"""
import importlib

import pytest


def _run_scan(monkeypatch, perception, *, max_markets, hip3, movers, sweep,
              n_crypto=120, n_hip3=40, reset_offset=True):
    # First 30 crypto = deep-volume static vol picks; the rest carry a 12% 24h
    # move so the movers slots can fill. HIP-3 names likewise get moves and
    # volume above the $1M HIP-3 movers floor so both HIP-3 halves can fill.
    universe = []
    for i in range(n_crypto):
        m = {"coin": f"C{i:03d}", "type": "perp", "dex": None,
             "dayNtlVlm": 5_000_000_000 - i * 1_000_000,
             "prevDayPx": 100.0}
        if i >= 30:
            m["midPx"] = 112.0  # +12% -> qualifies as a crypto mover
        universe.append(m)
    for i in range(n_hip3):
        universe.append({
            "coin": f"xyz:H{i:02d}", "type": "perp", "dex": "xyz",
            "dayNtlVlm": 80_000_000 - i * 100_000,
            "prevDayPx": 100.0, "midPx": 110.0})  # +10%, above HIP-3 movers floor
    mids = {m["coin"]: str(m.get("midPx", 100)) for m in universe}
    if reset_offset:
        perception._sweep_offset = 0
    monkeypatch.setenv("HERMES_MAX_MARKETS", str(max_markets))
    monkeypatch.setenv("HERMES_MAX_MARKETS_HIP3", str(hip3))
    monkeypatch.setenv("HERMES_MAX_MARKETS_MOVERS", str(movers))
    monkeypatch.setenv("HERMES_UNIVERSE_SWEEP", str(sweep))
    monkeypatch.setattr(perception, "fetch_all_mids",
                        lambda include_hip3=False: mids)
    monkeypatch.setattr(perception, "get_universe",
                        lambda include_hip3=False: universe)
    monkeypatch.setattr(
        "hermes_trader.agents.config_store.read_agent_config",
        lambda: {"enable_hip3": True,
                 "min_market_volume_usd": 0, "min_hip3_volume_usd": 0})
    seen = []

    def _capture(m, mid, cfg, ms, ws=None, wsb=False, tse=True):
        seen.append(m["coin"])
        return (True, None)

    monkeypatch.setattr(perception, "_scan_single_market", _capture)
    perception.scan_once(min_score=0)
    return seen


@pytest.fixture
def perception():
    mod = importlib.import_module("hermes_trader.agents.perception")
    return mod


def test_sweep_is_carved_inside_cap_not_appended(monkeypatch, perception):
    # 60 cap, 25 hip3, 10 movers, 8 sweep -> crypto-vol residual 17;
    # buckets 17+10+12+13 = 52, sweep fills the remaining 8 => exactly 60.
    seen = _run_scan(monkeypatch, perception,
                     max_markets=60, hip3=25, movers=10, sweep=8)
    assert len(seen) == 60, f"scan exceeded/fell short of cap: {len(seen)}"


def test_sweep_zero_is_backward_compatible_full_buckets(monkeypatch, perception):
    seen = _run_scan(monkeypatch, perception,
                     max_markets=60, hip3=25, movers=10, sweep=0)
    assert len(seen) == 60


def test_sweep_never_exceeds_cap_even_if_over_requested(monkeypatch, perception):
    # Requesting more sweep than headroom must still stay at the hard cap.
    seen = _run_scan(monkeypatch, perception,
                     max_markets=20, hip3=10, movers=0, sweep=50)
    assert len(seen) <= 20


def test_sweep_rotates_new_coins_across_cycles(monkeypatch, perception):
    # Small universe: static top-vol coins repeat, but rotating the offset
    # surfaces long-tail coins that the bucket picks never reach.
    run1 = _run_scan(monkeypatch, perception,
                     max_markets=12, hip3=2, movers=0, sweep=4,
                     n_crypto=40, reset_offset=True)
    cursor1 = perception._sweep_offset
    run2 = _run_scan(monkeypatch, perception,
                     max_markets=12, hip3=2, movers=0, sweep=4,
                     n_crypto=40, reset_offset=False)
    assert cursor1 > 0  # the persistent ring cursor advanced
    # the static top-volume crypto coins repeat every cycle; the rotating
    # cursor must surface additional distinct long-tail coins on cycle 2.
    assert len(set(run1) | set(run2)) > len(set(run1))
