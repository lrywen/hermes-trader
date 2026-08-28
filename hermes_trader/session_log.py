"""Append-only JSONL activity log — the trading system's visible heartbeat.

The trading loop and the FastAPI server append events here; `status.py` and the
hourly cron report read them back. One line per event, each tagged with a `ts`
(epoch ms). Path is overridable via the `SESSION_LOG_PATH` env var.

R11-G1: rotation + gzip + retention.

The active log is `SESSION_LOG_FILE`. When it crosses any of the configured
thresholds (line count, byte size, age) the active file is closed, gzipped to
`SESSION_LOG_FILE.<ts>.gz`, and a fresh empty active file is created. Only
the most recent ``SESSION_LOG_KEEP`` rotated files are kept on disk; older
ones are removed. All of this happens behind a process-level
``threading.Lock`` and a cross-process ``fcntl.flock`` on a sidecar
``SESSION_LOG_LOCK_FILE`` so two trading-loop processes cannot race the
rename. The lock is always released in ``finally``.

Tunables (all env-var-overridable, sane defaults):

* ``SESSION_LOG_MAX_LINES``   — default 100_000
* ``SESSION_LOG_MAX_BYTES``   — default 50 * 1024 * 1024 (50 MB)
* ``SESSION_LOG_MAX_AGE_S``   — default 86_400 (24 h)
* ``SESSION_LOG_KEEP``        — default 5 rotated gz files retained
* ``SESSION_LOG_ROTATE_DISABLED=1`` — opt out entirely (for tests / DR)
"""
from __future__ import annotations

import fcntl
import glob
import gzip
import json
import os
import threading
import time
from typing import Any, Dict, List, Optional

SESSION_LOG_FILE = os.environ.get(
    "SESSION_LOG_PATH",
    os.path.expanduser("~/.hermes-trader-session-log.jsonl"),
)

# R11-G1: rotation tunables. Read once at import; tests that need to
# poke values use monkeypatch on the module attribute. Re-reading the
# env on every append would make "set a value and forget" impossible
# (a misbehaving operator could rotate the log by changing the env
# mid-flight). If you genuinely need a runtime override, write to the
# module attribute directly.
_SESSION_LOG_MAX_LINES = int(os.environ.get("SESSION_LOG_MAX_LINES", "100000"))
_SESSION_LOG_MAX_BYTES = int(
    os.environ.get("SESSION_LOG_MAX_BYTES", str(50 * 1024 * 1024))
)
_SESSION_LOG_MAX_AGE_S = float(os.environ.get("SESSION_LOG_MAX_AGE_S", "86400"))
_SESSION_LOG_KEEP = int(os.environ.get("SESSION_LOG_KEEP", "5"))
_SESSION_LOG_ROTATE_DISABLED = bool(
    os.environ.get("SESSION_LOG_ROTATE_DISABLED") == "1"
)

# R11-G1: cross-process lock sidecar. Same pattern as memory.py — the
# lock lives next to the data file so two processes (e.g. the trading
# loop + the dashboard reader that may also call append) cannot race
# the rotate. The fd is opened lazily on first rotate attempt and
# cached for the process lifetime. The lock path is computed from
# ``SESSION_LOG_FILE`` each time (not frozen at import) so tests
# that swap the file path via monkeypatch pick up the right sidecar.
_SESSION_LOG_FLOCK_FD: Optional[int] = None


def _lock_file_path() -> str:
    return SESSION_LOG_FILE + ".lock"

# R11-G1: in-process lock around the rotate critical section. The
# cross-process flock is the real safety net; this is just a
# thread-safety guarantee so a single process can never enter the
# rotate path twice concurrently (e.g. if the trading loop is calling
# append from two threads).
_ROTATE_LOCK = threading.Lock()


def _open_lock_fd() -> int:
    """Open (or reuse) the cross-process flock sidecar."""
    global _SESSION_LOG_FLOCK_FD
    if _SESSION_LOG_FLOCK_FD is None:
        _SESSION_LOG_FLOCK_FD = os.open(
            _lock_file_path(),
            os.O_CREAT | os.O_RDWR,
            0o644,
        )
    return _SESSION_LOG_FLOCK_FD


def _should_rotate(line_count: int, byte_count: int, mtime_s: float) -> bool:
    """Decide whether the active log has crossed any rotate threshold."""
    if _SESSION_LOG_ROTATE_DISABLED:
        return False
    if _SESSION_LOG_MAX_LINES and line_count >= _SESSION_LOG_MAX_LINES:
        return True
    if _SESSION_LOG_MAX_BYTES and byte_count >= _SESSION_LOG_MAX_BYTES:
        return True
    if _SESSION_LOG_MAX_AGE_S and (time.time() - mtime_s) >= _SESSION_LOG_MAX_AGE_S:
        return True
    return False


