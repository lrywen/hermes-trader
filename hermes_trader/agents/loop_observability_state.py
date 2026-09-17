"""Cross-process loop-observability heartbeat (P0-2, 2026-09-18).

Why this exists
---------------
The remaining P0-2 failure-mode gauges (``feed_gap_fraction``,
``feed_trustworthy`` and ``ai_brain_ready``) are produced inside the
**trading-loop process** — candle-quality gaps are detected in
``client/hl_client.py`` and the LLM circuit lives in ``agents/research.py``.
Prometheus scrapes the separate **web process** (``/metrics``), whose
registry and memory cannot see those signals. This module is the
single-writer half of the channel, mirroring ``market_circuit_state``:

* loop-side producers call the in-process :func:`note_cold_fetch` /
  :func:`note_llm_success` / :func:`note_llm_failure` accumulators (no I/O);
* the loop flushes one small whole-file payload per tick via
  :func:`flush` (atomic rename, best-effort, never raises);
* the web process only reads it via :func:`read_state`.

Only ``/data`` is shared across compose (two processes, one container) and
k8s (two containers) topologies, so the default path lives there; override
with ``HERMES_LOOP_OBSERVABILITY_STATE_FILE``.

Contract
--------
* Every public function is best-effort and NEVER raises into the trading
  path — observability I/O must not perturb a trade.
* The feed-gap denominator is a bounded rolling window of cold fetches so
  a historical outage ages out instead of pinning the gauge.
* ``flush`` is the only writer; there is exactly one loop process, so no
  flock is required (``os.replace`` gives the reader old-or-new, never a
  torn file).
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections import deque
from typing import Any, Optional

from hermes_trader.agents.atomic_io import write_json_atomic

logger = logging.getLogger(__name__)

STATE_FILE = os.environ.get(
    "HERMES_LOOP_OBSERVABILITY_STATE_FILE", "/data/.loop-observability.state"
)
_STATE_VERSION = 1

# Bounded rolling window of cold candleSnapshot fetches: True when the
# fetch's quality report carried a "gaps" issue. Sized to cover roughly one
# full 18-coin scan so the fraction reflects current feed health.
_DEFAULT_WINDOW = 256

_lock = threading.Lock()
_gap_window: deque[bool] = deque(maxlen=_DEFAULT_WINDOW)
_llm_last_success_ts: float = 0.0
_llm_circuit_open: bool = False


def reset() -> None:
    """Clear the in-process accumulators (tests / process restart)."""
    global _llm_last_success_ts, _llm_circuit_open
    with _lock:
        _gap_window.clear()
        _llm_last_success_ts = 0.0
        _llm_circuit_open = False


def set_window(size: int) -> None:
    """Resize the rolling gap window, preserving the most recent samples."""
    global _gap_window
    size = max(1, int(size))
    with _lock:
        kept = list(_gap_window)[-size:]
        _gap_window = deque(kept, maxlen=size)


def note_cold_fetch(issues: Optional[list | tuple] = None) -> None:
    """Record one cold candleSnapshot fetch in the rolling gap window.

    ``issues`` is the quality gate's bounded issue list; a fetch counts as a
    gap only when it literally contains ``"gaps"``. Never raises.
    """
    try:
        had_gap = bool(issues) and "gaps" in issues
        with _lock:
            _gap_window.append(bool(had_gap))
    except Exception as e:  # never perturb the fetch path
        logger.debug("[loop_observability] note_cold_fetch failed: %s", e)


def note_llm_success(ts: Optional[float] = None) -> None:
    """Record a usable LLM response and clear the open-circuit flag."""
    global _llm_last_success_ts, _llm_circuit_open
    try:
        with _lock:
            _llm_last_success_ts = float(ts) if ts else time.time()
            _llm_circuit_open = False
    except Exception as e:
        logger.debug("[loop_observability] note_llm_success failed: %s", e)


def note_llm_failure(ts: Optional[float] = None, *,
                     circuit_open: bool = False) -> None:
    """Record an LLM failure; ``circuit_open`` marks the breaker tripped."""
    global _llm_circuit_open
    try:
        with _lock:
            if circuit_open:
                _llm_circuit_open = True
    except Exception as e:
        logger.debug("[loop_observability] note_llm_failure failed: %s", e)


def _key_configured() -> bool:
    return bool(os.environ.get("OPENROUTER_API_KEY", "").strip())


def flush(feed_trustworthy: bool, *, path: Optional[str] = None) -> None:
    """Atomically rewrite the observability state file for this loop tick.

    ``feed_trustworthy`` is the tick's market-data verdict (the
    market_circuit ``data_ok`` flag); it is combined with the rolling gap
    window. Never raises.
    """
    target = path or STATE_FILE
    try:
        with _lock:
            total = len(_gap_window)
            gaps = sum(1 for g in _gap_window if g)
            payload = {
                "version": _STATE_VERSION,
                "ts": time.time(),
                # Feed fields are flat for direct gauge mapping; ai_brain is a
                # small nested block (its ready verdict combines the leaves).
                "cold_fetches": total,
                "cold_with_gap": gaps,
                "feed_gap_fraction": (gaps / total) if total else 0.0,
                "feed_trustworthy": bool(feed_trustworthy) and gaps == 0,
                "ai_brain": {
                    "key_configured": _key_configured(),
                    "circuit_open": bool(_llm_circuit_open),
                    "last_success_ts": float(_llm_last_success_ts),
                },
            }
        # Cheap, fully regenerable once-per-tick cache: atomic rename
        # without fsync (same contract as market_circuit_state).
        write_json_atomic(target, payload, indent=None, fsync=False)
    except Exception as e:  # never perturb the trading loop
        logger.debug("[loop_observability] flush write failed: %s", e)


def read_state(path: Optional[str] = None) -> Optional[dict[str, Any]]:
    """Return the latest payload, or ``None`` when unavailable/corrupt.

    Never raises: a missing file, corrupt JSON, non-dict payload or
    unsupported future version all degrade to ``None`` so the metrics layer
    can apply its own explicit sentinel.
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
