#!/usr/bin/env python3
"""P0-A (2026-10-19): does the live decision chain show a directional edge on
REAL Hyperliquid shadow fills, before the trigger weights are trusted for LIVE?

Scope / honesty
---------------
The trigger weights in config.py were derived from a 1-year Binance-SPOT OHLCV
event study and are explicitly "not yet validated in SHADOW against real
Hyperliquid-fill ROE". The strictly-correct validation is *per fired trigger
name x weight -> real fill ROE*. That is NOT reconstructible for the history:
fired trigger names were only persisted from 2026-10-14 (decision-snapshots),
which overlaps just 7 of the 218 closed fills. The fill rows never stored
fired_names either, so a point-in-time trigger label cannot be recovered for the
other 211 without coin+time nearest-neighbour guessing (which would leak and
contaminate the verdict).

This script therefore answers the COARSER but still decisive question that the
existing 218 real fills CAN support, using only fields stored ON the fill at
open time (zero join / zero look-ahead risk):

  * side (long/short)
  * entry_regime (up / down / chop / neutral) recorded at entry
  * realized_pnl_pct (net ROE, fees included) and realized_pnl_usd

It tests whether the chain as a whole has a directional edge:

  1. overall expectancy per account (taker / maker), net ROE and net USD;
  2. trend-aligned (long in up, short in down) vs counter/non-trend entries;
  3. long vs short;
  4. per entry_regime;
  5. a secondary, EXPLICITLY LOW-CONFIDENCE confidence sub-sample, recovered by
     nearest research event within a tight window (only ~56% recoverable and
     async, so it is labelled advisory, never primary evidence).

Significance is a paired/bootstrap-free non-parametric view: bootstrap 95% CI
on the median and mean net ROE, a sign test on the win fraction, and a
Mann-Whitney U between aligned and counter groups (normal approximation).

Read-only: never places orders and never writes the book.
"""

from __future__ import annotations

import argparse
import bisect
import glob
import gzip
import json
import math
import os
import statistics
from collections import Counter, defaultdict
from typing import Any

DATA_DIR = os.environ.get("HERMES_DATA_DIR", "/data")
DEFAULT_BOOK = os.path.join(DATA_DIR, ".shadow-book.json")
ACCOUNTS = ("taker", "maker_shadow")

# Research events are async; only accept a research verdict as the entry's label
# when it landed within [-15 min, +2 min] of the open. Even then it is advisory.
RESEARCH_LOOKBACK_MS = 15 * 60 * 1000
RESEARCH_LOOKAHEAD_MS = 2 * 60 * 1000

_rng_state = 0x243F6A8885A308D3


def _rng() -> float:
    """Deterministic xorshift64* so the report is reproducible without numpy."""
    global _rng_state
    x = _rng_state
    x ^= x >> 12
    x ^= (x << 25) & 0xFFFFFFFFFFFFFFFF
    x ^= x >> 27
    _rng_state = x
    return ((x * 0x2545F4914F6CDD1D) & 0xFFFFFFFFFFFFFFFF) / 2**64


def _f(x: Any, default: float = 0.0) -> float:
    try:
        if x is None:
            return default
        return float(x)
    except (TypeError, ValueError):
        return default


def _opener(path: str):
    return gzip.open(path, "rt", errors="replace") if path.endswith(".gz") \
        else open(path, errors="replace")


# --------------------------------------------------------------------------- load

def load_closed_trades(book_path: str) -> list[dict]:
    """Pair each open with its close per account -> one row per round trip."""
    book = json.load(open(book_path))
    rows: list[dict] = []
    for acct, av in book.get("accounts", {}).items():
        opens: dict[str, dict] = {}
        for fill in av.get("fills", []):
            if fill.get("type") == "open":
                opens[fill.get("position_id")] = fill
        for fill in av.get("fills", []):
            if fill.get("type") != "close":
                continue
            op = opens.get(fill.get("position_id"))
            rows.append({
                "account": acct,
                "coin": fill.get("coin"),
                "side": fill.get("side"),
                "regime": fill.get("entry_regime") or "unknown",
                "roe": _f(fill.get("realized_pnl_pct")),
                "usd": _f(fill.get("realized_pnl_usd")),
                "spot": _f(fill.get("spot_pct")),
                "hold_min": _f(fill.get("hold_minutes")),
                "open_ts": (op or {}).get("ts") or fill.get("opened_at"),
                "reason": fill.get("reason") or "",
                "confidence": None,
            })
    return rows


