#!/usr/bin/env python3
"""P0-2: measure REALIZED entry slippage from signal close to fillable open.

Why this exists (ADR unification 2026-11-02): the short liquidity floor
``runner_entry_gate.min_short_volume_usd`` is 50M — 10x the 5M long floor. It
can only be relaxed with evidence on actual execution cost in thin books. The
system has ~10 real fills (all long, September), so real-fill slippage cannot
answer that. This script instead measures the *implementable* slippage on the
universe of real runner-gate candidates using historical 5m bars:

  decision price = close of the signal bar containing the candidate ts
  fill price     = OPEN of the next 5m bar (first marketable taker print)
  raw slip bp    = (fill - decision) / decision * 1e4
  dir slip bp    = +raw for long (buying higher costs), -raw for short
                   (selling lower costs) — always "positive = adverse"

This is price-impact-free (mid-to-mid) microstructure slippage at the 5m
horizon: it captures the adverse move between decision and the earliest fill,
NOT order-book depth impact. It is the floor a taker pays before depth cost.
Pair it with the fixed 9.26bp round-trip cost to compare tiers.

Buckets by the candidate's OWN recorded volume_24h_usd (no survivorship from a
today snapshot): micro <5M, small 5-50M, mid 50-200M, large >=200M. The
pivotal contrast is small (5-50M, currently short-banned) vs mid/large
(currently allowed): if their next-open adverse slip is statistically the
same, the 50M floor is not supported by this evidence.

Read-only. No orders. Anti-look-ahead: fill strictly at the bar AFTER the
signal bar close.

Usage:
    python3 scripts/measure_realized_slippage.py --days 30
    python3 scripts/measure_realized_slippage.py --no-network   # cache only
"""
from __future__ import annotations

import argparse
import bisect
import datetime as _dt
import glob
import json
import os
import statistics as st
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Optional

BAR_MS = 5 * 60_000
_DATA_DIR = os.environ.get("HERMES_DATA_DIR", "/data")
_BINANCE_BASE = "https://data-api.binance.vision"

# Volume tiers keyed on the candidate's own recorded 24h notional.
# "unknown" is a real bucket, not micro: ~52% of runner-gate candidates have
# no recorded volume_24h_usd, and folding them into micro_lt5M (the old
# behaviour) made the thinnest tier ~70% missing-data and every "all sample"
# statement wrong (review D2, 2026-11-02).
UNKNOWN_TIER = "unknown_volume"
TIERS = [
    ("micro_lt5M", 0.0, 5e6),
    ("small_5M_50M", 5e6, 5e7),
    ("medium_50M_200M", 5e7, 2e8),
    ("large_ge200M", 2e8, float("inf")),
]
TIER_NAMES = [name for name, _, _ in TIERS] + [UNKNOWN_TIER]


def load_candidates(pattern: str) -> list[dict[str, Any]]:
    """De-duplicated runner-gate candidates (same key as reconcile)."""
    rows: dict[tuple, dict[str, Any]] = {}
    for fp in sorted(glob.glob(pattern)):
        try:
            for line in open(fp, errors="ignore"):
                line = line.strip()
                if not line:
                    continue
                r = json.loads(line)
                rows[(r.get("ts"), r.get("coin"), r.get("side"), r.get("rule"))] = r
        except Exception:
            continue
    return list(rows.values())


# ── Binance spot 5m data (mirrors backtest_short_5m, standalone) ──────────

_CACHE: dict[str, list[list]] = {}
_CACHE_PATH = ""
_SYM_MEMO: dict[str, Optional[str]] = {}


def cache_load(path: str) -> None:
    global _CACHE_PATH
    _CACHE_PATH = path
    try:
        _CACHE = json.loads(Path(path).read_text()) if Path(path).exists() else {}
    except Exception:
        _CACHE = {}


def cache_flush() -> None:
    if _CACHE_PATH:
        try:
            Path(_CACHE_PATH).write_text(json.dumps(_CACHE))
        except Exception:
            pass


def _binance_symbol(coin: str) -> Optional[str]:
    if coin in _SYM_MEMO:
        return _SYM_MEMO[coin]
    tries = [f"{coin}USDT"]
    if coin.startswith("k") and len(coin) > 1:
        tries.append(f"{coin[1:]}USDT")
    found = None
    for s in tries:
        qs = urllib.parse.urlencode({"symbol": s, "interval": "5m", "limit": 1})
        req = urllib.request.Request(
            f"{_BINANCE_BASE}/api/v3/klines?{qs}",
            headers={"User-Agent": "hermes-slippage"})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                if json.loads(r.read().decode()):
                    found = s
                    break
        except Exception:
            pass
        time.sleep(0.04)
    _SYM_MEMO[coin] = found
    return found


