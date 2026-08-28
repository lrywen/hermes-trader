"""Authoritative append-only event log (events.jsonl).

This is the single writer for the ``{event, trace_id, timestamp, payload}``
JSONL feed that :class:`hermes_trader.agents.memory.AgentMemory` replays on
startup to rebuild its trade/close history. Before this module existed,
``order``/``close`` events never reached events.jsonl — the trading loop
wrote the high-volume operational heartbeat to ``session-log.jsonl`` while
the memory replayer read ``events.jsonl``. The two files had diverged, so a
container/volume recovery could never reconstruct realized outcomes from the
event log (the PURR record-loss incident on 2026-08-22).

Contract:
  * Every line is one JSON object: ``{"event": str, "trace_id": str,
    "timestamp": "YYYY-MM-DDTHH:MM:SSZ", "payload": {...}}``.
  * Writes are append-only and best-effort — a disk failure must never
    interrupt trading (mirrors ``session_log.append``).
  * The path is overridable via ``HERMES_EVENTS_FILE`` (same env var memory
    reads), so tests and deployments can redirect the feed consistently.

``session-log.jsonl`` remains the high-volume operational heartbeat
(scan/research/heartbeat/tick events). Event types that matter for
post-trade reconstruction or cross-component trace correlation are
forked here via :func:`fork_from_session`.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

EVENTS_FILE = os.environ.get(
    "HERMES_EVENTS_FILE",
    os.path.expanduser("~/.hermes-trading/events.jsonl"),
)
_EVENTS_PATH = Path(EVENTS_FILE)
_LOCK = threading.Lock()

# Rotate events.jsonl when it exceeds this size (default 50 MB, matches the
# external shared event_log module). Keeps 3 backup generations.
_MAX_EVENTS_MB = int(os.environ.get("HERMES_EVENTS_MAX_MB", "50"))
_MAX_BACKUPS = int(os.environ.get("HERMES_EVENTS_BACKUPS", "3"))
_MAX_EVENTS_BYTES = _MAX_EVENTS_MB * 1024 * 1024

# Session-log event types that should ALSO be forked into events.jsonl so a
# signal→order→close trace can be followed end-to-end. High-churn events
# (scan/heartbeat/ta_skip/research) are intentionally excluded — they belong
# in session-log.jsonl, not the authoritative outcome feed.
_FORKABLE_EVENTS = frozenset({
    "execute",
    "dsl_exit",
    "ai_close",
    "external_close_recorded",
    "external_close_unattributed",
    "hard_killswitch",
    "risk",
    "risk_gate",
    # F22: operator audit trail must also survive in the authoritative feed so
    # a mode switch / config change / manual close can be traced after a
    # restart. These are low-churn (human-driven) events; memory replay ignores
    # unknown types.
    "mode_switch",
    "config_update",
    "operator_action",
})


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _rotate_if_needed() -> None:
    """Rotate events.jsonl when it exceeds the size cap (caller holds _LOCK)."""
    if not _EVENTS_PATH.exists():
        return
    try:
        if _EVENTS_PATH.stat().st_size < _MAX_EVENTS_BYTES:
            return
        for i in range(_MAX_BACKUPS - 1, 0, -1):
            src = Path(f"{_EVENTS_PATH}.{i}")
            dst = Path(f"{_EVENTS_PATH}.{i + 1}")
            if src.exists():
                dst.unlink(missing_ok=True)
                shutil.move(str(src), str(dst))
        backup = Path(f"{_EVENTS_PATH}.1")
        backup.unlink(missing_ok=True)
        shutil.move(str(_EVENTS_PATH), str(backup))
    except OSError as e:
        logger.warning(f"[event_log] rotation failed: {e}")


def append(event: str, payload: Optional[Dict[str, Any]] = None,
           trace_id: str = "", timestamp: Optional[str] = None) -> bool:
    """Append one event to events.jsonl.

    Every record is guaranteed to carry a non-empty ISO-8601 ``timestamp``.
    If the caller omits one, the current UTC instant is used.

    Returns True if the line was durably written. Best-effort: any I/O error
    is logged and swallowed so the trading loop is never blocked by audit
    storage.
    """
    rec = {
        "event": event,
        "trace_id": trace_id or "",
        # Defensive: never let an explicit empty/None timestamp slip through.
        "timestamp": (timestamp or _now_iso()),
        "payload": dict(payload or {}),
    }
    try:
        with _LOCK:
            # Ensure parent dir exists (first run on a fresh volume).
            parent = os.path.dirname(EVENTS_FILE)
            if parent:
                os.makedirs(parent, exist_ok=True)
            _rotate_if_needed()
            with open(EVENTS_FILE, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                f.flush()
        return True
    except OSError as e:
        logger.warning(f"[event_log] append {event} failed: {e}")
        return False


def fork_from_session(record: Dict[str, Any]) -> bool:
    """Fork a session-log record into events.jsonl when it is outcome-relevant.

    Called by :func:`hermes_trader.session_log.append` after the session log
    is durably written. Only a curated whitelist of event types is forked
    (execute/exit/risk) — the operational heartbeat stays in session-log.
    The record is projected onto the canonical ``{event, trace_id,
    timestamp, payload}`` schema. Returns True if forked.
    """
    ev = record.get("event")
    if ev not in _FORKABLE_EVENTS:
        return False
    # Session-log records use millisecond "ts"; events.jsonl uses ISO-8601
    # "timestamp". Convert when present so downstream consumers see the same
    # instant the heartbeat logged.
    ts = record.get("ts")
    iso_ts: Optional[str] = None
    if isinstance(ts, (int, float)):
        try:
            iso_ts = datetime.fromtimestamp(
                ts / 1000.0, tz=timezone.utc
            ).strftime("%Y-%m-%dT%H:%M:%SZ")
        except (OverflowError, OSError, ValueError):
            iso_ts = None
    payload = {k: v for k, v in record.items()
               if k not in ("ts", "timestamp", "event", "trace_id")}
    return append(
        event=str(ev),
        payload=payload,
        trace_id=str(record.get("trace_id", "") or ""),
        timestamp=iso_ts,
    )


# ── Time-window query helper ─────────────────────────────────────────────

def parse_iso(ts: str) -> Optional[datetime]:
    """Parse an ISO-8601 timestamp (with or without trailing Z) to aware UTC."""
    if not ts or not isinstance(ts, str):
        return None
    try:
        raw = ts[:-1] + "+00:00" if ts.endswith("Z") else ts
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except (ValueError, TypeError):
        return None


def query_events(
    *,
    event_type: Optional[str] = None,
    start: Optional[str] = None,
    end: Optional[str] = None,
    path: Optional[str] = None,
) -> list:
    """Return events from events.jsonl filtered by type and ISO time window.

    ``start`` / ``end`` are inclusive ISO-8601 strings (e.g.
    ``"2026-08-20T00:00:00Z"``). Rotated backup files (``.1`` .. ``.3``) are
    also scanned so queries span the full retained history. Each returned
    dict is augmented with a parsed ``_dt`` field (timezone-aware UTC).
    """
    targets = [Path(path or EVENTS_FILE)]
    for i in range(1, _MAX_BACKUPS + 1):
        targets.append(Path(f"{targets[0]}.{i}"))

    start_dt = parse_iso(start) if start else None
    end_dt = parse_iso(end) if end else None

    out: list = []
    for fp in targets:
        if not fp.exists():
            continue
        try:
            with fp.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if event_type and rec.get("event") != event_type:
                        continue
                    dt = parse_iso(rec.get("timestamp", ""))
                    if dt is None:
                        continue
                    if start_dt and dt < start_dt:
                        continue
                    if end_dt and dt > end_dt:
                        continue
                    rec["_dt"] = dt
                    out.append(rec)
        except OSError as e:
            logger.warning(f"[event_log] query read {fp} failed: {e}")
    out.sort(key=lambda r: r["_dt"])
    return out
