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
    "timestamp": "YYYY-MM-DDTHH:MM:SSZ", "payload": {...}}``. Since the
    O-10 hardening it additionally carries a tamper-evident hash-chain
    triple: ``{"seq": int, "prev_hash": str, "hash": str}`` where each
    ``hash`` is a SHA-256 over the record body and ``prev_hash`` links to
    the predecessor's ``hash`` (see :func:`verify_chain`). Records written
    before this field existed are tolerated as legacy entries.
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

import hashlib
import json
import logging
import os
import shutil
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

EVENTS_FILE = os.environ.get(
    "HERMES_EVENTS_FILE",
    os.path.expanduser("~/.hermes-trading/events.jsonl"),
)
_LOCK = threading.Lock()

# Audit 2026-09-03 P2-5: events.jsonl is written from TWO processes — the
# trading loop (direct appends) and the web/dashboard process (operator
# events via session_log.fork_from_session). The threading.Lock above only
# serialises threads within one process; a cross-process flock on a sidecar
# makes rotate+append+anchor-advance atomic across both writers so they can't
# interleave a rotation with an append. Best-effort: if the sidecar cannot be
# opened (read-only volume / exotic FS) we fall back to the in-process lock.
import contextlib

try:
    import fcntl  # type: ignore  # POSIX only
    _HAVE_FCNTL = True
except ImportError:  # pragma: no cover - Windows/dev
    fcntl = None  # type: ignore
    _HAVE_FCNTL = False

_LOCK_FD: Optional[Any] = None
_LOCK_FD_LOCK = threading.Lock()


def _lock_fd() -> Optional[Any]:
    """Lazily open (and keep open) the flock sidecar file descriptor."""
    global _LOCK_FD
    if not _HAVE_FCNTL:
        return None
    if _LOCK_FD is None:
        with _LOCK_FD_LOCK:
            if _LOCK_FD is None:
                try:
                    parent = os.path.dirname(EVENTS_FILE)
                    if parent:
                        os.makedirs(parent, exist_ok=True)
                    _LOCK_FD = open(f"{EVENTS_FILE}.lock", "a+")
                except OSError as e:
                    logger.warning(f"[event_log] flock sidecar open failed: {e}")
                    return None
    return _LOCK_FD


@contextlib.contextmanager
def _cross_process_lock():
    """Hold both the in-process lock and an exclusive cross-process flock."""
    with _LOCK:
        fd = _lock_fd()
        if fd is not None:
            try:
                fcntl.flock(fd.fileno(), fcntl.LOCK_EX)
            except OSError as e:  # pragma: no cover - defensive
                logger.warning(f"[event_log] flock acquire failed: {e}")
            try:
                yield
            finally:
                try:
                    fcntl.flock(fd.fileno(), fcntl.LOCK_UN)
                except OSError:  # pragma: no cover - defensive
                    pass
        else:
            yield


def _events_path() -> Path:
    """Active log path, resolved at call time.

    Read as a runtime function (not an import-time constant) so tests that
    monkeypatch ``EVENTS_FILE`` (and deployments overriding the env var)
    redirect rotation, chaining and the anchor sidecar consistently.
    """
    return Path(EVENTS_FILE)


def _anchor_path() -> Path:
    """Chain-anchor sidecar path, derived from the active log path."""
    return Path(f"{EVENTS_FILE}.chain")

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

# ── O-10 (supplemental audit 2026-08-30): tamper-evident hash chain ──────
# Every chained record carries {"seq", "prev_hash", "hash"} where
#   hash = SHA256( canonical_json({event, trace_id, timestamp, payload, seq,
#                                  prev_hash}) )
# The hash binds the record's content to its predecessor's hash, so deleting
# or rewriting any line breaks the link and is detected by verify_chain().
# The head of the chain (last seq/hash of the active file) is persisted in a
# small sidecar so a freshly-rotated events.jsonl can anchor to the tail of
# the previous generation (rotation moves the whole file away, which would
# otherwise reset the chain). Records written before this feature shipped
# have no hash fields; verification treats them as legacy and only validates
# the contiguous chained runs.
_GENESIS_HASH = ""