def _binance_klines(symbol: str, start_ms: int, end_ms: int) -> list[list]:
    out: list[list] = []
    t = start_ms
    while t <= end_ms:
        qs = urllib.parse.urlencode({"symbol": symbol, "interval": "5m",
                                     "startTime": t, "endTime": end_ms, "limit": 1000})
        req = urllib.request.Request(
            f"{_BINANCE_BASE}/api/v3/klines?{qs}",
            headers={"User-Agent": "hermes-slippage"})
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                raw = json.loads(r.read().decode())
        except Exception:
            break
        if not raw:
            break
        out += [[int(k[0]), float(k[1]), float(k[2]), float(k[3]), float(k[4])]
                for k in raw]
        last = int(raw[-1][0])
        if len(raw) < 1000:
            break
        t = last + BAR_MS
        time.sleep(0.05)
    return out


def _series(coin: str, start_ms: int, end_ms: int, use_network: bool):
    """Return (ts_list, close_list, open_list) spanning the window, or None."""
    sym = _binance_symbol(coin) if use_network else None
    if use_network and sym is None:
        return None
    if not use_network:
        return None
    key = f"{sym}:5m"
    raw = _binance_klines(sym, start_ms, end_ms)
    merged = {r[0]: r for r in (_CACHE.get(key) or [])}
    for r in raw:
        merged[r[0]] = r
    _CACHE[key] = [merged[t] for t in sorted(merged)]
    rows = [r for r in merged.values() if start_ms <= r[0] <= end_ms]
    rows.sort(key=lambda r: r[0])
    if len(rows) < 2:
        return None
    return [r[0] for r in rows], [r[4] for r in rows], [r[1] for r in rows]


# ── HyperLiquid perp series (review D1): same venue as the candidates, read
# from the local candle cache (COIN|5m -> {ts_str: [o,h,l,c,v]}). No cross-venue
# basis. Coverage is smaller than Binance spot; prefer fidelity over n.
_HL_CACHE: dict[str, dict] = {}
_HL_CACHE_LOADED = False


def hl_cache_load(path: str) -> None:
    global _HL_CACHE_LOADED
    try:
        _HL_CACHE.clear()
        _HL_CACHE.update(json.loads(Path(path).read_text()))
    except Exception:
        _HL_CACHE.clear()
    _HL_CACHE_LOADED = True


def _series_hl(coin: str, start_ms: int, end_ms: int, use_network: bool = True):
    """Return (ts, close, open) for the HyperLiquid perp, same venue as the
    candidates (review D1). Local cache first; if it does not cover the window
    and networking is allowed, fetch via the production HL candleSnapshot path
    (paged, bar-cached). Returns None if neither yields >=2 bars.
    """
    node = _HL_CACHE.get(f"{coin}|5m")
    local: list[tuple] = []
    if isinstance(node, dict):
        for ts_str, k in node.items():
            try:
                t = int(ts_str)
                if start_ms <= t <= end_ms:
                    # HL candle vector is [o, h, l, c, v]
                    local.append((t, float(k[3]), float(k[0])))
            except Exception:
                continue
    local.sort(key=lambda r: r[0])

    covers = bool(local) and local[0][0] <= start_ms + BAR_MS and local[-1][0] >= end_ms - BAR_MS
    if covers or not use_network:
        rows = local
    else:
        try:
            from hermes_trader.data import historical_candles as hc
            candles = hc.fetch_candle_range(coin, "5m", start_ms, end_ms)
            rows = [(c.t, c.c, c.o) for c in candles if start_ms <= c.t <= end_ms]
        except Exception:
            rows = local
    if len(rows) < 2:
        return None
    return [r[0] for r in rows], [r[1] for r in rows], [r[2] for r in rows]


def _tier(vol_usd: Optional[float]) -> str:
    # Missing/non-positive volume is reported separately, never silently
    # treated as the thinnest real-liquidity bucket (review D2).
    if vol_usd is None or vol_usd <= 0:
        return UNKNOWN_TIER
    v = float(vol_usd)
    for name, lo, hi in TIERS:
        if lo <= v < hi:
            return name
    return UNKNOWN_TIER