def _stat_active() -> Optional[Dict[str, Any]]:
    """Return (line_count, byte_count, mtime) for the active log, or
    None if it doesn't exist. We compute the line count by counting
    newlines so the rotate decision is based on actual record count
    rather than relying on a stale counter (the trading loop may
    write from multiple threads between calls)."""
    try:
        st = os.stat(SESSION_LOG_FILE)
    except FileNotFoundError:
        return None
    line_count = 0
    byte_count = st.st_size
    try:
        with open(SESSION_LOG_FILE, "rb") as f:
            # 64KB buffer is fine for JSONL; we don't need to read the
            # whole file just to count newlines.
            while True:
                chunk = f.read(65536)
                if not chunk:
                    break
                line_count += chunk.count(b"\n")
    except OSError:
        # If the file disappears mid-stat, treat it as "needs rotate"
        # only if a threshold is already crossed by size; otherwise
        # let the next append handle it.
        line_count = 0
    return {
        "line_count": line_count,
        "byte_count": byte_count,
        "mtime_s": st.st_mtime,
    }


def _list_rotated() -> List[str]:
    """Return rotated files for ``SESSION_LOG_FILE``, oldest first by
    embedded timestamp in the filename."""
    pattern = SESSION_LOG_FILE + ".*.gz"
    try:
        files = glob.glob(pattern)
    except OSError:
        return []
    # Filename suffix is "<path>.<unix_ts>.gz" — the unix_ts makes
    # lexical sort match chronological sort.
    files.sort()
    return files


def _enforce_retention() -> int:
    """Delete oldest rotated files so only ``_SESSION_LOG_KEEP`` remain.

    Returns the number of files removed. Safe to call on an empty
    directory — no-op. The list is sorted oldest-first, so we drop
    from the front until the count matches the keep limit.
    """
    if _SESSION_LOG_KEEP <= 0:
        # Keep==0 means "drop everything" — useful for tests.
        removed = 0
        for f in _list_rotated():
            try:
                os.unlink(f)
                removed += 1
            except OSError:
                pass
        return removed
    files = _list_rotated()
    over = len(files) - _SESSION_LOG_KEEP
    if over <= 0:
        return 0
    removed = 0
    for f in files[:over]:
        try:
            os.unlink(f)
            removed += 1
        except OSError:
            pass
    return removed


def rotate() -> Optional[str]:
    """Rotate the active log if any threshold is exceeded.

    The rotation sequence (always done under cross-process flock):

    1. Open + ``LOCK_EX`` the lock sidecar (``SESSION_LOG_FILE.lock``).
    2. Re-stat the active log (a concurrent writer may have grown it
       between our caller's stat and now).
    3. If no threshold is crossed, return None.
    4. Otherwise: gzip the active file to
       ``SESSION_LOG_FILE.<unix_ts>.gz`` (atomic via tmp+rename).
    5. Truncate the active file in place so the next append starts a
       fresh empty log. We do NOT unlink+recreate so any open
       ``O_APPEND`` fd on the original inode (e.g. the trading loop's
       own append handle) keeps appending to the same inode — but
       since the rename moved that inode away, new opens will see the
       freshly-truncated file. Process-local fds that were opened
       before rotate will continue writing to the now-rotated file
       (their append lands in the gzipped copy's source, which is
       fine — the gzipped file is a snapshot, not a live feed).
    6. Enforce the retention cap on older rotated files.

    Returns the path of the new gz file, or None on no-op. The return
    value is intended for ops/tests, not for callers to depend on.

    Errors are swallowed and logged; rotate is best-effort and must
    never propagate. The trading loop's append already swallows disk
    errors, so a failed rotate just means the next append tries
    again next time.
    """
    if _SESSION_LOG_ROTATE_DISABLED:
        return None
    with _ROTATE_LOCK:
        lock_fd = -1
        try:
            lock_fd = _open_lock_fd()
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
        except OSError:
            # Could not even acquire the cross-process lock — log and
            # bail; the trading loop will retry next append.
            return None
        try:
            st = _stat_active()
            if st is None:
                return None
            if not _should_rotate(
                st["line_count"], st["byte_count"], st["mtime_s"]
            ):
                return None
            # Compress the active file to <path>.<ts>.gz.
            ts = int(time.time())
            gz_path = f"{SESSION_LOG_FILE}.{ts}.gz"
            tmp_gz = gz_path + ".tmp"
            try:
                with open(SESSION_LOG_FILE, "rb") as src, \
                        gzip.open(tmp_gz, "wb", compresslevel=6) as dst:
                    # 1 MiB copy buffer — large enough to amortize the
                    # gzip chunk overhead, small enough to keep peak
                    # memory bounded for a 50 MB log.
                    while True:
                        buf = src.read(1024 * 1024)
                        if not buf:
                            break
                        dst.write(buf)
                os.replace(tmp_gz, gz_path)
            except OSError:
                # Clean up the tmp on failure so the next rotate
                # attempt starts clean.
                try:
                    os.unlink(tmp_gz)
                except OSError:
                    pass
                return None

            # Truncate the active log in place. Use O_TRUNC on a fresh
            # open so the inode is preserved (callers that already
            # have the file open with O_APPEND will keep appending to
            # the rotated-out inode; the next process to open will
            # see a fresh empty file).
            try:
                with open(SESSION_LOG_FILE, "w") as f:
                    pass
            except OSError:
                # If we can't truncate, the next rotation will pick up
                # the not-yet-emptied file. Not a disaster.
                pass

            removed = _enforce_retention()
            return gz_path
        finally:
            try:
                if lock_fd != -1:
                    fcntl.flock(lock_fd, fcntl.LOCK_UN)
            except OSError:
                pass


