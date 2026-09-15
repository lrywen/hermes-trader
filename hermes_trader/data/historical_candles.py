"""Historical candle data layer for point-in-time offline replay.

The live ``hl_client.fetch_hl_candles`` only serves the *most recent* N bars
with a 90s in-process TTL cache — useless for batch historical backfill, where
each signal needs the range pinned to its own timestamp. This layer provides:

* :func:`fetch_candle_range` — arbitrary ``[start_ms, end_ms]`` candleSnapshot
  ranges via the same authenticated ``_http_post`` transport as live;
* interval-aligned, strictly-monotonic parsing with non-finite/out-of-order
  bars dropped (same hygiene as the live fetch path);
* an append-only bar-level disk cache keyed by ``(coin, interval, t)``, so
  repeated replays / multiple arms share one immutable-history store and
  never re-request a closed bar (audited 2026-09-14: pinned historical ranges
  and recent-window fetches return byte-identical OHLC for closed bars);
* :func:`closed_bars_as_of` — the point-in-time view: only bars that had
  *closed* at ``as_of_ms`` are ever returned, which is the anti-look-ahead
  contract every backfill consumer must rely on.

This module never places orders and performs no live decisioning. Cache writes
are best-effort: a cache failure degrades to a direct fetch, never a raise.
"""
from __future__ import annotations

import json
import math
import os
import threading
import time
from pathlib import Path
from typing import Any, Optional

from hermes_trader.client.hl_client import _http_post
from hermes_trader.models.types import Candle

