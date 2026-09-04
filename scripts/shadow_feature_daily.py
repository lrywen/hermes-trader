#!/usr/bin/env python3
"""Daily SHADOW-feature summary for the roadmap v3 gray-release features.

Aggregates the four shadow JSONLs written by the trading loop and prints a
compact readiness report, designed to run once per day from the container
scheduler (read-only: nothing is written back to the JSONLs).

Features covered (roadmap docs/advanced-optimization-roadmap.md):
  * confidence_decay       — AI-conviction freshness decay (executor.py)
  * atr_regime_calibration — ATR stop-width regime scaling (executor/sizing)
  * sizing_v2              — volatility-targeted position sizing shadow
  * market_circuit         — extreme-tape halt (only trips are logged)

The confidence_decay section also replays the would-block rate across a grid
of candidate half-lives, so the enforce threshold can be calibrated from
shadow evidence before flipping the mode.

Usage:
    python3 scripts/shadow_feature_daily.py
    python3 scripts/shadow_feature_daily.py --window-hours 24 --push
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

# Container default shadow directory; each feature honors its own env override.
DEFAULT_DATA_DIR = os.environ.get("HERMES_SHADOW_DATA_DIR", "/data")

FILES = {
    "confidence_decay": (
        os.environ.get("HERMES_CONFIDENCE_DECAY_SHADOW_FILE")
        or "confidence_decay_shadow.jsonl"
    ),
    "atr_regime_calibration": (
        os.environ.get("HERMES_ATR_REGIME_CALIB_SHADOW_FILE")
        or "atr_regime_calib_shadow.jsonl"
    ),
    "sizing_v2": "sizing_v2_shadow.jsonl",
    "market_circuit": (
        os.environ.get("HERMES_MARKET_CIRCUIT_SHADOW_FILE")
        or "market_circuit_shadow.jsonl"
    ),
}

# Candidate half-lives for the confidence-decay replay grid (seconds).
HALFLIFE_GRID = [
    (900, "15min (current)"),
    (1800, "30min"),
    (3600, "1h"),
    (5400, "1.5h"),
    (7200, "2h"),
    (10800, "3h"),
    (14400, "4h"),
    (21600, "6h"),
]


def _resolve(path: str, data_dir: str) -> Path:
    p = Path(path)
    return p if p.is_absolute() else Path(data_dir) / p


def _load(path: Path) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    if not path.exists():
        return rows
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    return rows


def _ts_ms(rec: Dict[str, Any]) -> Optional[float]:
    for k in ("ts", "ts_ms", "timestamp_ms"):
        v = rec.get(k)
        if isinstance(v, (int, float)) and v > 0:
            return float(v)
    return None


def _in_window(rec: Dict[str, Any], cutoff_ms: float) -> bool:
    ts = _ts_ms(rec)
    return ts is None or ts >= cutoff_ms  # no ts → count it (don't drop)


def _pct(n: int, d: int) -> str:
    return f"{100.0 * n / d:.1f}%" if d else "N/A"


def _stats(nums: List[float]) -> str:
    if not nums:
        return "n/a"
    return f"min={min(nums):.3f} max={max(nums):.3f} mean={sum(nums)/len(nums):.3f}"


def section_confidence_decay(rows: List[Dict[str, Any]]) -> List[str]:
    out = ["[confidence_decay] AI-conviction freshness decay"]
    if not rows:
        out.append("  no records in window")
        return out
    gate_candidates = [r for r in rows
                       if r.get("confidence_raw", 0) >= r.get("min_confidence", 0.62)]
    blocked = [r for r in rows if r.get("would_block_gate")]
    drops = [r["confidence_raw"] - r["confidence_decayed"]
             for r in rows if r.get("confidence_decayed", 1) < r.get("confidence_raw", 0)]
    ages_min = sorted(r.get("age_s", 0) / 60.0 for r in rows)
    out.append(f"  records={len(rows)}  gate_candidates(raw>=min_conf)={len(gate_candidates)}")
    out.append(f"  would_block={len(blocked)} ({_pct(len(blocked), len(gate_candidates))} of candidates)")
    if drops:
        out.append(f"  confidence drop: mean={sum(drops)/len(drops):.3f} max={max(drops):.3f}")
    if ages_min:
        out.append(f"  signal age(min): p50={ages_min[len(ages_min)//2]:.1f} "
                   f"p90={ages_min[int(len(ages_min)*0.9)]:.1f} max={ages_min[-1]:.1f}")
    top = Counter(r.get("coin", "?") for r in blocked).most_common(6)
    if top:
        out.append("  blocked coins: " + ", ".join(f"{c}x{n}" for c, n in top))

    # Replay grid: would-block rate at each candidate half-life.
    out.append("  halflife replay (would_block rate over gate candidates):")
    for hl_s, label in HALFLIFE_GRID:
        b = 0
        for r in gate_candidates:
            raw = r.get("confidence_raw", 0)
            age = r.get("age_s", 0)
            mc = r.get("min_confidence", 0.62)
            decayed = raw * (2.0 ** (-age / hl_s))
            if decayed < mc:
                b += 1
        out.append(f"    {label:<16s} blocked={b:<4d} rate={_pct(b, len(gate_candidates))}")
    return out


def section_atr_regime(rows: List[Dict[str, Any]]) -> List[str]:
    out = ["[atr_regime_calibration] ATR stop-width scaling"]
    if not rows:
        out.append("  no records in window")
        return out
    regimes = Counter(r.get("vol_regime") for r in rows)
    factors = [r["factor"] for r in rows if isinstance(r.get("factor"), (int, float))]
    changed = [r for r in rows if r.get("would_change")]
    out.append(f"  records={len(rows)}  regimes={dict(regimes)}")
    out.append(f"  factor: {_stats(factors)}")
    out.append(f"  would_change stops: {len(changed)}")
    for r in changed:
        out.append(f"    {r.get('coin')}: ratio={r.get('ratio')} vol={r.get('vol_regime')} "
                   f"factor={r.get('factor')} stop {r.get('raw_stop_pct')}->{r.get('calibrated_stop_pct')}%")
    return out


def section_sizing_v2(rows: List[Dict[str, Any]]) -> List[str]:
    out = ["[sizing_v2] volatility-targeted sizing"]
    if not rows:
        out.append("  no records in window")
        return out
    regimes = Counter(r.get("regime") for r in rows)
    ratios = [r["notional_ratio"] for r in rows
              if isinstance(r.get("notional_ratio"), (int, float))]
    smaller = sum(1 for x in ratios if x < 0.99)
    bigger = sum(1 for x in ratios if x > 1.01)
    same = len(ratios) - smaller - bigger
    lev = Counter(r.get("leverage") for r in rows)
    out.append(f"  records={len(rows)}  regimes={dict(regimes)}")
    out.append(f"  notional v2/v1: {_stats(ratios)}")
    out.append(f"  v2 smaller={smaller} bigger={bigger} same={same}")
    out.append(f"  leverage dist: {dict(sorted(lev.items(), key=lambda x: (x[0] is None, x[0])))}")
    return out


def section_market_circuit(rows: List[Dict[str, Any]]) -> List[str]:
    out = ["[market_circuit] extreme-tape halt (only trips are logged)"]
    if not rows:
        out.append("  no trips in window (clear verdicts are not logged)")
        return out
    actions = Counter(r.get("action") or r.get("verdict") for r in rows)
    triggers = Counter(r.get("trigger") for r in rows)
    out.append(f"  trip records={len(rows)}  actions={dict(actions)}  triggers={dict(triggers)}")
    for r in rows[-10:]:
        reasons = r.get("reasons") or []
        out.append(f"    {r.get('coin') or 'MARKET'} [{r.get('trigger')}] "
                   f"{'; '.join(reasons)[:120]}")
    return out


def build_report(window_hours: int, data_dir: str) -> tuple[str, str]:
    cutoff_ms = (datetime.now(timezone.utc).timestamp() - window_hours * 3600) * 1000.0
    title = f"Shadow feature daily — {datetime.now(timezone.utc).strftime('%Y-%m-%d')} (window {window_hours}h)"
    lines: List[str] = []
    for name, rel in FILES.items():
        path = _resolve(rel, data_dir)
        rows = [r for r in _load(path) if _in_window(r, cutoff_ms)]
        total = len(_load(path))
        lines.append(f"## {path.name}  ({len(rows)} in window / {total} total)")
        if name == "confidence_decay":
            lines += section_confidence_decay(rows)
        elif name == "atr_regime_calibration":
            lines += section_atr_regime(rows)
        elif name == "sizing_v2":
            lines += section_sizing_v2(rows)
        else:
            lines += section_market_circuit(rows)
        lines.append("")
    return title, "\n".join(lines)


def push_feishu(title: str, body: str) -> bool:
    try:
        from hermes_trader import notify
        return notify.send_card(
            title=title, fields={}, category="report", level="info", markdown=body,
        )
    except Exception as e:  # pragma: no cover - best effort
        print(f"Feishu push failed: {e}", file=sys.stderr)
        return False


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--window-hours", type=int, default=24)
    ap.add_argument("--data-dir", default=DEFAULT_DATA_DIR)
    ap.add_argument("--push", action="store_true", help="Push report card to Feishu")
    args = ap.parse_args()

    title, body = build_report(args.window_hours, args.data_dir)
    print("=" * 64)
    print(f"  {title}")
    print("=" * 64)
    print(body)

    if args.push:
        ok = push_feishu(title, body)
        print(f"Feishu push: {'OK' if ok else 'FAILED'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
