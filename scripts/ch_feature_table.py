#!/usr/bin/env python3
"""Build a per-signal historical feature table for h2 (entry-path forward
returns by channel/band/regime) and h4 (MFE/MAE by regime).

For every LONG signal from the surviving ch_event_replay trades we attach,
using ONLY bars at/after the PIT entry (no lookahead):
  * fwd close returns at 4h/12h/24h/48h (16/48/96/192 15m bars)
  * bar-based favorable/adverse excursion over the first 24h window (96 bars):
    MFE = max(high/entry-1), MAE = min(low/entry-1)
  * regime = nearest historical `analysis` event for the same coin (within
    +-6h, ms created_at) from the trace event bus (events_all.jsonl)
  * channel / band / composite from the ch signal labels

Writes CH_FEATURES jsonl. Network-free. Env:
  CH_TRADES /tmp/v2/ch_trades.jsonl ; CH_DEEP /tmp/v2/ch_deep_candles.json
  CH_EVENTS /tmp/v2/events_all.jsonl ; CH_FEATURES /tmp/v2/ch_features.jsonl
"""
from __future__ import annotations

import bisect
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import importlib.util

_HERE = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location(
    "ch_event_replay", os.path.join(_HERE, "ch_event_replay.py"))
rp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rp)  # type: ignore

HORIZONS = {"4h": 16, "12h": 48, "24h": 96, "48h": 192}
EXC_BARS = 96
REGIME_TOL_MS = 6 * 3600_000


def load_regime_map(path):
    """coin -> sorted [(created_at_ms, regime)] from analysis trace events."""
    m = {}
    for line in open(path, encoding="utf-8"):
        try:
            d = json.loads(line)
        except Exception:
            continue
        if d.get("event") != "analysis":
            continue
        p = d.get("payload") or {}
        coin = p.get("coin")
        ts = p.get("created_at")
        reg = p.get("regime")
        if coin and isinstance(ts, (int, float)) and reg:
            m.setdefault(coin, []).append((int(ts), reg))
    for v in m.values():
        v.sort()
    return m


def btc_regime_series(series):
    """Reproduce the PRODUCTION market regime, which for crypto classifies the
    BTC proxy on the trailing 100 CLOSED 1h candles (see market_regime.
    _detect_for_proxy_with_score). Aggregate BTC 15m -> aligned 1h, then return
    a sorted [(1h_close_ms, regime)] using the pure classify_candles. Every
    per-coin signal inherits the BTC regime in force at its timestamp (this is
    exactly the production cross-sectional semantics, not a per-coin regime)."""
    from types import SimpleNamespace
    from hermes_trader.agents.market_regime import classify_candles
    bars = series.get("BTC")
    if not bars:
        return []
    bucket = 3600_000
    agg = {}
    for t, o, h, l, c, *_ in bars:
        k = (t // bucket) * bucket
        if k not in agg:
            agg[k] = [o, h, l, c]
        else:
            a = agg[k]
            a[1] = max(a[1], h)
            a[2] = min(a[2], l)
            a[3] = c
    keys = sorted(agg)
    out = []
    W = 100
    for n in range(W, len(keys)):
        window = keys[n - W:n]   # trailing 100 closed 1h bars ending before n
        cds = [SimpleNamespace(t=k, o=agg[k][0], h=agg[k][1], l=agg[k][2],
                               c=agg[k][3], v=0.0) for k in window]
        try:
            out.append((keys[n - 1] + bucket, classify_candles(cds)))
        except Exception:
            continue
    return out


def regime_at(reg_series, ts):
    """BTC regime in force at signal ts: latest classified 1h close <= ts."""
    if not reg_series:
        return ""
    tsl = [a[0] for a in reg_series]
    i = bisect.bisect_right(tsl, ts) - 1
    return reg_series[i][1] if i >= 0 else ""


def main() -> int:
    sigs = [json.loads(l) for l in open(
        os.environ.get("CH_TRADES", "/tmp/v2/ch_trades.jsonl"))]
    sigs = [s for s in sigs if s.get("side") == "LONG"]
    series = rp._load_cache(os.environ.get(
        "CH_DEEP", "/tmp/v2/ch_deep_candles.json"))
    reg_series = btc_regime_series(series)

    by_coin = {}
    for s in sigs:
        by_coin.setdefault(s["coin"], []).append(s)

    out = os.environ.get("CH_FEATURES", "/tmp/v2/ch_features.jsonl")
    n = miss = 0
    with open(out, "w", encoding="utf-8") as f:
        for coin, ss in by_coin.items():
            bars = series.get(coin)
            if not bars:
                continue
            ts = [b[0] for b in bars]
            for s in ss:
                i = bisect.bisect_left(ts, s["ts"])
                if i <= 0 or i >= len(bars) - 2:
                    continue
                entry = float(bars[i][1])
                if entry <= 0:
                    continue
                row = {
                    "coin": coin, "ts": s["ts"],
                    "channel": s.get("channel"), "band": s.get("band"),
                    "composite": s.get("composite"),
                    "regime": regime_at(reg_series, s["ts"]),
                }
                ok = True
                for lab, h in HORIZONS.items():
                    j = i + h
                    if j < len(bars):
                        row[f"fwd_{lab}"] = round(
                            100.0 * (float(bars[j][4]) / entry - 1.0), 4)
                    else:
                        ok = False
                # excursion over first 24h from bar highs/lows
                end = min(len(bars), i + 1 + EXC_BARS)
                hi = max(float(b[2]) for b in bars[i + 1:end])
                lo = min(float(b[3]) for b in bars[i + 1:end])
                row["mfe24"] = round(100.0 * (hi / entry - 1.0), 3)
                row["mae24"] = round(100.0 * (lo / entry - 1.0), 3)
                # bar-based "would arm protect at 1.5/1.0/0.75" and "would hit
                # -max_loss spot 1.5% before +protect" path flags
                if not ok:
                    miss += 1
                f.write(json.dumps(row, separators=(",", ":")) + "\n")
                n += 1
    print("WROTE_FEATURES", out, n, "truncated_horizon", miss)
    return 0


if __name__ == "__main__":
    sys.exit(main())
