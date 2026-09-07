"""Shared append + size-based rotation for shadow / audit JSONL files.

Audit 2026-09-06 (F2, engineering hygiene): the codebase grew 8+ near-identical
"best-effort append a shadow verdict" helpers (market_circuit, regime_overlay,
late-entry/trend/extension/reentry gates in risk_gates, age-decay in
perception, pullback/atr-calib/sizing-v2/confidence-decay in executor). Each
opened the file in append mode forever — no rotation, so a long-running bot
grows unbounded JSONL files — and each swallowed OSError with only a warning
log, so a persistently unwritable directory silently lost audit data with no
metric.

This module is the single writer for those streams:

  * append_jsonl(path, rec, stream=...) — one JSON line, best-effort, NEVER
    raises (shadow logging must not break the trade hot path);
  * size-based rotation — once the active file exceeds MAX_BYTES it is renamed
    to ``<path>.1`` (previous ``.1`` → ``.2`` … up to BACKUP_COUNT, older
    dropped) and a fresh active file is started. Rotation happens inline just
    before the append that would cross the threshold; it is cheap (a single
    rename chain, no compression) and guarded by a process-local lock;
  * daily rotation — Audit 2026-09-06 (F2): before the first append on a new
    local day, an active file still holding yesterday's records is rotated
    away through the same ``.1``/``.2`` chain, so each active file only ever
    contains a single day's verdicts (older days age out via BACKUP_COUNT
    alongside the size-based siblings);
  * every failure (makedirs / rotation / write) increments
    metrics.SHADOW_LOG_WRITE_ERRORS labelled by stream, in addition to a
    warning log.

Thresholds are intentionally conservative for small audit records
(~200-400 bytes/line): 10 MiB ≈ tens of thousands of verdicts per file.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Rotate once the active shadow file grows past this size (bytes).
MAX_BYTES = 10 * 1024 * 1024
# Keep at most this many rotated siblings (file.1 .. file.BACKUP_COUNT).
BACKUP_COUNT = 5

# Process-local serialization for rotation+append on the same path. Shadow
# writers are low-frequency (a few records per scan), so a single global lock
# is ample and keeps the rotation chain race-free within this process.
_WRITE_LOCK = threading.Lock()

# Tests (and any caller that wants to point rotation elsewhere) can toggle
# rotation off entirely without monkeypatching internals.
_rotation_enabled = True

# Audit 2026-09-06 (F2): local day (YYYY-MM-DD) of the records held by each
# active shadow path, used to skip the daily-rotation stat() once we already
# know the active file holds today's records. It is only a fast-path cache: the
# source of truth across process restarts is the active file's own mtime (see
# append_jsonl), so a file left over from a previous day is still rotated on
# the first append after restart. Keyed by absolute path per stream.
_active_day: dict[str, str] = {}


def disable_rotation() -> None:
    """Turn rotation off (test helper)."""
    global _rotation_enabled
    _rotation_enabled = False


def _local_day(now: Optional[float] = None) -> str:
    """Return the current local day as 'YYYY-MM-DD' (used for daily rotation)."""
    return time.strftime("%Y-%m-%d", time.localtime(now if now is not None else time.time()))


def _rotate_locked(path: str) -> None:
    """Rename chain path.(N-1) -> path.N, path -> path.1. Caller holds lock."""
    oldest = f"{path}.{BACKUP_COUNT}"
    if os.path.exists(oldest):
        os.remove(oldest)
    for i in range(BACKUP_COUNT - 1, 0, -1):
        src = f"{path}.{i}"
        dst = f"{path}.{i + 1}"
        if os.path.exists(src):
            os.replace(src, dst)
    os.replace(path, f"{path}.1")


def append_jsonl(path: str, record: dict[str, Any], *, stream: str = "shadow") -> bool:
    """Best-effort append ``record`` as one JSON line to ``path``.

    Creates the parent directory if needed, rotates the file when it exceeds
    MAX_BYTES, and swallows every error (logging + metrics). Returns True when
    the line was written, False otherwise. Never raises.
    """
    if not path:
        return False
    try:
        line = json.dumps(record, ensure_ascii=False) + "\n"
    except (TypeError, ValueError) as e:
        # A non-serialisable record is a caller bug; count it but never raise.
        logger.warning("[shadow_log:%s] record not JSON-serialisable: %s", stream, e)
        _count_error(stream)
        return False

    try:
        with _WRITE_LOCK:
            parent = os.path.dirname(path)
            if parent:
                os.makedirs(parent, exist_ok=True)
            key = os.path.abspath(path)
            if _rotation_enabled:
                try:
                    today = _local_day()
                    # Audit 2026-09-06 (F2): daily rotation — if the active
                    # file already holds an earlier day's records, roll it
                    # through the same .1/.2 chain before today's first append.
                    # The file's mtime is the source of truth (survives
                    # restarts); _active_day only skips the stat once we know
                    # the active file is today's.
                    file_day = _active_day.get(key)
                    if file_day is None and os.path.exists(path):
                        file_day = _local_day(os.path.getmtime(path))
                    if file_day is not None and file_day < today:
                        _rotate_locked(path)
                        _active_day.pop(key, None)
                        file_day = None
                    if os.path.exists(path) and os.path.getsize(path) >= MAX_BYTES:
                        _rotate_locked(path)
                        _active_day.pop(key, None)
                        file_day = None
                except OSError as e:
                    # Rotation failure must not block the append — the file may
                    # still be writable; we just keep appending to the big file.
                    logger.warning("[shadow_log:%s] rotate failed: %s", stream, e)
                    _count_error(stream)
            with open(path, "a", encoding="utf-8") as f:
                f.write(line)
            _active_day[key] = _local_day()
        return True
    except OSError as e:
        logger.warning("[shadow_log:%s] append failed to %s: %s", stream, path, e)
        _count_error(stream)
        return False


def _count_error(stream: str) -> None:
    try:
        from hermes_trader import metrics

        metrics.SHADOW_LOG_WRITE_ERRORS.labels(stream=stream).inc()
    except Exception:
        # Metrics are themselves best-effort.
        pass
