"""P0-1: backfill the production 1h/4h candle cache up to today.

The production cache (/data/.historical-candles.json) stopped at 2026-09-15,
~46 days stale, which blocks every gate reconciliation that needs recent 1h
forward windows. This script pages Hyperliquid candleSnapshot (the same source
historical_candles.py uses) for the current scan universe and merges into the
production cache. Read-only against the exchange; writes only the cache file.
Does NOT touch ch_hist_candles.json (the research snapshot must stay frozen
for reproducibility).

Usage (inside the container):
    python3 scripts/backfill_hist_candles.py --days 90 --intervals 1h 4h
    python3 scripts/backfill_hist_candles.py --days 400 --intervals 1d
    python3 scripts/backfill_hist_candles.py --verify-only
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import time

from hermes_trader.data.historical_candles import (
    INTERVAL_MS,
    _load_disk_cache,
    cached_span,
    fetch_candle_range,
    flush_disk_cache,
    set_cache_file,
)

PROD_CACHE = "/data/.historical-candles.json"


def _universe() -> list[str]:
    """Current scan universe (top-N by 24h volume), same source perception uses."""
    from hermes_trader.client.universe import get_universe
    markets = get_universe()
    perps = [m for m in markets if m.get("type") == "perp"
             and not str(m.get("coin", "")).startswith("@")]
    perps.sort(key=lambda m: m.get("dayNtlVlm", 0) or 0, reverse=True)
    return [m["coin"] for m in perps[:44]]


def backfill(coins: list[str], intervals: list[str], days: int) -> dict:
    now_ms = int(time.time() * 1000)
    start_ms = now_ms - days * 86_400_000
    set_cache_file(PROD_CACHE)
    # set_cache_file only pins the path and clears _DISK_LOADED; it does NOT
    # load. cached_span reads the in-memory _BAR_CACHE, so without an explicit
    # load the before_newest span is always None. fetch_candle_range would load
    # lazily, but we need the span BEFORE the first fetch.
    _load_disk_cache()
    stats = {}
    for iv in intervals:
        # Align end to the last fully-closed bar *of this interval* so a forming
        # candle (e.g. today's 1d) never enters the immutable-history cache.
        step = INTERVAL_MS.get(iv, 3_600_000)
        end_ms = now_ms - (now_ms % step) - 1
        for coin in coins:
            span = cached_span(coin, iv)
            before = span[1] if span else None
            try:
                bars = fetch_candle_range(coin, iv, start_ms, end_ms)
                span2 = cached_span(coin, iv)
                newest = span2[1] if span2 else None
                stats[f"{coin}|{iv}"] = {
                    "bars": len(bars),
                    "before_newest": before,
                    "after_newest": newest,
                }
                print(f"  {coin:8} {iv}: {len(bars):5} bars, "
                      f"newest {_fmt(newest)}", flush=True)
            except Exception as e:  # network / interval unsupported on HL
                stats[f"{coin}|{iv}"] = {"error": str(e)[:120]}
                print(f"  {coin:8} {iv}: ERROR {str(e)[:80]}", flush=True)
            time.sleep(0.15)  # be gentle on HL weight limits
    ok = flush_disk_cache(PROD_CACHE)
    print(f"\nflush_disk_cache -> {ok}")
    return stats


def _fmt(ms):
    if not ms:
        return "none"
    return dt.datetime.utcfromtimestamp(ms / 1000).strftime("%m-%d %H:%M")


def verify(coins: list[str], intervals: list[str]) -> dict:
    """Report per-interval coverage: series count, newest bar, staleness.

    Staleness is interval-aware: a series is stale once its newest bar is more
    than two *bars* old (a 1d series is not stale after 2 hours).
    """
    set_cache_file(PROD_CACHE)
    # Must load explicitly: set_cache_file does not read the file, and
    # cached_span only inspects in-memory _BAR_CACHE. Without this load
    # --verify-only always reports 0 series despite a healthy on-disk cache.
    _load_disk_cache()
    now_ms = int(time.time() * 1000)
    out = {}
    for iv in intervals:
        newest_all = 0
        oldest_all = None
        n = 0
        stale = []
        max_age_ms = 2 * INTERVAL_MS.get(iv, 3_600_000)
        for coin in coins:
            span = cached_span(coin, iv)
            if not span:
                continue
            n += 1
            newest_all = max(newest_all, span[1])
            oldest_all = span[0] if oldest_all is None else min(oldest_all, span[0])
            if now_ms - span[1] > max_age_ms:
                stale.append(coin)
        out[iv] = {
            "series": n,
            "newest": _fmt(newest_all),
            "oldest": _fmt(oldest_all),
            "stale_coins": stale,
        }
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=90)
    ap.add_argument("--intervals", nargs="+", default=["1h", "4h", "1d"])
    ap.add_argument("--coins", nargs="*", default=None)
    ap.add_argument("--verify-only", action="store_true")
    args = ap.parse_args()
    coins = args.coins or _universe()
    print(f"universe: {len(coins)} coins -> {coins[:6]}...")
    if args.verify_only:
        print(json.dumps(verify(coins, args.intervals), indent=2))
        return
    backfill(coins, args.intervals, args.days)
    print("\n=== post-backfill coverage ===")
    print(json.dumps(verify(coins, args.intervals), indent=2))


if __name__ == "__main__":
    sys.exit(main())