def attach_research_confidence(rows: list[dict], session_glob: str) -> int:
    """Best-effort nearest research confidence. Returns rows labelled."""
    res: dict[str, list[tuple]] = defaultdict(list)
    files = sorted(glob.glob(session_glob))
    for path in files:
        if path.endswith(".lock"):
            continue
        for line in _opener(path):
            try:
                ev = json.loads(line)
            except Exception:
                continue
            if ev.get("event") == "research" and ev.get("coin") and ev.get("ts"):
                res[ev["coin"]].append(
                    (ev["ts"], ev.get("verdict"), _f(ev.get("confidence"), None)
                     if ev.get("confidence") is not None else None))
    for c in res:
        res[c].sort()
    n = 0
    for r in rows:
        ts0 = r.get("open_ts")
        if not ts0:
            continue
        arr = res.get(r["coin"], [])
        keys = [t for t, _, _ in arr]
        i = bisect.bisect_right(keys, ts0 + RESEARCH_LOOKAHEAD_MS)
        cand = None
        for j in range(i - 1, -1, -1):
            if keys[j] < ts0 - RESEARCH_LOOKBACK_MS:
                break
            cand = arr[j]
            break
        if cand and cand[2] is not None:
            r["confidence"] = cand[2]
            n += 1
    return n


# --------------------------------------------------------------------------- stats

def _median(xs: list[float]) -> float:
    return statistics.median(xs) if xs else float("nan")


def _boot_ci(xs: list[float], stat, b: int = 5000, alpha: float = 0.05):
    if not xs:
        return float("nan"), float("nan")
    n = len(xs)
    vals = []
    for _ in range(b):
        sample = [xs[int(_rng() * n) % n] for _ in range(n)]
        vals.append(stat(sample))
    vals.sort()
    return vals[int(alpha / 2 * b)], vals[int((1 - alpha / 2) * b)]


def _sign_test_p(wins: int, n: int) -> float:
    """Two-sided exact binomial sign test vs p=0.5 (normal approx, cc)."""
    if n == 0:
        return float("nan")
    z = max(0.0, (abs(wins - n / 2) - 0.5) / math.sqrt(n / 4))
    return min(1.0, math.erfc(z / math.sqrt(2)))


def _mwu_p(a: list[float], b: list[float]) -> float:
    """Mann-Whitney U, two-sided normal approximation with tie/cc correction."""
    na, nb = len(a), len(b)
    if na == 0 or nb == 0:
        return float("nan")
    combined = [(v, 0) for v in a] + [(v, 1) for v in b]
    combined.sort(key=lambda t: t[0])
    # average ranks for ties
    ranks = [0.0] * len(combined)
    i = 0
    tie_counts: list[int] = []
    while i < len(combined):
        j = i
        while j + 1 < len(combined) and combined[j + 1][0] == combined[i][0]:
            j += 1
        avg = (i + j) / 2 + 1
        for k in range(i, j + 1):
            ranks[k] = avg
        if j > i:
            tie_counts.append(j - i + 1)
        i = j + 1
    ra = sum(ranks[k] for k, (_, g) in enumerate(combined) if g == 0)
    u = ra - na * (na + 1) / 2
    n = na + nb
    tie_term = sum(t**3 - t for t in tie_counts)
    var = na * nb / 12 * ((n + 1) - tie_term / (n * (n - 1)) if n > 1 else (n + 1))
    mean = na * nb / 2
    if var <= 0:
        return float("nan")
    z = (abs(u - mean) - 0.5) / math.sqrt(var)
    return math.erfc(z / math.sqrt(2))


def _summ(rows: list[dict]) -> dict:
    roe = [r["roe"] for r in rows]
    usd = [r["usd"] for r in rows]
    wins = sum(1 for v in roe if v > 0)
    n = len(rows)
    med = _median(roe)
    lo, hi = _boot_ci(roe, _median)
    mlo, mhi = _boot_ci(roe, lambda x: sum(x) / len(x))
    return {
        "n": n,
        "win": wins / n if n else float("nan"),
        "win_p": _sign_test_p(wins, n) if n else float("nan"),
        "roe_med": med, "roe_med_ci": (lo, hi),
        "roe_mean": (sum(roe) / n) if n else float("nan"),
        "roe_mean_ci": (mlo, mhi),
        "usd_sum": sum(usd),
        "usd_med": _median(usd),
    }


def _aligned(r: dict) -> bool:
    return (r["side"] == "long" and r["regime"] == "up") or \
           (r["side"] == "short" and r["regime"] == "down")


# --------------------------------------------------------------------------- report

def _line(label: str, s: dict, width: int = 22) -> str:
    if not s or s.get("n", 0) == 0:
        return f"  {label:<{width}} n=0"
    lo, hi = s["roe_med_ci"]
    return (f"  {label:<{width}} n={s['n']:>3}  win={s['win']*100:5.1f}% "
            f"(p={s['win_p']:.3f})  ROE med={s['roe_med']:7.2f}% "
            f"[{lo:6.2f},{hi:6.2f}]  mean={s['roe_mean']:7.2f}% "
            f"netUSD=${s['usd_sum']:8.3f}")


