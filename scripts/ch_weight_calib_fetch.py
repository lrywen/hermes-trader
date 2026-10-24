#!/usr/bin/env python3
"""Fetch 5m/15m/1h candles for the weight-recalibration counterfactual.

Offline, PIT. Pulls the union of (a) top-N coins by real taker fills and
(b) coins seen in the recent runner_gate_shadow rejections, over a trailing
window. Uses the immutable-history bar cache (HERMES_HIST_CANDLE_CACHE) so
re-runs never re-request a closed bar. Network only on cache miss.

Env:
  CH_UNIVERSE   newline coin list (default: built from shadow book + gate)
  CH_DAYS       trailing days (default 32)
  CH_OUT        candle cache target (default /tmp/v2/ch_wt_candles.json; the
                authoritative bar store is HERMES_HIST_CANDLE_CACHE)
"""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO)

from hermes_trader.data import historical_candles as hc  # noqa: E402

DAYS = float(os.environ.get("CH_DAYS", "32"))
INTERVALS = ("5m", "15m", "1h")
# Extra left pad so 1h trend_strength (100-bar warmup in prod) is well seeded.
PAD_MS = 120 * 3_600_000


def _universe() -> list[str]:
    env = os.environ.get("CH_UNIVERSE")
    if env:
        return [c.strip() for c in open(env) if c.strip()]
    coins: list[str] = []
    seen = set()
    book = json.load(open(os.environ.get("CH_BOOK", "/tmp/v2/shadow_book_1024.json")))
    opens = [f for f in book["accounts"]["taker"]["fills"] if f.get("type") == "open"]
    import collections
    cnt = collections.Counter(
        f["coin"].replace(":PERP", "").split(":")[0] for f in opens)
    for coin, _ in cnt.most_common(40):
        if coin not in seen:
            seen.add(coin); coins.append(coin)
    for coin in ("REZ", "ZORA", "GRAM", "INIT"):
        if coin not in seen:
            seen.add(coin); coins.append(coin)
    return coins


def main() -> int:
    cache = os.environ.get("HERMES_HIST_CANDLE_CACHE", "/tmp/v2/ch_hist_candles.json")
    hc.set_cache_file(cache)
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    start_ms = now_ms - int(DAYS * 86_400_000) - PAD_MS
    coins = _universe()
    print(f"universe={len(coins)} coins  days={DAYS}  cache={cache}", flush=True)
    summary = {}
    for i, coin in enumerate(coins):
        row = {}
        for iv in INTERVALS:
            try:
                bars = hc.fetch_candle_range(coin, iv, start_ms, now_ms)
                row[iv] = len(bars)
            except Exception as e:  # network/parse: record and continue
                row[iv] = f"ERR {type(e).__name__}: {str(e)[:50]}"
                time.sleep(0.5)
            time.sleep(float(os.environ.get("CH_FETCH_SLEEP", "0.25")))
        summary[coin] = row
        hc.flush_disk_cache()
        print(f"[{i+1:2}/{len(coins)}] {coin:10} {row}", flush=True)
    hc.flush_disk_cache()
    json.dump(summary, open(os.environ.get("CH_OUT", "/tmp/v2/ch_wt_summary.json"), "w"), indent=1)
    print("done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
