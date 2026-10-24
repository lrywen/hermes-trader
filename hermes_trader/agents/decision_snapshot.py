"""P1: append-only decision-snapshot ledger for counterfactual replay.

NOFX-style decision replay needs two inputs Hermes previously threw away:

1. the *proposed* order on a blocked intent — recorded in the ``risk_gate``
   event's ``proposed`` field (executor ``_record_risk_gate_block``);
2. a durable, per-cycle record of **what the decision layer saw** — this module.

Per autonomous-loop cycle, for every coin that reached a perception/decision
point, we append one compact JSON line to an append-only, day-partitioned file::

    ${HERMES_DATA_DIR:-/data}/decision-snapshots/date=YYYY-MM-DD/snapshots.jsonl

Each line carries: cycle/bar timestamp, coin, mid, composite score + fired
triggers, the perception's microstructure reads (volume profile / CVD / whale),
and account equity. It deliberately stores **bounded scalars** (no full candle
arrays — those are reconstructible by ``bar_close_ms`` from book-raw / candle
history, and storing them per cycle would be heavy). Paired with the proposed
plan in ``risk_gate``, this lets an offline replayer rebuild "what the AI saw
and what it would have done" for both admitted and blocked intents.

Design:
- append-only (never overwritten), fsync per write group, best-effort;
- fail-open: any failure logs and never blocks the trading loop;
- rotation/retention handled by scripts/clean_trades_raw.py.
"""

from __future__ import annotations

import json
import math
import os
import time
from typing import Any, Iterable, Optional

_SNAP_DIR = os.path.join(
    os.environ.get("HERMES_DATA_DIR", "/data"), "decision-snapshots"
)


def _day_dir(ts_ms: int, root: str) -> str:
    d = time.gmtime(ts_ms / 1000.0)
    return os.path.join(root, f"date={d.tm_year:04d}-{d.tm_mon:02d}-{d.tm_mday:02d}")


def _finite(v: Any) -> Optional[float]:
    try:
        fv = float(v)
    except (TypeError, ValueError):
        return None
    return fv if math.isfinite(fv) else None


def _compact_triggers(triggers: Any, max_n: int = 12) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for t in (triggers or [])[:max_n]:
        if not isinstance(t, dict):
            continue
        out.append({
            "name": t.get("name"),
            "fired": bool(t.get("fired")),
            "strength": _finite(t.get("strength")),
        })
    return out


def build_snapshot_record(
    perception: dict[str, Any],
    *,
    cycle_ts_ms: int,
    equity: Optional[float] = None,
    regime: Optional[str] = None,
    mode: Optional[str] = None,
) -> Optional[dict[str, Any]]:
    """Map one perception (+ cycle context) to a bounded snapshot dict.

    Returns None if the perception has no coin (nothing identifiable to replay).
    Pure: no I/O, never raises on malformed perception.
    """
    try:
        coin = perception.get("coin")
        if not coin:
            return None
        rec: dict[str, Any] = {
            "ts": int(cycle_ts_ms),
            "bar_close_ms": perception.get("bar_close_ms"),
            "coin": str(coin),
            "mid": _finite(perception.get("mid")),
            "composite_score": _finite(perception.get("composite_score")),
            # Scoring-scale tag (triggers.SCORE_VERSION) so counterfactual
            # replay can split composite values across score-mapping changes.
            # Absent/None on old rows means the pre-taper legacy curve.
            "score_version": perception.get("score_version"),
            "triggers": _compact_triggers(perception.get("triggers")),
        }
        # Microstructure reads are optional nested blobs; keep only if present.
        if perception.get("volume_profile") is not None:
            rec["volume_profile"] = perception.get("volume_profile")
        if perception.get("cvd_divergence") is not None:
            rec["cvd_divergence"] = perception.get("cvd_divergence")
        if perception.get("whale_signal"):
            rec["whale_signal"] = perception.get("whale_signal")
        eq = _finite(equity)
        if eq is not None:
            rec["equity"] = eq
        if regime:
            rec["regime"] = str(regime)
        if mode:
            rec["mode"] = str(mode)
        return rec
    except Exception:
        return None


def append_snapshots(
    records: Iterable[dict[str, Any]],
    *,
    root: Optional[str] = None,
    ts_ms: Optional[int] = None,
) -> int:
    """Append snapshot records to the day-partitioned JSONL (best-effort).

    Returns the number actually written. Creates the day directory; opens in
    append mode and flushes+fsyncs so a crash loses at most the current group.
    Never raises into the caller.
    """
    base = root or _SNAP_DIR
    now = int(ts_ms if ts_ms is not None else time.time() * 1000)
    lines: list[str] = []
    for r in records:
        if not isinstance(r, dict) or not r.get("coin"):
            continue
        try:
            lines.append(json.dumps(r, separators=(",", ":"), allow_nan=False))
        except (ValueError, TypeError):
            # allow_nan=False raises on NaN/Inf; skip that record rather than
            # writing illegal JSON that downstream parsers reject.
            continue
    if not lines:
        return 0
    try:
        day_dir = _day_dir(now, base)
        os.makedirs(day_dir, exist_ok=True)
        path = os.path.join(day_dir, "snapshots.jsonl")
        with open(path, "a", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        return len(lines)
    except Exception:
        return 0


def record_cycle_snapshots(
    perceptions: Iterable[dict[str, Any]],
    *,
    cycle_ts_ms: Optional[int] = None,
    equity: Optional[float] = None,
    regime: Optional[str] = None,
    mode: Optional[str] = None,
    root: Optional[str] = None,
) -> int:
    """Convenience: build then append one snapshot per perception in a cycle."""
    ts = int(cycle_ts_ms if cycle_ts_ms is not None else time.time() * 1000)
    recs = [
        r for r in (
            build_snapshot_record(p, cycle_ts_ms=ts, equity=equity,
                                  regime=regime, mode=mode)
            for p in perceptions
        ) if r
    ]
    return append_snapshots(recs, root=root, ts_ms=ts)
