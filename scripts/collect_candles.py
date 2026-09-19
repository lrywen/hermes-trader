#!/usr/bin/env python3
"""Prefetch historical Hyperliquid candles into the shared bar-level cache.

Thin CLI over the P4 historical data layer (``hermes_trader.data.
historical_candles``). It does NOT implement fetching itself — it only plans
the (coin, interval) span matrix and drives :func:`fetch_candle_range` in
20k-bar-safe chunks, so every bar lands in the append-only, point-in-time
disk cache that the backtest kernel / replays already read.

Why this exists:
  * ``backtest.py`` fetches only the *most recent* N bars per coin (live
    ``fetch_hl_candles`` path), so a long multi-interval backtest thrashes the
    API on first use and is hard to re-run offline;
  * the various ``backfill_*`` / ``bt_*`` scripts each couple their own
    chunked pull to one research arm. This is the one generic, re-runnable
    collector: run it once to warm the immutable-history store, then any
    backtest/replay reads closed bars with zero network calls.

Safety contract:
  * sets HERMES_BACKTEST=1 before importing client modules — never loads a
    live mainnet key, never places an order;
  * writes ONLY the regenerable candle cache (default
    /data/.historical-candles.json, tmp+os.replace atomically);
  * re-runnable: the bar cache is keyed by (coin, interval, t), so already
    cached closed bars are never re-requested (incremental backfill);
  * --dry-run prints the fetch plan without any network/disk writes.

Usage:
    python3 scripts/collect_candles.py --days 90 --interval 5m 1h 4h --coins 20
    python3 scripts/collect_candles.py --coin BTC ETH --interval 1h --days 30
    python3 scripts/collect_candles.py --days 30 --dry-run
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

# P3-17: backtest process — never load a live mainnet private key.
os.environ["HERMES_BACKTEST"] = "1"

_REPO = Path(__file__).resolve().parents[1]
_env = _REPO / ".env.local"
if _env.is_file():
    for _line in _env.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _, _v = _line.partition("=")
            if _k.strip() == "HYPERLIQUID_PRIVATE_KEY":
                continue
            os.environ.setdefault(_k.strip(), _v.strip())
sys.path.insert(0, str(_REPO))

from hermes_trader.client.universe import get_universe
from hermes_trader.data import historical_candles as hc

# Stay strictly under fetch_candle_range's 20k-bar single-request guard.
_CHUNK_BARS = 19_000


def select_coins(coins: int, *, explicit: Optional[List[str]] = None,
                 exclude_hip3: bool = True) -> List[str]:
    """Resolve the target coin list: explicit symbols win, else top-N perps
    by 24h notional volume (same ranking backtest.py uses)."""
    if explicit:
        # De-dup preserving order; strip whitespace.
        out: List[str] = []
        for c in explicit:
            c = c.strip()
            if c and c not in out:
                out.append(c)
        return out
    universe = get_universe()
    perps = [m for m in universe if m.get("type") == "perp"
             and not str(m.get("coin", "")).startswith("@")]
    if exclude_hip3:
        perps = [m for m in perps if ":" not in str(m.get("coin", ""))]
    ranked = sorted(perps, key=lambda m: m.get("dayNtlVlm", 0) or 0, reverse=True)
    return [str(m["coin"]) for m in ranked[:coins]]


def plan_spans(start_ms: int, end_ms: int, interval: str) -> List[tuple[int, int]]:
    """Split [start_ms, end_ms] into <=_CHUNK_BARS grid-aligned fetch spans."""
    step = hc.INTERVAL_MS[interval]
    g0 = start_ms - (start_ms % step)
    g1 = end_ms - (end_ms % step)
    chunk_ms = _CHUNK_BARS * step
    spans: List[tuple[int, int]] = []
    t = g0
    while t <= g1:
        spans.append((t, min(t + chunk_ms - step, g1)))
        t += chunk_ms
    return spans


def collect(coins: List[str], intervals: List[str], start_ms: int, end_ms: int,
            *, sleep_s: float = 0.0, use_cache: bool = True,
            on_progress=None) -> Dict[str, Any]:
    """Fetch every (coin, interval) span through the kernel cache.

    Returns a stats dict. A per-span network failure is counted, not raised —
    one bad series must not abort a long multi-coin warm-up.
    """
    stats: Dict[str, Any] = {
        "coins": len(coins), "intervals": list(intervals),
        "spans_planned": 0, "spans_ok": 0, "span_errors": 0,
        "bars_before": 0, "bars_after": 0,
        "errors": [],
    }
    for coin in coins:
        for interval in intervals:
            before = _series_len(coin, interval)
            for s0, s1 in plan_spans(start_ms, end_ms, interval):
                stats["spans_planned"] += 1
                try:
                    hc.fetch_candle_range(coin, interval, s0, s1,
                                          use_cache=use_cache)
                    stats["spans_ok"] += 1
                except Exception as e:  # best-effort batch: count, keep going
                    stats["span_errors"] += 1
                    stats["errors"].append(f"{coin} {interval} {s0}-{s1}: {e!r}")
                if sleep_s:
                    time.sleep(sleep_s)
            after = _series_len(coin, interval)
            stats["bars_before"] += before
            stats["bars_after"] += after
            if on_progress:
                on_progress(coin, interval, after - before, after)
    stats["bars_added"] = stats["bars_after"] - stats["bars_before"]
    return stats


def _series_len(coin: str, interval: str) -> int:
    span = hc.cached_span(coin, interval)
    if span is None:
        return 0
    step = hc.INTERVAL_MS[interval]
    return (span[1] - span[0]) // step + 1


def _parse_end_ms(end: Optional[str]) -> int:
    if not end or end == "now":
        return int(time.time() * 1000)
    # ISO yyyy-mm-dd (UTC) is the friendly form; epoch ms also accepted.
    txt = end.strip()
    if txt.isdigit():
        v = int(txt)
        return v if v > 10_000_000_000 else v * 1000
    dt = datetime.fromisoformat(txt.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--days", type=float, default=30,
                    help="history length to prefetch ending at --end (default 30)")
    ap.add_argument("--end", default="now",
                    help="range end: 'now' (default), epoch ms, or ISO yyyy-mm-dd (UTC)")
    ap.add_argument("--interval", nargs="+", default=["1h"],
                    choices=sorted(hc.INTERVAL_MS.keys()),
                    help="one or more candle intervals (default 1h)")
    ap.add_argument("--coins", type=int, default=20,
                    help="top-N perps by 24h volume when --coin is omitted (default 20)")
    ap.add_argument("--coin", nargs="+", default=None,
                    help="explicit coin symbols (overrides --coins)")
    ap.add_argument("--include-hip3", action="store_true",
                    help="keep colon-namespaced HIP-3 markets in the top-N universe")
    ap.add_argument("--cache-file", default=None,
                    help="bar cache path (default: module default / "
                         "HERMES_HIST_CANDLE_CACHE)")
    ap.add_argument("--sleep", type=float, default=0.0,
                    help="seconds to sleep after every span request (rate courtesy)")
    ap.add_argument("--no-cache", action="store_true",
                    help="bypass the disk cache (network fetch; nothing persisted)")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the fetch plan (coins/spans/bars) without network/disk")
    args = ap.parse_args()

    end_ms = _parse_end_ms(args.end)
    start_ms = end_ms - int(args.days * 86_400_000)
    coins = select_coins(args.coins, explicit=args.coin,
                         exclude_hip3=not args.include_hip3)
    if not coins:
        print("no coins resolved (empty universe / bad --coin list)")
        return 2

    if args.cache_file:
        hc.set_cache_file(args.cache_file)
    cache_target = args.cache_file or hc._DISK_CACHE_FILE or hc.DEFAULT_CACHE_FILE
    use_cache = not args.no_cache

    if args.dry_run:
        print("=== collect_candles DRY RUN (no network / no writes) ===")
        print(f"range     : {datetime.fromtimestamp(start_ms/1000, timezone.utc):%Y-%m-%d %H:%M} "
              f"-> {datetime.fromtimestamp(end_ms/1000, timezone.utc):%Y-%m-%d %H:%M} UTC")
        print(f"coins({len(coins)}): {', '.join(coins)}")
        print(f"cache     : {cache_target if use_cache else '(disabled)'}")
        total_bars = 0
        total_spans = 0
        for interval in args.interval:
            step = hc.INTERVAL_MS[interval]
            bars = (end_ms - (start_ms - start_ms % step)) // step + 1
            spans = plan_spans(start_ms, end_ms, interval)
            total_bars += bars * len(coins)
            total_spans += len(spans) * len(coins)
            print(f"  {interval:>3}: ~{bars:>6} bars/coin x {len(coins)} coins "
                  f"in {len(spans)} chunk(s)/coin")
        print(f"total     : ~{total_bars} bars, {total_spans} span requests "
              f"(cached bars are skipped on a real run)")
        return 0

    print(f"collecting {len(coins)} coins x {args.interval} over {args.days:g}d "
          f"-> {cache_target}")

    def _progress(coin: str, interval: str, added: int, total: int) -> None:
        print(f"  {coin:<12} {interval:>3}: +{added:>6} bars (cached {total:>7})")

    stats = collect(coins, list(args.interval), start_ms, end_ms,
                    sleep_s=max(0.0, args.sleep), use_cache=use_cache,
                    on_progress=_progress)

    flushed = hc.flush_disk_cache() if use_cache else False
    print("=" * 60)
    print(f"spans       : {stats['spans_ok']}/{stats['spans_planned']} ok"
          f"{', ' + str(stats['span_errors']) + ' errors' if stats['span_errors'] else ''}")
    print(f"bars cached : {stats['bars_before']} -> {stats['bars_after']} "
          f"(+{stats['bars_added']})")
    print(f"disk flush  : {'ok' if flushed else 'not persisted'} -> {cache_target if use_cache else '(disabled)'}")
    if stats["errors"]:
        print("first errors:")
        for e in stats["errors"][:5]:
            print(f"  {e}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