def append(event: Dict[str, Any]) -> None:
    """Append one event as a JSONL line. A `ts` field is added automatically.

    Best-effort: a logging failure must never interrupt trading, so disk errors
    are swallowed.

    R11-G1: every append re-evaluates the rotate threshold. The
    threshold check is cheap (one ``stat`` + newline count) and the
    rotate itself only fires when one of the configured limits is
    crossed, so the steady-state overhead on a normal write is
    minimal.
    """
    record = {"ts": int(time.time() * 1000), **event}
    try:
        with open(SESSION_LOG_FILE, "a") as f:
            f.write(json.dumps(record) + "\n")
    except OSError:
        pass
    # R11-G1: rotate may have crossed a threshold on this very
    # write — kick off a rotate attempt. It is a no-op if no
    # threshold is crossed. Best-effort; never raises.
    try:
        rotate()
    except Exception:
        pass
    # Fork outcome-relevant events (execute/exit/risk) into the authoritative
    # events.jsonl so the post-trade event log has a signal→order→close trace.
    # The high-volume heartbeat (scan/heartbeat/research/ta_skip) stays here
    # only. This bridges the pre-2026-08-22 architecture split where order/
    # close events never reached events.jsonl and memory rebuild found 0 orders.
    try:
        from hermes_trader import event_log
        event_log.fork_from_session(record)
    except Exception:
        pass
    # Best-effort Feishu notification dispatch. Imported lazily to avoid a
    # circular import (notify_dispatch may import modules that log events at
    # import time). Dispatched AFTER the disk write so a notification failure
    # can never lose the durable record.
    try:
        from hermes_trader import notify_dispatch
        notify_dispatch.dispatch(record)
    except Exception:
        pass


def tail(n: int = 10) -> List[Dict[str, Any]]:
    """Return the last `n` parseable events, oldest first.

    Reads backward from the end of the file in chunks instead of loading the
    whole log into memory (the log can grow to several MB over a session).

    R11-G1: tail reads the *active* log only. If you need the full
    session history (including rotated gz files), call
    ``read_all_history()`` instead. This keeps the hot path for the
    dashboard / status reports cheap.
    """
    try:
        with open(SESSION_LOG_FILE, "rb") as f:
            f.seek(0, 2)  # end
            block = 8192
            data = b""
            while data.count(b"\n") <= n and f.tell() > 0:
                step = min(block, f.tell())
                f.seek(-step, 1)
                data = f.read(step) + data
                f.seek(-step, 1)
        lines = [ln for ln in data.splitlines() if ln.strip()]
    except (FileNotFoundError, OSError):
        return []
    out: List[Dict[str, Any]] = []
    for ln in lines[-n:]:
        try:
            out.append(json.loads(ln))
        except (json.JSONDecodeError, UnicodeDecodeError):
            pass
    return out


def read_all_history(max_files: Optional[int] = None) -> List[Dict[str, Any]]:
    """Read the full session history: rotated gz files (oldest first),
    then the active log (newest on top so the most recent events win
    on dedup if the caller uses a set-based reducer).

    Each file is read line-by-line; only lines that parse as JSON are
    kept. Returns a flat list, oldest first.

    ``max_files`` is a safety cap — by default we read every rotated
    gz file. Pass a small number for tests / preview endpoints.
    """
    out: List[Dict[str, Any]] = []
    files = _list_rotated()
    if max_files is not None:
        files = files[-max_files:]
    for path in files:
        try:
            with gzip.open(path, "rt", encoding="utf-8") as f:
                for ln in f:
                    ln = ln.strip()
                    if not ln:
                        continue
                    try:
                        out.append(json.loads(ln))
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        pass
        except OSError:
            continue
    # Active log last so its events appear at the end of the list.
    out.extend(tail(n=10**9))
    return out


def reset_for_tests() -> None:
    """Test-only: drop the cached flock fd so a subsequent rotate opens a
    fresh sidecar against the new ``SESSION_LOG_FILE``. Never call in
    production code."""
    global _SESSION_LOG_FLOCK_FD
    if _SESSION_LOG_FLOCK_FD is not None:
        try:
            os.close(_SESSION_LOG_FLOCK_FD)
        except OSError:
            pass
        _SESSION_LOG_FLOCK_FD = None
    # Also clear any leftover lock sidecar from a previous test path.
    try:
        os.unlink(_lock_file_path())
    except OSError:
        pass