INTERVAL_MS = {"1m": 60_000, "5m": 300_000, "15m": 900_000,
               "1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000}

DEFAULT_CACHE_FILE = os.environ.get(
    "HERMES_HIST_CANDLE_CACHE", "/data/.historical-candles.json")

# Per-process bar cache: (coin, interval) -> {t_ms: Candle}
_BAR_CACHE: dict[tuple[str, str], dict[int, Candle]] = {}
_CACHE_LOCK = threading.RLock()
_DISK_CACHE_FILE: Optional[str] = None
_DISK_LOADED = False
_DISK_DIRTY = False

# In-process de-dup of in-flight disk persistence; the on-disk JSON shape is
# {"coin|interval": {"<t_ms>": [o,h,l,c,v]}, ...}.


def _interval_ms(interval: str) -> int:
    if interval not in INTERVAL_MS:
        raise ValueError(f"unsupported interval: {interval!r}")
    return INTERVAL_MS[interval]


def _disk_key(coin: str, interval: str) -> str:
    return f"{coin}|{interval}"


def _load_disk_cache() -> None:
    """Best-effort one-shot load of the bar cache from disk."""
    global _DISK_LOADED, _DISK_CACHE_FILE
    with _CACHE_LOCK:
        if _DISK_LOADED:
            return
        _DISK_LOADED = True
        path = _DISK_CACHE_FILE or DEFAULT_CACHE_FILE
        if not path or not os.path.exists(path):
            return
        try:
            with open(path, encoding="utf-8") as f:
                raw = json.load(f)
        except (OSError, json.JSONDecodeError):
            return
        if not isinstance(raw, dict):
            return
        for key, bars in raw.items():
            if "|" not in key or not isinstance(bars, dict):
                continue
            coin, interval = key.split("|", 1)
            if interval not in INTERVAL_MS:
                continue
            bucket = _BAR_CACHE.setdefault((coin, interval), {})
            for t_s, row in bars.items():
                try:
                    t = int(t_s)
                    o, h, l, c, v = (float(row[0]), float(row[1]),
                                     float(row[2]), float(row[3]),
                                     float(row[4]))
                except (TypeError, ValueError, IndexError):
                    continue
                if all(math.isfinite(x) for x in (o, h, l, c, v)):
                    bucket[t] = Candle(t=t, o=o, h=h, l=l, c=c, v=v)


def flush_disk_cache(path: Optional[str] = None) -> bool:
    """Persist newly cached bars atomically. Returns True on success.

    The whole bar map is rewritten tmp+os.replace (regenerable cache, no
    fsync). Safe to call repeatedly; a no-op when nothing changed.
    """
    global _DISK_DIRTY
    with _CACHE_LOCK:
        target = path or _DISK_CACHE_FILE or DEFAULT_CACHE_FILE
        if not target:
            return False
        if not _DISK_DIRTY:
            return True
        payload: dict[str, dict[str, list[float]]] = {}
        for (coin, interval), bars in _BAR_CACHE.items():
            payload[_disk_key(coin, interval)] = {
                str(t): [cdl.o, cdl.h, cdl.l, cdl.c, cdl.v]
                for t, cdl in sorted(bars.items())
            }
        try:
            os.makedirs(os.path.dirname(os.path.abspath(target)) or ".",
                        exist_ok=True)
            tmp = f"{target}.tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f, separators=(",", ":"))
            os.replace(tmp, target)
            _DISK_DIRTY = False
            return True
        except OSError:
            return False


def reset_cache() -> None:
    """Drop all cached bars (tests only)."""
    global _DISK_LOADED, _DISK_DIRTY, _DISK_CACHE_FILE
    with _CACHE_LOCK:
        _BAR_CACHE.clear()
        _DISK_LOADED = False
        _DISK_DIRTY = False
        _DISK_CACHE_FILE = None


def set_cache_file(path: Optional[str]) -> None:
    """Pin a different disk cache location (tests / alternate deployments)."""
    global _DISK_CACHE_FILE, _DISK_LOADED
    with _CACHE_LOCK:
        _DISK_CACHE_FILE = path
        _DISK_LOADED = False


def _parse_rows(raw: Any) -> list[Candle]:
    """Parse HL candleSnapshot rows with the live fetch path's hygiene:
    non-finite and duplicate/out-of-order bars are dropped, ascending output."""
    if not isinstance(raw, list):
        return []
    out: list[Candle] = []
    prev_t: Optional[int] = None
    for row in raw:
        try:
            t = int(row["t"])
            o, h, l, c = (float(row["o"]), float(row["h"]),
                          float(row["l"]), float(row["c"]))
            v = float(row.get("v", 0.0) or 0.0)
        except (TypeError, ValueError, KeyError):
            continue
        if not all(math.isfinite(x) for x in (o, h, l, c, v)):
            continue
        if prev_t is not None and t <= prev_t:
            continue
        prev_t = t
        out.append(Candle(t=t, o=o, h=h, l=l, c=c, v=v))
    return out


def _request_range(coin: str, interval: str,
                   start_ms: int, end_ms: int) -> list[Candle]:
    step = _interval_ms(interval)
    # Small back/forward pad so callers get the boundary bar either way; the
    # bar-open grid is what actually decides membership.
    payload = {"type": "candleSnapshot", "req": {
        "coin": coin, "interval": interval,
        "startTime": int(start_ms), "endTime": int(end_ms) + step - 1}}
    raw = _http_post("/info", payload)
    return _parse_rows(raw)


def fetch_candle_range(coin: str, interval: str,
                       start_ms: int, end_ms: int, *,
                       use_cache: bool = True) -> list[Candle]:
    """Fetch closed 1h/4h/... bars with bar-open ``t`` in ``[start, end]``.

    Bars already present in the bar cache are not re-requested; only missing
    sub-ranges hit the API. Result is the merged, ascending, de-duped view.
    Raises ``ValueError`` on a bad interval; network errors propagate (callers
    doing batch replays decide their own retry/skip policy).
    """
    step = _interval_ms(interval)
    if end_ms < start_ms:
        return []
    # Guard against pathological spans (e.g. a far-future as_of) that would
    # otherwise spin a huge grid under the cache lock. The outcome windows we
    # replay never exceed ~10k bars per request.
    max_bars = 20_000
    if (end_ms - (start_ms - start_ms % step)) // step > max_bars:
        raise ValueError(
            f"refusing single fetch over {(end_ms-start_ms)//step} bars "
            f"(>{max_bars}); chunk the replay or shrink as_of")
    global _DISK_DIRTY
    if use_cache:
        _load_disk_cache()
    key = (coin, interval)
    with _CACHE_LOCK:
        cached = _BAR_CACHE.setdefault(key, {}) if use_cache else {}

        wanted: list[int] = []
        t = start_ms - (start_ms % step)
        while t <= end_ms:
            wanted.append(t)
            t += step
        missing = [tt for tt in wanted if tt not in cached]

        if missing:
            # Request the full span covering the missing grid points in one
            # call; HL returns the forming bar too (open t == current grid),
            # which callers filter via closed_bars_as_of.
            fetched = _request_range(coin, interval, missing[0],
                                     missing[-1] + step)
            if use_cache:
                added = 0
                for cdl in fetched:
                    if cdl.t not in cached:
                        cached[cdl.t] = cdl
                        added += 1
                if added:
                    _DISK_DIRTY = True

        bars = [cached[tt] for tt in wanted
                if tt in cached and cached[tt] is not None]
        return bars


def closed_bars_as_of(coin: str, interval: str,
                      start_ms: int, as_of_ms: int, *,
                      use_cache: bool = True) -> list[Candle]:
    """Point-in-time window: bars that had CLOSED at or before ``as_of_ms``.

    A bar opening at ``t`` closes at ``t + interval``; the still-forming bar
    (``t + interval > as_of_ms``) is excluded so offline replay can never see
    a future price. ``start_ms`` is the inclusive lower bound on bar-open time.
    """
    step = _interval_ms(interval)
    end_grid = as_of_ms - step  # open-time of the newest bar closed by as_of
    if end_grid < start_ms:
        return []
    bars = fetch_candle_range(coin, interval, start_ms, end_grid,
                              use_cache=use_cache)
    return [b for b in bars if b.t + step <= as_of_ms]


def cached_span(coin: str, interval: str) -> Optional[tuple[int, int]]:
    """Return ``(min_t, max_t)`` currently cached for a series, if any."""
    with _CACHE_LOCK:
        bucket = _BAR_CACHE.get((coin, interval))
        if not bucket:
            return None
        ts = sorted(bucket)
        return ts[0], ts[-1]


def warm_cache_from_records(records: list[dict[str, Any]], *,
                            interval: str = "1h",
                            before_bars: int = 0,
                            after_bars_max: int = 168 + 8,
                            sleep_s: float = 0.0,
                            progress=None) -> dict[str, Any]:
    """Prefetch every bar span a batch of point-in-time records will need.

    Each record is ``{"coin": str, "timestamp"|"ts": ms}``. Spans are merged
    per coin into disjoint ranges to minimise requests. ``before_bars`` covers
    indicator warm-up; ``after_bars_max`` the forward outcome window. Returns
    a small stats dict. ``progress(coin, n_ranges)`` is an optional callback.
    """
    step = _interval_ms(interval)
    # coin -> list of (span_start, span_end) in ms
    spans: dict[str, list[tuple[int, int]]] = {}
    used = 0
    skipped = 0
    for r in records:
        coin = r.get("coin")
        t0 = r.get("timestamp", r.get("ts"))
        if not coin or not isinstance(t0, (int, float)):
            skipped += 1
            continue
        t0 = int(t0)
        if t0 < 10_000_000_000:  # seconds -> ms
            t0 *= 1000
        grid = t0 - (t0 % step)
        spans.setdefault(str(coin), []).append((
            grid - before_bars * step,
            grid + after_bars_max * step))
        used += 1

    n_requests = 0
    for coin, raw_spans in spans.items():
        merged = _merge_spans(sorted(raw_spans))
        for s0, s1 in merged:
            fetch_candle_range(coin, interval, s0, s1)
            n_requests += 1
            if sleep_s:
                time.sleep(sleep_s)
        if progress:
            progress(coin, len(merged))
    return {"records_used": used, "records_skipped": skipped,
            "coins": len(spans), "range_requests": n_requests}


def _merge_spans(spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
    out: list[list[int]] = []
    for s0, s1 in spans:
        if out and s0 <= out[-1][1]:
            out[-1][1] = max(out[-1][1], s1)
        else:
            out.append([s0, s1])
    return [(a, b) for a, b in out]
