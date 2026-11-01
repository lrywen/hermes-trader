#!/usr/bin/env python3
"""P1.5 — coverage audit for the runner-entry gate decision stream.

The fusion plan's precondition for P4 (decoupling the order-lifecycle cluster
"without changing behaviour") is a gate shadow sample of **≥500 verdicts**.
Today the live stream (`runner_gate_shadow.jsonl`) holds only ~189 rows, all on
one UTC day, of which 29 carry the dual counter (``passes40``); the replay
stream (`decision_shadow_replay.jsonl` from ``scripts/replay_gate.py``) can
supply more, but only rows that carry a real verdict count.

This script therefore reports, per source and combined:

* row counts and how many rows are *gradable* (``passes40`` present — old
  rows predate the dual counter);
* UTC-day coverage (a 500-row single-day sample is not coverage, it is one
  scene: gate verdicts are strongly regime-dependent);
* the remaining deficit against ``--min-rows`` (default 500).

Pure read-only aggregation; missing files yield an explicit empty block.
"""
from __future__ import annotations

import argparse
import json
import os
from collections import Counter
from datetime import datetime, timezone
from typing import Any, Optional

DAY_MS = 86_400_000
DEFAULT_MIN_ROWS = 500
DEFAULT_LIVE = os.environ.get("HERMES_DATA_DIR", "/data") + "/runner_gate_shadow.jsonl"
DEFAULT_REPLAY = (os.environ.get("HERMES_DATA_DIR", "/data")
                  + "/decision_shadow_replay.jsonl")


def _utc_day(ts_ms: float) -> str:
    return datetime.fromtimestamp(ts_ms / 1000.0, timezone.utc).strftime("%Y-%m-%d")


def load_rows(path: str) -> Optional[list[dict[str, Any]]]:
    """Parse a decision JSONL **including its rotated generations**.

    ``runner_gate_shadow.jsonl`` rotates daily: the current file holds only
    today's rows, older days live in ``<path>.1``, ``<path>.2``, … Reading just
    the main file systematically under-counts coverage (and drops to ~0 right
    after rotation). We therefore read the main file plus every existing
    ``<path>.N`` generation (N ascending = oldest→newest within rotations).
    Returns ``None`` only when neither the main file nor any generation exists.
    """
    candidates: list[str] = []
    if os.path.exists(path):
        candidates.append(path)
    # Rotated generations: .1 is the most recent rotation, .N the oldest.
    n = 1
    while True:
        gen = f"{path}.{n}"
        if not os.path.exists(gen):
            break
        candidates.append(gen)
        n += 1
    if not candidates:
        return None
    rows: list[dict[str, Any]] = []
    for p in candidates:
        with open(p, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(obj, dict):
                    rows.append(obj)
    return rows


def summarize(rows: list[dict[str, Any]], path: str) -> dict[str, Any]:
    """Per-source coverage: rows, gradable verdicts, UTC days, sides, rules."""
    gradable = 0
    days: set[int] = set()
    undated = 0
    by_side: Counter[str] = Counter()
    rules: Counter[str] = Counter()
    admitted = 0
    replay_rows = 0
    for r in rows:
        if r.get("replay"):
            replay_rows += 1
        try:
            ts = float(r.get("ts"))
        except (TypeError, ValueError):
            undated += 1
        else:
            if ts > 0:
                days.add(int(ts) // DAY_MS)
        if r.get("passes40") is None:
            continue  # pre-dual-counter row: no verdict to grade
        gradable += 1
        by_side[str(r.get("side") or "?")] += 1
        rules[str(r.get("rule") or "(admitted)")] += 1
        if r.get("admitted"):
            admitted += 1
    ordered = sorted(days)
    return {
        "file": path,
        "exists": True,
        "rows": len(rows),
        "gradable_verdicts": gradable,
        "rows_without_dual_counter": len(rows) - gradable,
        "rows_missing_ts": undated,
        "replay_tagged_rows": replay_rows,
        "utc_days": len(ordered),
        "first_day": _utc_day(ordered[0] * DAY_MS) if ordered else None,
        "last_day": _utc_day(ordered[-1] * DAY_MS) if ordered else None,
        "admitted": admitted,
        "by_side": dict(sorted(by_side.items())),
        "top_rules": dict(rules.most_common(10)),
    }


def combine(live: dict[str, Any], replay: dict[str, Any],
            min_rows: int = DEFAULT_MIN_ROWS) -> dict[str, Any]:
    """Union of the two sources against the ``min_rows`` coverage gate.

    Days are unioned only when both sides expose day bounds; the union is
    reported as a *lower bound* (``utc_days_min``) because neither source
    publishes its day set, only its span.
    """
    verdicts = int(live.get("gradable_verdicts") or 0) + \
        int(replay.get("gradable_verdicts") or 0)
    deficit = max(0, min_rows - verdicts)
    return {
        "min_rows": min_rows,
        "gradable_verdicts": verdicts,
        "deficit": deficit,
        "meets_min_rows": deficit == 0,
        "utc_days_min": max(int(live.get("utc_days") or 0),
                            int(replay.get("utc_days") or 0)),
        "sources_present": [s["file"] for s in (live, replay) if s.get("exists")],
    }


def evaluate(live_path: str = DEFAULT_LIVE, replay_path: str = DEFAULT_REPLAY,
             min_rows: int = DEFAULT_MIN_ROWS) -> dict[str, Any]:
    """Build the per-source + combined coverage report (pure I/O, no mutation)."""
    blocks = {}
    for name, path in (("live", live_path), ("replay", replay_path)):
        rows = load_rows(path)
        if rows is None:
            blocks[name] = {"file": path, "exists": False, "rows": 0,
                            "gradable_verdicts": 0, "utc_days": 0}
        else:
            blocks[name] = summarize(rows, path)
    blocks["combined"] = combine(blocks["live"], blocks["replay"], min_rows)
    return blocks


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--live-file", default=DEFAULT_LIVE)
    ap.add_argument("--replay-file", default=DEFAULT_REPLAY)
    ap.add_argument("--min-rows", type=int, default=DEFAULT_MIN_ROWS)
    ap.add_argument("--out", default=None)
    ap.add_argument("--strict", action="store_true",
                    help="exit 2 when the coverage gate is not met")
    args = ap.parse_args(argv)
    report = evaluate(args.live_file, args.replay_file, args.min_rows)
    blob = json.dumps(report, indent=2, sort_keys=True, default=str)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(blob + "\n")
    print(blob)
    if args.strict and not report["combined"]["meets_min_rows"]:
        return 2
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