def _canonical_body(rec: dict[str, Any]) -> bytes:
    """Deterministic byte serialization of the fields a chain hash covers."""
    return json.dumps(
        {
            "event": rec.get("event"),
            "trace_id": rec.get("trace_id"),
            "timestamp": rec.get("timestamp"),
            "payload": rec.get("payload"),
            "seq": rec.get("seq"),
            "prev_hash": rec.get("prev_hash"),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _record_hash(rec: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical_body(rec)).hexdigest()


def _tail_of_file(path: Path) -> Optional[dict[str, Any]]:
    """Return the last valid chained record in *path* (None if no chained tail)."""
    tail: Optional[dict[str, Any]] = None
    try:
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(rec, dict) and rec.get("hash") and rec.get("seq") is not None:
                    tail = rec
    except OSError:
        return tail
    return tail


def _chain_tail() -> tuple[int, str]:
    """Resolve the current chain head as (seq, prev_hash) for the next append.

    Caller must hold _LOCK. Prefers the actual tail of the active file; when
    the file was just rotated away (empty/missing), falls back to the anchor
    sidecar written at rotation time.
    """
    tail = _tail_of_file(_events_path())
    if tail is not None:
        return int(tail.get("seq", 0)), str(tail.get("hash", ""))
    try:
        anchor = _anchor_path()
        if anchor.exists():
            data = json.loads(anchor.read_text(encoding="utf-8"))
            return int(data.get("seq", 0)), str(data.get("hash", ""))
    except (OSError, ValueError, TypeError) as e:
        logger.warning(f"[event_log] chain anchor unreadable, starting genesis: {e}")
    return 0, _GENESIS_HASH


def _write_anchor(seq: int, tail_hash: str) -> None:
    """Durably persist the chain head so the next generation can anchor to it."""
    anchor = _anchor_path()
    tmp = Path(f"{anchor}.tmp")
    tmp.write_text(
        json.dumps({"seq": seq, "hash": tail_hash}),
        encoding="utf-8",
    )
    os.replace(str(tmp), str(anchor))


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _rotate_if_needed() -> None:
    """Rotate events.jsonl when it exceeds the size cap (caller holds _LOCK)."""
    events_path = _events_path()
    if not events_path.exists():
        return
    try:
        if events_path.stat().st_size < _MAX_EVENTS_BYTES:
            return
        # O-10: anchor the hash chain to the tail of the about-to-rotate file
        # so the fresh generation's first record links to the previous one
        # instead of restarting at genesis (cross-generation chaining).
        old_tail = _tail_of_file(events_path)
        if old_tail is not None:
            try:
                _write_anchor(int(old_tail.get("seq", 0)), str(old_tail.get("hash", "")))
            except OSError as e:
                logger.warning(f"[event_log] chain anchor write on rotation failed: {e}")
        for i in range(_MAX_BACKUPS - 1, 0, -1):
            src = Path(f"{events_path}.{i}")
            dst = Path(f"{events_path}.{i + 1}")
            if src.exists():
                dst.unlink(missing_ok=True)
                shutil.move(str(src), str(dst))
        backup = Path(f"{events_path}.1")
        backup.unlink(missing_ok=True)
        shutil.move(str(events_path), str(backup))
    except OSError as e:
        logger.warning(f"[event_log] rotation failed: {e}")


def append(event: str, payload: Optional[dict[str, Any]] = None,
           trace_id: str = "", timestamp: Optional[str] = None) -> bool:
    """Append one event to events.jsonl.

    Every record is guaranteed to carry a non-empty ISO-8601 ``timestamp``.
    If the caller omits one, the current UTC instant is used.

    O-10 (supplemental audit 2026-08-30): each record is also linked into a
    tamper-evident SHA-256 chain via ``seq`` / ``prev_hash`` / ``hash``
    fields (see module header). Chaining is best-effort like the write
    itself — if the chain head cannot be read the record still lands and
    starts a fresh genesis run rather than blocking trading.

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
        with _cross_process_lock():
            # Ensure parent dir exists (first run on a fresh volume).
            parent = os.path.dirname(EVENTS_FILE)
            if parent:
                os.makedirs(parent, exist_ok=True)
            _rotate_if_needed()
            # Link this record to the current chain head (active file tail,
            # or the cross-generation anchor right after a rotation).
            prev_seq, prev_hash = _chain_tail()
            rec["seq"] = prev_seq + 1
            rec["prev_hash"] = prev_hash
            rec["hash"] = _record_hash(rec)
            with open(EVENTS_FILE, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                f.flush()
            # Refresh the persisted chain head so a crash/rotation before the
            # next append does not strand the chain. Best-effort: a failure
            # here does not invalidate the just-written line.
            try:
                _write_anchor(rec["seq"], rec["hash"])
            except OSError as e:
                logger.warning(f"[event_log] chain anchor update failed: {e}")
        return True
    except OSError as e:
        logger.warning(f"[event_log] append {event} failed: {e}")
        return False


def fork_from_session(record: dict[str, Any]) -> bool:
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


# ── O-10: chain integrity verification ──────────────────────────────────

def verify_chain(path: Optional[str] = None) -> dict[str, Any]:
    """Verify the tamper-evident hash chain across the active log and backups.

    Scans the retained generations oldest-first (``.3`` .. ``.1`` then the
    active file) and replays ONE continuous chain: rotation only splits the
    feed across files, so the head of an older generation is the predecessor
    of the first record of the next-newer generation. For each chained record
    it recomputes the SHA-256 hash and checks the ``seq``/``prev_hash`` link.

    Root policy: the first chained record seen is accepted as a chain root —
    it is either the genuine genesis (``prev_hash == ""``) or the head of a
    generation whose predecessor was already dropped by retention. A legacy
    (pre-chain) record likewise resets the root, since chained records after
    an upgrade start a fresh run. Records written before the hash-chain
    feature shipped carry no ``hash``/``seq`` and are counted as
    ``legacy_records`` (skipped, not errors). Every other break is reported.

    Returns a result dict::

        {"ok": bool, "chained_records": int, "legacy_records": int,
         "corrupt_lines": int, "errors": [ {"file", "line", "reason", ...} ],
         "last_seq": int}

    ``ok`` is True only when no chain error was found (legacy records and an
    empty log are both fine). Best-effort and read-only; never raises.
    """
    active = Path(path or EVENTS_FILE)
    # Oldest generation first so seq/prev_hash are replayed in write order.
    targets = [Path(f"{active}.{i}") for i in range(_MAX_BACKUPS, 0, -1)]
    targets.append(active)

    # None until the first chained record establishes a chain root.
    expected_seq: Optional[int] = None
    expected_prev: Optional[str] = None
    chained = 0
    legacy = 0
    corrupt_lines = 0
    errors: list[dict[str, Any]] = []

    for fp in targets:
        if not fp.exists():
            continue
        try:
            with fp.open("r", encoding="utf-8") as f:
                for lineno, line in enumerate(f, start=1):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        corrupt_lines += 1
                        errors.append({
                            "file": str(fp), "line": lineno,
                            "reason": "unparseable_json",
                        })
                        continue
                    if not isinstance(rec, dict) or not rec.get("hash"):
                        # Pre-chain (legacy) record: tolerated. Chained records
                        # written after it form a fresh genesis-anchored run.
                        if isinstance(rec, dict) and "event" in rec:
                            legacy += 1
                            expected_seq = None
                            expected_prev = None
                        continue
                    chained += 1
                    seq = rec.get("seq")
                    recomputed = _record_hash(rec)
                    if recomputed != rec.get("hash"):
                        errors.append({
                            "file": str(fp), "line": lineno,
                            "reason": "hash_mismatch", "seq": seq,
                            "expected_hash": recomputed,
                            "stored_hash": rec.get("hash"),
                        })
                        # Re-anchor to the stored record so subsequent links
                        # can still be evaluated independently.
                        expected_seq = int(seq) if isinstance(seq, int) else None
                        expected_prev = str(rec.get("hash", ""))
                        continue
                    is_root = expected_seq is None or expected_prev is None
                    if not is_root:
                        if not isinstance(seq, int) or seq != expected_seq + 1:
                            errors.append({
                                "file": str(fp), "line": lineno,
                                "reason": "seq_gap", "seq": seq,
                                "expected_seq": (expected_seq or 0) + 1,
                            })
                        if rec.get("prev_hash") != expected_prev:
                            errors.append({
                                "file": str(fp), "line": lineno,
                                "reason": "prev_hash_mismatch", "seq": seq,
                                "expected_prev": expected_prev,
                                "stored_prev": rec.get("prev_hash"),
                            })
                    expected_seq = int(seq) if isinstance(seq, int) else expected_seq
                    expected_prev = str(rec.get("hash", ""))
        except OSError as e:
            errors.append({"file": str(fp), "line": 0, "reason": f"read_error: {e}"})

    return {
        "ok": not errors,
        "chained_records": chained,
        "legacy_records": legacy,
        "corrupt_lines": corrupt_lines,
        "errors": errors,
        "last_seq": expected_seq or 0,
    }
