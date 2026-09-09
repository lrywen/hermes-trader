"""Cross-process market_circuit heartbeat state (CS-F observability, 2026-09-08).

Why this exists
---------------
``MARKET_CIRCUIT_VERDICTS`` is a prometheus_client ``Counter`` incremented
inside ``market_circuit.evaluate``, which runs in the **trading-loop process**.
Prometheus scrapes the **web process** (``/metrics``). Without
``PROMETHEUS_MULTIPROC_DIR`` the two processes keep separate default
registries, so the loop's increments never appear in the scraped output —
the ``HermesMarketCircuitDataMissing`` alert (an ``increase()`` on that
counter) could never fire in either deployment topology (compose: two
processes in one container; k8s: two containers sharing only ``/data``).

Fix pattern (same as ``positions_snapshot``): the loop is the SINGLE writer
and rewrites one small whole-file state payload per tick via an atomic rename;
the web process only reads it. No flock is needed — there is exactly one
writer, and ``os.replace`` guarantees a reader sees either the old or the new
payload, never a torn file. Cumulative counters are read-modify-written by
that single writer.

Only ``/data`` is shared in BOTH topologies, so the default path lives there;
override with ``HERMES_MARKET_CIRCUIT_STATE_FILE``.

Contract
--------
* ``record_evaluation`` is best-effort and NEVER raises (a heartbeat failure
  must not perturb the trading loop).
* ``read_state`` is best-effort and NEVER raises; a missing/corrupt/garbage
  file or unsupported version returns ``None``. Staleness is the consumer's
  decision (metrics/dashboard compare ``ts`` against ``time.time()``).
"""
from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Optional

from hermes_trader.agents.atomic_io import write_json_atomic

logger = logging.getLogger(__name__)

STATE_FILE = os.environ.get(
    "HERMES_MARKET_CIRCUIT_STATE_FILE", "/data/.market-circuit.state"
)
_STATE_VERSION = 1

# Numeric state enum exported via the ``hermes_market_circuit_state`` gauge.
STATE_CLEAR = 0
STATE_TRIPPED = 1
STATE_DATA_MISSING = 2
STATE_OFF = 3
STATE_ERROR = 4

_STATE_BY_ACTION = {
    "clear": STATE_CLEAR,
    "no_trip": STATE_CLEAR,
    "off": STATE_OFF,
    "data_missing": STATE_DATA_MISSING,
    "error": STATE_ERROR,
    # Every verdict that means a trigger fired (halt armed or would-trip).
    "trip": STATE_TRIPPED,
    "would_trip": STATE_TRIPPED,
    "halt_armed": STATE_TRIPPED,
    "halt_already_armed": STATE_TRIPPED,
    "armed": STATE_TRIPPED,
}


def _state_for(verdict: dict[str, Any]) -> int:
    """Map a verdict dict to the numeric state enum.

    Explicit ``state`` wins (callers may pass one for the exception path);
    otherwise the verdict's ``action`` drives it; an unknown action that still
    has ``tripped=True`` maps to tripped, else to error.
    """
    explicit = verdict.get("state")
    if isinstance(explicit, int):
        return explicit
    action = str(verdict.get("action") or "").strip()
    if action in _STATE_BY_ACTION:
        return _STATE_BY_ACTION[action]
    return STATE_TRIPPED if verdict.get("tripped") else STATE_ERROR


def record_evaluation(verdict: dict[str, Any], *,
                      mode: str,
                      verdict_label: str,
                      path: Optional[str] = None) -> None:
    """Rewrite the heartbeat state file after one ``evaluate`` tick.

    Best-effort: any OSError / parse error is swallowed (debug-logged) so the
    trading loop is never affected by observability I/O.

    Cumulative per-(mode, verdict) counts are read-modify-written here by the
    single loop writer. A corrupt prior file resets counts to zero rather than
    dropping the heartbeat.
    """
    target = path or STATE_FILE
    try:
        now = time.time()
        norm_mode = str(mode or "off").lower()
        counts: dict[str, dict[str, int]] = {}
        try:
            with open(target, "r", encoding="utf-8") as f:
                prev = json.load(f)
            if isinstance(prev, dict) and int(prev.get("version", 0)) <= _STATE_VERSION:
                raw_counts = prev.get("counts")
                if isinstance(raw_counts, dict):
                    for m, bucket in raw_counts.items():
                        if isinstance(m, str) and isinstance(bucket, dict):
                            counts[m] = {
                                str(v): int(n)
                                for v, n in bucket.items()
                                if isinstance(n, (int, float))
                            }
        except (FileNotFoundError, ValueError, OSError, TypeError):
            pass

        mode_bucket = counts.setdefault(norm_mode, {})
        mode_bucket[verdict_label] = int(mode_bucket.get(verdict_label, 0)) + 1

        payload = {
            "version": _STATE_VERSION,
            "ts": now,
            "mode": norm_mode,
            "action": str(verdict.get("action") or ""),
            "data_ok": bool(verdict.get("data_ok", False)),
            "tripped": bool(verdict.get("tripped", False)),
            "state": _state_for(verdict),
            "counts": counts,
        }
        # Cheap, fully regenerable once-per-tick cache: atomic rename (no torn
        # reads) without fsync — same contract as positions_snapshot.
        write_json_atomic(target, payload, indent=None, fsync=False)
    except Exception as e:  # never perturb the trading loop
        logger.debug("[market_circuit_state] heartbeat write failed: %s", e)


def read_state(path: Optional[str] = None) -> Optional[dict[str, Any]]:
    """Return the latest heartbeat payload, or ``None`` if unavailable.

    Never raises: missing file, corrupt JSON, non-dict content, or an
    unsupported future version all degrade to ``None`` so the metrics endpoint
    and dashboard can apply their own absence/staleness handling.
    """
    target = path or STATE_FILE
    try:
        with open(target, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return None
        if int(data.get("version", 0)) > _STATE_VERSION:
            return None
        return data
    except (FileNotFoundError, ValueError, OSError, TypeError):
        return None
