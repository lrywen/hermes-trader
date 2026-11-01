"""Regression: backfill_hist_candles.verify() must actually load the disk cache.

Root cause (found 2026-11-01): set_cache_file() only pins the path and clears
_DISK_LOADED; it does NOT read the file. cached_span() inspects only the
in-memory _BAR_CACHE, so verify() — which never calls fetch_candle_range (the
lazy-load trigger) — always reported 0 series despite a healthy on-disk cache.
That made `--verify-only` an ops trap (would falsely scream "1d data gone").

The fix is an explicit _load_disk_cache() after set_cache_file() in both
verify() and backfill() (the latter also made the before_newest log always
None). This test pins the verify side against a real on-disk cache file.
"""
from __future__ import annotations

import importlib.util
import json
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _load_backfill_module(cache_path: str):
    """Import scripts/backfill_hist_candles.py with PROD_CACHE redirected."""
    sys.path.insert(0, str(REPO))
    try:
        spec = importlib.util.spec_from_file_location(
            "_backfill_hist_candles_verify_test",
            REPO / "scripts" / "backfill_hist_candles.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    finally:
        sys.path.remove(str(REPO))
    mod.PROD_CACHE = cache_path
    return mod


def _write_cache(path: Path) -> None:
    """Write one BTC 1d series in the real on-disk layout:
    {coin|interval: {t_str: [o,h,l,c,v]}}."""
    day = 86_400_000
    t = int(time.time() * 1000)
    path.write_text(json.dumps({"BTC|1d": {
        str(t - 3 * day): [1, 1, 1, 1, 1],
        str(t - 2 * day): [1, 1, 1, 1, 1],
        str(t - day): [1, 1, 1, 1, 1],
    }}))


def test_verify_reports_series_from_disk_cache(tmp_path):
    cache = tmp_path / ".historical-candles.json"
    _write_cache(cache)
    mod = _load_backfill_module(str(cache))
    out = mod.verify(["BTC"], ["1d"])
    # Before the fix this was 0 (set_cache_file never loaded the file).
    assert out["1d"]["series"] == 1
    assert out["1d"]["stale_coins"] == []