def _pair_bar(ts: int, bars_t: list[int], bars_c: list[float],
              bars_o: list[float]) -> Optional[tuple[int, float, float]]:
    """Locate the signal bar containing ``ts`` and return (bar_ts, signal
    close, NEXT-bar open). Strict anti-look-ahead: the fill price always comes
    from the bar AFTER the one whose close is the decision price.

    Returns None if:
      * ts falls before the first bar,
      * there is no bar after the signal bar,
      * ts is not within one BAR_MS of a bar open (misaligned / gap),
      * either price is non-positive.
    """
    j = bisect.bisect_right(bars_t, ts - 1) - 1
    if j < 0 or j + 1 >= len(bars_t) or abs(bars_t[j] - ts) > BAR_MS:
        return None
    decision = bars_c[j]
    fill = bars_o[j + 1]
    if not (decision > 0 and fill > 0):
        return None
    return bars_t[j], decision, fill


def measure(cands: list[dict[str, Any]], use_network: bool,
            progress: bool = False, source: str = "binance") -> list[dict[str, Any]]:
    """Pair each candidate's signal-bar close with the next-bar open.

    source="binance": Binance spot 5m (broad coverage, cross-venue basis).
    source="hl":      local HyperLiquid perp 5m cache (same venue, no basis).
    """
    # group by coin so one fetch covers all that coin's candidates
    by_coin: dict[str, list[dict]] = {}
    for c in cands:
        if c.get("coin") and c.get("ts"):
            by_coin.setdefault(c["coin"], []).append(c)

    out: list[dict[str, Any]] = []
    for i, (coin, cs) in enumerate(sorted(by_coin.items())):
        ts_list = sorted(int(c["ts"]) for c in cs)
        start_ms = ts_list[0] - 2 * BAR_MS
        end_ms = ts_list[-1] + 2 * BAR_MS
        ser = None
        if source == "hl":
            try:
                ser = _series_hl(coin, start_ms, end_ms, use_network=use_network)
            except Exception:
                ser = None
        elif use_network:
            try:
                ser = _series(coin, start_ms, end_ms, True)
            except Exception:
                ser = None
        if progress and (i + 1) % 10 == 0:
            print(f"  ...{i+1}/{len(by_coin)} coins, {len(out)} pairs",
                  file=sys.stderr)
        if not ser:
            continue
        bars_t, bars_c, bars_o = ser
        for c in cs:
            paired = _pair_bar(int(c["ts"]), bars_t, bars_c, bars_o)
            if paired is None:
                continue
            bar_ts, decision, fill = paired
            raw_bp = (fill - decision) / decision * 1e4
            side = c.get("side")
            # adverse-when-positive: long buys the open (higher=worse), short
            # sells the open (lower=worse).
            dir_bp = raw_bp if side == "long" else (-raw_bp if side == "short" else raw_bp)
            out.append({
                "coin": coin, "side": side, "ts": bar_ts, "source": source,
                "tier": _tier(c.get("volume_24h_usd")),
                "volume_24h_usd": c.get("volume_24h_usd"),
                "decision_close": decision, "fill_open": fill,
                "raw_bp": round(raw_bp, 3), "adverse_bp": round(dir_bp, 3),
                "admitted": bool(c.get("admitted")),
                "fresh_impulse": bool(c.get("fresh_impulse")),
            })
    return out


def _quant(xs: list[float], q: float) -> float:
    xs = sorted(xs)
    if not xs:
        return float("nan")
    return xs[min(len(xs) - 1, int(q * (len(xs) - 1)))]


def _boot_mean_ci(xs: list[float], B: int = 2000, seed: int = 7) -> tuple[float, float]:
    import random
    if len(xs) < 2:
        return (float("nan"), float("nan"))
    rnd = random.Random(seed)
    n = len(xs)
    means = []
    for _ in range(B):
        means.append(sum(xs[rnd.randrange(n)] for _ in range(n)) / n)
    means.sort()
    return means[int(.025 * B)], means[int(.975 * B)]


def _day_key(ts_ms: int) -> str:
    return _dt.datetime.utcfromtimestamp(ts_ms / 1000).strftime("%Y-%m-%d")


def _boot_day_ci(pairs: list[dict[str, Any]], B: int = 4000,
                 seed: int = 7) -> dict[str, Any]:
    """UTC day-block bootstrap (review D3).

    Candidates cluster heavily in time/coins, so an iid resample understates
    uncertainty. Average adverse_bp WITHIN each UTC day first, then resample
    the daily means (B=4000, fixed seed). Returns the day count and CI of the
    cross-day mean. Fewer than 2 distinct days -> NaN interval.
    """
    import random
    by_day: dict[str, list[float]] = {}
    for p in pairs:
        by_day.setdefault(_day_key(p["ts"]), []).append(p["adverse_bp"])
    daily = [st.mean(v) for v in by_day.values()]
    if len(daily) < 2:
        return {"days": len(daily), "day_mean_bp": (round(st.mean(daily), 3)
                if daily else None), "day_ci95_bp": [float("nan"), float("nan")]}
    rnd = random.Random(seed)
    n = len(daily)
    means = []
    for _ in range(B):
        means.append(sum(daily[rnd.randrange(n)] for _ in range(n)) / n)
    means.sort()
    return {
        "days": n,
        "day_mean_bp": round(st.mean(daily), 3),
        "day_ci95_bp": [round(means[int(.025 * B)], 3),
                        round(means[int(.975 * B)], 3)],
    }


