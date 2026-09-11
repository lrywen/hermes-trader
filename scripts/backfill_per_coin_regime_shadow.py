#!/usr/bin/env python3
"""Offline BACKFILL of the per-coin macro×own regime probe from events.jsonl.

The live probe (per_coin_regime_shadow) only accumulates rows as new candidates
arrive, and the interesting bucket (macro ALIGNED but the coin's own 4h points
against the side) needs BTC to be trending with the signal book -- which can
take weeks. This script reconstructs the same probe rows for candidates that
ALREADY happened, by replaying candles at each candidate's decision time.

Input : the append-only audit log (default /data/events.jsonl), rows with
        event == "execute" carry coin + side + timestamp for every candidate
        that reached the risk gates.
Output: JSONL in the EXACT live probe schema (default
        /data/per_coin_regime_backfill.jsonl) so
        scripts/reconcile_per_coin_regime_shadow.py can grade it unchanged.

Point-in-time discipline: for each candidate only bars that had already CLOSED
at the decision timestamp are used (same rule as closed_candles_only), and the
labels come from the live pure functions (classify_candles /
regime_strength_score / own_4h_divergence / quadrant_tier) -- no re-implemented
logic, so a backfilled row is comparable with a live one.

Usage:
    python3 scripts/backfill_per_coin_regime_shadow.py
    python3 scripts/backfill_per_coin_regime_shadow.py --days 12 --max-coins 60
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

os.environ["HERMES_BACKTEST"] = "1"
_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO))

from hermes_trader.indicators.math import ema, adx  # noqa: E402
from hermes_trader.client.hl_client import fetch_hl_candles  # noqa: E402
from hermes_trader.models.types import Candle  # noqa: E402
from hermes_trader.agents.market_regime import (  # noqa: E402
    classify_candles, regime_strength_score, _classifier_params,
)
from hermes_trader.agents.per_coin_regime_shadow import (  # noqa: E402
    own_4h_divergence, quadrant_tier, _macro_aligned,
)

_MS_1H = 3_600_000
_MS_4H = 4 * _MS_1H
_CLASSIFY_BARS = 100          # live _detect_for_proxy_with_score uses 100 bars
_OWN4_BARS = 200

EVENTS_FILE = os.environ.get("HERMES_EVENTS_FILE", "/data/events.jsonl")
OUT_FILE = os.environ.get("HERMES_PER_COIN_REGIME_BACKFILL_FILE",
                          "/data/per_coin_regime_backfill.jsonl")


def _closed(bars: List[Candle], cutoff_ms: float, bar_ms: int) -> List[Candle]:
    return [b for b in bars if b.t + bar_ms <= cutoff_ms]


def _parse_iso(ts: str) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def load_candidates(path: str, days: float) -> List[dict]:
    """Unique (coin, side, hour-bucket) candidates from the execute events.

    One 1h bar produces the same labels for the same coin/side, so repeated
    scan hits inside an hour are collapsed -- otherwise n is inflated by the
    scan cadence instead of by independent observations.
    """
    cutoff_ms = (time.time() - days * 86400) * 1000.0
    seen: set = set()
    out: List[dict] = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or '"execute"' not in line:
                continue
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            if ev.get("event") != "execute":
                continue
            p = ev.get("payload") or {}
            coin = str(p.get("coin") or "")
            side = str(p.get("side") or "").lower()
            if not coin or side not in ("long", "short"):
                continue
            dt = _parse_iso(ev.get("timestamp", ""))
            if dt is None:
                continue
            ms = dt.timestamp() * 1000.0
            if ms < cutoff_ms:
                continue
            key = (coin, side, int(ms // _MS_1H))
            if key in seen:
                continue
            seen.add(key)
            out.append({"coin": coin, "side": side, "ms": ms,
                        "timestamp": dt.astimezone(timezone.utc)
                                       .strftime("%Y-%m-%dT%H:%M:%SZ"),
                        "trace_id": ev.get("trace_id"),
                        "payload": p})
    out.sort(key=lambda r: r["ms"])
    return out


class CandleStore:
    """Per-coin 1h/4h series with a fetch-failure blacklist and pacing."""

    def __init__(self, sleep_s: float = 0.25, bars_1h: int = 800,
                 bars_4h: int = 400):
        self.sleep_s = sleep_s
        self.bars_1h = bars_1h
        self.bars_4h = bars_4h
        self._cache: Dict[tuple, List[Candle]] = {}
        self.failed: Counter = Counter()

    def get(self, coin: str, interval: str) -> List[Candle]:
        key = (coin, interval)
        if key not in self._cache:
            count = self.bars_1h if interval == "1h" else self.bars_4h
            try:
                self._cache[key] = fetch_hl_candles(coin, interval, count) or []
            except Exception as e:  # rate limit / delisted / unknown coin
                self.failed[coin] += 1
                self._cache[key] = []
                print(f"  ! candle fetch failed {coin} {interval}: {e}",
                      file=sys.stderr)
            if self.sleep_s:
                time.sleep(self.sleep_s)
        return self._cache[key]


def label(cand: dict, store: CandleStore, params: tuple,
          require_own_adx: float, strong_score: float, mid_score: float
          ) -> Optional[dict]:
    """Rebuild one probe row at the candidate's decision time."""
    fast, slow, slope_up, adx_max = params
    coin, side, ms = cand["coin"], cand["side"], cand["ms"]

    btc = _closed(store.get("BTC", "1h"), ms, _MS_1H)[-_CLASSIFY_BARS:]
    if len(btc) < 35:
        return None
    macro_regime = classify_candles(btc, fast, slow, slope_up, adx_max)
    macro_score = regime_strength_score(btc)

    own1 = _closed(store.get(coin, "1h"), ms, _MS_1H)[-_CLASSIFY_BARS:]
    if len(own1) >= 35:
        own_regime = classify_candles(own1, fast, slow, slope_up, adx_max)
        own_score: Optional[float] = regime_strength_score(own1)
    else:
        own_regime, own_score = "neutral", None

    own4 = _closed(store.get(coin, "4h"), ms, _MS_4H)[-_OWN4_BARS:]
    analysis: Dict[str, Any] = {}
    if len(own4) >= 35:
        closes = [float(c.c) for c in own4]
        try:
            e21 = ema(closes, 21)[-1]
        except Exception:
            e21 = None
        try:
            a14 = adx(own4, 14)[-1]
        except Exception:
            a14 = None
        analysis = {"close4h": closes[-1], "ema21_4h": e21, "adx4h": a14}

    div = own_4h_divergence(side, analysis, require_own_adx)
    aligned = _macro_aligned(macro_regime, side)
    quad = quadrant_tier(macro_regime=macro_regime, macro_score=macro_score,
                         own_regime=own_regime, own_score=own_score, side=side,
                         strong_score=strong_score, mid_score=mid_score)
    would = ("demote_to_weak_aligned" if div["would_demote"] else "pass") \
        if aligned else "n/a_non_aligned"

    p = cand.get("payload") or {}
    detail: Dict[str, Any] = {
        "macro_regime": macro_regime,
        "macro_trend_score": round(macro_score, 4) if macro_score is not None else None,
        "macro_via": "BTC",
        "composite_score": p.get("composite_score"),
        "confidence": p.get("confidence"),
        "own_1h_regime": own_regime,
        "own_1h_score": round(own_score, 4) if own_score is not None else None,
        "quadrant_tier": quad.get("tier"),
        "tier_would": quad.get("would"),
    }
    detail.update(div)
    return {
        "timestamp": cand["timestamp"],
        "trace_id": cand.get("trace_id"),
        "rule": "per_coin_regime",
        "coin": coin,
        "side": side,
        "macro_aligned": bool(aligned),
        "would": would,
        "detail": detail,
        "backfilled": True,
        "outcome": None,
        "exit_px": None,
        "pnl_usd": None,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--events", default=EVENTS_FILE)
    ap.add_argument("--out", default=OUT_FILE)
    ap.add_argument("--days", type=float, default=12.0,
                    help="Only backfill candidates newer than this (the "
                         "reconcile candle window is ~12.5 days)")
    ap.add_argument("--max-coins", type=int, default=0,
                    help="Keep only the N most frequent coins (0 = all); "
                         "guards against HL candle rate limits")
    ap.add_argument("--sleep", type=float, default=0.25,
                    help="Seconds between candle fetches")
    ap.add_argument("--require-own-adx", type=float, default=20.0)
    ap.add_argument("--strong-own-score", type=float, default=0.65)
    ap.add_argument("--mid-own-score", type=float, default=0.55)
    args = ap.parse_args()

    if not os.path.exists(args.events):
        print(f"events file not found: {args.events}")
        return 1

    cands = load_candidates(args.events, args.days)
    if not cands:
        print("no candidates in window")
        return 1
    freq = Counter(c["coin"] for c in cands)
    if args.max_coins > 0:
        keep = {c for c, _ in freq.most_common(args.max_coins)}
        cands = [c for c in cands if c["coin"] in keep]
    coins = sorted({c["coin"] for c in cands})
    print(f"candidates={len(cands)}  coins={len(coins)}  "
          f"window={cands[0]['timestamp']} .. {cands[-1]['timestamp']}")

    store = CandleStore(sleep_s=args.sleep)
    params = _classifier_params()
    rows: List[dict] = []
    skipped = 0
    for i, cand in enumerate(cands, 1):
        row = label(cand, store, params, args.require_own_adx,
                    args.strong_own_score, args.mid_own_score)
        if row is None:
            skipped += 1
            continue
        rows.append(row)
        if i % 200 == 0:
            print(f"  ... {i}/{len(cands)} labelled", flush=True)

    with open(args.out, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    tiers: Counter = Counter()
    woulds: Counter = Counter()
    by_side: Dict[str, Counter] = defaultdict(Counter)
    for r in rows:
        tiers[(r["detail"] or {}).get("quadrant_tier")] += 1
        woulds[r["would"]] += 1
        by_side[r["side"]][r["would"]] += 1

    print(f"\nwrote {len(rows)} rows -> {args.out}  (skipped={skipped}, "
          f"coins_with_fetch_errors={len(store.failed)})")
    print("\n── quadrant tier ──")
    for k, v in tiers.most_common():
        print(f"  {str(k):12} {v}")
    print("\n── would ──")
    for k, v in woulds.most_common():
        print(f"  {str(k):24} {v}")
    print("\n── would by side ──")
    for s in sorted(by_side):
        parts = "  ".join(f"{k}={v}" for k, v in by_side[s].most_common())
        print(f"  {s:6} {parts}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
