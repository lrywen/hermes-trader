#!/usr/bin/env python3
"""Extract point-in-time SURFACE-CHANNEL signals from session-log scan events.

The bf analysis bucketed by bullish_fired; this extractor answers the L1 audit
F2 question: the composite gate (54) admits <2% of surfaced coins and the real
selectors are the OR-bypasses. Do the two dominant bypass channels —
trend_bypass (uptrend/downtrendMomentum) and dailyMover — precede forward
returns different from the genuine >=gate setups?

For every surfaced coin in every scan event we emit one signal:
  {coin, ts(scan), composite, fired:[...], channel, composite_band,
   verdict, conf}
``channel`` is the MUTUALLY-EXCLUSIVE reason the coin would have surfaced,
matching the OR precedence in perception._scan_single_market
(burst -> trend -> reversal pattern -> dailyMover -> composite gate), so the
channel counts add up to the surfaced total and stay comparable to the audit's
65.5% trend / 29% mover split.

The AI verdict/confidence are joined from research events by the same
nearest-neighbour window the bf run used (research is asynchronous; this join
is advisory only, never used for the raw channel forward-return test).

Reads the concatenated session-log on stdin (or CH_SESSION env path glob),
writes JSON array to CH_SIGNALS (default /tmp/ch_signals.json). Network-free.
"""
from __future__ import annotations

import glob
import json
import os
import sys
import gzip
import bisect
import datetime as dt
from collections import defaultdict

# Mirrors perception._scan_single_market trigger membership.
_TREND = {"uptrendMomentum", "downtrendMomentum"}
_PATTERN = {"bullishReversalCandle", "bearishReversalCandle"}
_BURST = {"momentumBurst"}
# audit window default gate; classification of "true gate" uses the emitted
# composite vs this threshold (the deployed gate across the audit window).
GATE = float(os.environ.get("CH_GATE", "54"))

# research nearest-neighbour join window (ms), advisory only.
JOIN_BEFORE = int(os.environ.get("CH_JOIN_BEFORE_MS", str(15 * 60_000)))
JOIN_AFTER = int(os.environ.get("CH_JOIN_AFTER_MS", str(2 * 60_000)))


def classify(fired: set[str], composite: float) -> str:
    """Mutually-exclusive surface channel, same OR precedence as production."""
    if fired & _BURST:
        return "burst"
    if fired & _TREND:
        return "trend"
    if fired & _PATTERN:
        return "pattern"
    if "dailyMover" in fired:
        return "mover"
    if composite >= GATE:
        return "gate"
    return "other"


def composite_band(c: float) -> str:
    if c is None:
        return "na"
    if c >= GATE:
        return ">=54"
    if c >= 45:
        return "45-54"
    if c >= 35:
        return "35-45"
    if c >= 25:
        return "25-35"
    return "<25"


def _open_logs():
    spec = os.environ.get("CH_SESSION")
    files = sorted(glob.glob(spec)) if spec else []
    if not files:
        for line in sys.stdin:
            yield line
        return
    for fp in files:
        op = gzip.open if fp.endswith(".gz") else open
        with op(fp, "rt", encoding="utf-8", errors="replace") as f:
            for line in f:
                yield line


def main() -> int:
    # First pass: index research events per coin for the advisory verdict join.
    research = defaultdict(list)  # coin -> [(ts, verdict, conf)]
    raw = []
    for line in _open_logs():
        try:
            d = json.loads(line)
        except Exception:
            continue
        ev = d.get("event")
        if ev == "research":
            t = d.get("ts")
            if isinstance(t, (int, float)):
                research[d.get("coin")].append(
                    (t, d.get("verdict"), d.get("confidence")))
        elif ev == "scan":
            raw.append(d)
    for coin in research:
        research[coin].sort()

    def verdict_for(coin, ts):
        arr = research.get(coin)
        if not arr:
            return None, None
        ts_list = [a[0] for a in arr]
        i = bisect.bisect_left(ts_list, ts)
        best = None
        for j in (i - 1, i):
            if 0 <= j < len(arr):
                dt_ = arr[j][0] - ts
                if -JOIN_BEFORE <= dt_ <= JOIN_AFTER:
                    if best is None or abs(dt_) < abs(best[0] - ts):
                        best = arr[j]
        if best is None:
            return None, None
        return best[1], best[2]

    only_since = os.environ.get("CH_SINCE")
    cutoff = 0
    if only_since:
        cutoff = int(dt.datetime.strptime(only_since, "%Y-%m-%d")
                     .replace(tzinfo=dt.timezone.utc).timestamp() * 1000)

    signals = []
    for d in raw:
        ts = d.get("ts")
        if not isinstance(ts, (int, float)) or ts < cutoff:
            continue
        for cs in d.get("coin_scores", []) or []:
            fired = set(cs.get("triggers", []) or [])
            comp = float(cs.get("score", 0.0))
            v, conf = verdict_for(cs.get("coin"), ts)
            signals.append({
                "coin": cs.get("coin"),
                "ts": int(ts),
                "composite": round(comp, 2),
                "fired": sorted(fired),
                "channel": classify(fired, comp),
                "band": composite_band(comp),
                "verdict": v,
                "conf": conf,
            })

    # A coin surfaced in several overlapping scan cycles maps to the same 15m
    # bar and would otherwise be priced as independent signals (200k+ mostly
    # repeated coin-cycles). Dedup to one row per (coin, 15m bucket), keeping
    # the highest composite, so forward-return samples are distinct bar entries.
    if os.environ.get("CH_DEDUP", "1") == "1":
        best = {}
        for s in signals:
            key = (s["coin"], s["ts"] // (15 * 60_000))
            cur = best.get(key)
            if cur is None or s["composite"] > cur["composite"]:
                best[key] = s
        signals = list(best.values())


    out = os.environ.get("CH_SIGNALS", "/tmp/ch_signals.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(signals, f)
    # channel census to stderr/stdout for sanity
    from collections import Counter
    census = Counter(s["channel"] for s in signals)
    print(f"signals={len(signals)} coins={len({s['coin'] for s in signals})}")
    for k, v in census.most_common():
        print(f"  {k:8} {v:6} {100*v/max(1,len(signals)):5.1f}%")
    print("WROTE", out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
