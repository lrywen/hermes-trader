"""B-4: per-coin half-spread table covers the 81-coin backtest pool.

Locks the Hyperliquid l2Book top-of-book half-spread data file that backs the
research backtest's ``--slip-mode per-coin`` (default): it must cover the full
81-coin ``bt_ready_pool`` with sane positive values, and applying it must move
the pool mean cost materially away from the flat majors constant (the B-4
acceptance: per-coin vs flat difference should be significant — small caps have
much wider spreads than majors, not the 1.4% near-null seen at 8 coins).
"""
from __future__ import annotations

import json
import statistics
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]
_DATA = _REPO / "hermes_trader" / "data" / "per_coin_half_spread_bps.json"
_RECON = Path("/tmp/p6b1_recon.json")  # 81-coin pool manifest (dev host)


def test_data_file_structure():
    doc = json.loads(_DATA.read_text())
    assert "half_spread_bps" in doc and "_meta" in doc
    spreads = doc["half_spread_bps"]
    assert len(spreads) >= 81
    for coin, v in spreads.items():
        assert isinstance(coin, str) and coin == coin.upper()
        assert isinstance(v, (int, float)) and v > 0


def test_covers_eight_majors_with_stable_values():
    spreads = json.loads(_DATA.read_text())["half_spread_bps"]
    # Majors are tight markets; sanity-bound them.
    assert spreads["BTC"] < 0.5
    assert spreads["ETH"] < 0.5


@pytest.mark.skipif(not _RECON.is_file(),
                    reason="81-coin pool manifest only on the dev host")
def test_covers_full_bt_ready_pool():
    pool = json.loads(_RECON.read_text())["bt_ready_pool"]
    spreads = json.loads(_DATA.read_text())["half_spread_bps"]
    missing = sorted(set(pool) - set(spreads))
    assert not missing, f"spread table missing pool coins: {missing}"


@pytest.mark.skipif(not _RECON.is_file(),
                    reason="81-coin pool manifest only on the dev host")
def test_per_coin_mean_materially_differs_from_flat_majors():
    pool = json.loads(_RECON.read_text())["bt_ready_pool"]
    spreads = json.loads(_DATA.read_text())["half_spread_bps"]
    pool_vals = [spreads[c] for c in pool if c in spreads]
    mean81 = statistics.mean(pool_vals)
    # Small caps are ~several-fold wider than the majors flat constant 0.31;
    # the B-4 acceptance requires the modes to diverge by well over 1.4%.
    assert mean81 > 0.31 * 2.0