def analyze(rows: list[dict], labelled: int) -> str:
    out: list[str] = []
    out.append("=" * 100)
    out.append("P0-A  REAL Hyperliquid shadow fills — coarse directional-edge check (2026-10-19)")
    out.append("=" * 100)
    out.append(f"closed round-trips: {len(rows)} (accounts: "
               f"{dict(Counter(r['account'] for r in rows))})")
    span = [r for r in rows if r.get('open_ts')]
    if span:
        out.append(f"fees are already inside realized_pnl_pct/usd (net ROE).")
    out.append("")
    out.append("LIMITATION: fired trigger names are not on the fills and only 7 "
               "fills overlap the")
    out.append("post-10-14 decision snapshots, so per-WEIGHT validation is not "
               "possible. This is a")
    out.append("chain-level directional-edge test using only fill-stored PIT "
               "fields (regime/side).")
    out.append("")

    verdict_blocks: list[tuple[str, bool]] = []

    for acct in ACCOUNTS:
        sub = [r for r in rows if r["account"] == acct]
        if not sub:
            continue
        out.append("-" * 100)
        out.append(f"ACCOUNT: {acct}")
        overall = _summ(sub)
        out.append(_line("OVERALL", overall))

        aligned = [r for r in sub if _aligned(r)]
        counter = [r for r in sub if not _aligned(r)]
        sa, sc = _summ(aligned), _summ(counter)
        out.append(_line("trend-ALIGNED", sa))
        out.append(_line("counter/non-trend", sc))
        p = _mwu_p([r["roe"] for r in aligned], [r["roe"] for r in counter])
        out.append(f"  aligned-vs-counter Mann-Whitney two-sided p = {p:.4f}")
        verdict_blocks.append(
            (f"{acct}: aligned median {sa['roe_med']:.2f}% vs "
             f"counter {sc['roe_med']:.2f}% (MWU p={p:.3f})",
             sa["n"] >= 10 and sc["n"] >= 10 and p < 0.05 and sa["roe_med"] > sc["roe_med"]))

        for side in ("long", "short"):
            out.append(_line(f"side={side}", _summ([r for r in sub if r["side"] == side])))
        out.append("")
        out.append("  by entry_regime:")
        for reg in ("up", "down", "chop", "neutral", "unknown"):
            s = _summ([r for r in sub if r["regime"] == reg])
            if s["n"]:
                out.append(_line(f"regime={reg}", s, width=20))
        out.append("")

    # confidence advisory (low coverage, async nearest research event)
    conf_rows = [r for r in rows if r.get("confidence") is not None]
    out.append("-" * 100)
    out.append(f"ADVISORY ONLY — research confidence recovered for {labelled}/{len(rows)} "
               f"({labelled/max(len(rows),1):.0%}); async nearest-event, may mislabel.")
    if conf_rows:
        hi = [r for r in conf_rows if r["confidence"] >= 0.7]
        lo = [r for r in conf_rows if r["confidence"] < 0.7]
        out.append(_line("confidence >=0.70", _summ(hi)))
        out.append(_line("confidence  <0.70", _summ(lo)))
    out.append("")

    # exit-reason mix (explains WHERE expectancy comes from / leaks)
    out.append("-" * 100)
    out.append("exit reason mix (taker):")
    reasons = Counter()
    for r in rows:
        if r["account"] != "taker":
            continue
        key = r["reason"].split("(")[0].strip()[:38] or "?"
        reasons[key] += 1
    for k, v in reasons.most_common(12):
        sel = [r for r in rows if r["account"] == "taker"
               and (r["reason"].split("(")[0].strip()[:38] or "?") == k]
        med = _median([r["roe"] for r in sel])
        out.append(f"  {v:>3}  medROE={med:7.2f}%  {k}")
    out.append("")

    out.append("=" * 100)
    out.append("READING")
    out.append("-" * 100)
    for text, strong in verdict_blocks:
        tag = "edge signal" if strong else "no statistically separable edge"
        out.append(f"  [{tag}] {text}")
    out.append("  A positive OVERALL median with CI above 0 = the chain prints "
               "positive net expectancy")
    out.append("  on real fills; trend-ALIGNED >> counter is the directional "
               "edge P0-A asks about.")
    out.append("  Small per-cell n (esp. shorts/counter) means absence of "
               "significance is NOT proof of no edge.")
    out.append("=" * 100)
    return "\n".join(out)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--book", default=DEFAULT_BOOK)
    ap.add_argument("--session-glob",
                    default=os.path.join(DATA_DIR, "session-log.jsonl*"))
    ap.add_argument("--out", default="")
    args = ap.parse_args()
    rows = load_closed_trades(args.book)
    labelled = attach_research_confidence(rows, args.session_glob)
    report = analyze(rows, labelled)
    if args.out:
        with open(args.out, "w") as fh:
            fh.write(report + "\n")
    print(report)


if __name__ == "__main__":
    main()