def report(pairs: list[dict[str, Any]]) -> dict[str, Any]:
    def block(xs: list[dict]) -> dict[str, Any]:
        adv = [p["adverse_bp"] for p in xs]
        if not adv:
            return {"n": 0}
        lo, hi = _boot_mean_ci(adv)
        return {
            "n": len(adv),
            "mean_adverse_bp": round(st.mean(adv), 3),
            "ci95_bp": [round(lo, 3), round(hi, 3)],
            # UTC day-block bootstrap (preferred inference; iid CI is secondary)
            **_boot_day_ci(xs),
            "median_bp": round(_quant(adv, .5), 3),
            "p90_adverse_bp": round(_quant(adv, .9), 3),
            "frac_adverse_pct": round(100 * sum(1 for x in adv if x > 0) / len(adv), 1),
        }

    by_tier: dict[str, list] = {name: [] for name in TIER_NAMES}
    for p in pairs:
        by_tier.setdefault(p["tier"], []).append(p)

    res = {
        "total_pairs": len(pairs),
        "overall": block(pairs),
        "by_tier": {t: block(by_tier.get(t, [])) for t in TIER_NAMES},
        "by_side": {},
        "small_vs_large": {},
    }
    for side in ("long", "short"):
        res["by_side"][side] = block([p for p in pairs if p["side"] == side])

    # Pivotal contrast for the 50M floor: small(5-50M) vs medium+large(>=50M).
    # Both sides have REAL recorded volume; unknown_volume is excluded from
    # this contrast (it is reported separately in by_tier).
    small = [p for p in pairs if p["tier"] == "small_5M_50M"]
    big = [p for p in pairs if p["tier"] in ("medium_50M_200M", "large_ge200M")]
    res["small_vs_large"] = {
        "small_5_50M": block(small),
        "medium_or_large_ge50M": block(big),
        "note": "This measures ONE-BAR DELAY COST (signal close -> next 5m "
                "open), not order-book slippage. It answers 'does waiting one "
                "bar cost more in thin-volume coins?', not the 50M floor's "
                "depth-risk rationale. Compare via day_ci95_bp (independent "
                "UTC days), not iid ci95_bp; d>=30 required for a decision.",
    }
    return res


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pattern", default=os.path.join(_DATA_DIR, "runner_gate_shadow.jsonl*"))
    ap.add_argument("--cache", default=os.path.join(_DATA_DIR, "ch_slippage_candles.json"))
    ap.add_argument("--hl-cache", default=os.path.join(_DATA_DIR, "ch_hist_candles.json"),
                    help="local HyperLiquid perp candle cache for --source hl")
    ap.add_argument("--source", choices=("binance", "hl"), default="binance",
                    help="price source; hl = same-venue perp, no cross-venue basis")
    ap.add_argument("--out", default="")
    ap.add_argument("--no-network", action="store_true")
    ap.add_argument("--max-rows", type=int, default=0)
    args = ap.parse_args()

    cands = load_candidates(args.pattern)
    cands.sort(key=lambda c: c.get("ts", 0))
    if args.max_rows:
        cands = cands[-args.max_rows:]
    print(f"candidates: {len(cands)} source={args.source}", file=sys.stderr)

    if args.source == "hl":
        hl_cache_load(args.hl_cache)
        pairs = measure(cands, use_network=not args.no_network, progress=True,
                        source="hl")
    else:
        cache_load(args.cache)
        pairs = measure(cands, use_network=not args.no_network, progress=True,
                        source="binance")
        cache_flush()
    print(f"paired: {len(pairs)}", file=sys.stderr)

    res = report(pairs)
    res["source"] = args.source
    txt = json.dumps(res, ensure_ascii=False, indent=2)
    print(txt)
    if args.out:
        Path(args.out).write_text(txt + "\n")
        with open(args.out + ".pairs.jsonl", "w") as f:
            for p in pairs:
                f.write(json.dumps(p, ensure_ascii=False) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
