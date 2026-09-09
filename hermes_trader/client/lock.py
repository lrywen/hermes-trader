"""scanner_lock — fcntl lock with PID-aliveness stale recovery.

Ensures only one scan runs at a time, with automatic recovery if a previous
scan crashed: fcntl flock is released by the kernel on process death, so a
stale lock file is reclaimed on the next acquire.
"""

import calendar
import fcntl
import json
import logging
import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Optional

logger = logging.getLogger(__name__)

_DEFAULT_LOCK_DIR = os.path.expanduser("~/.hermes")


def _lock_path(name: str, lock_dir: Optional[str] = None) -> Path:
    base = Path(lock_dir or _DEFAULT_LOCK_DIR)
    base.mkdir(parents=True, exist_ok=True)
    return base / f"hermes-{name}.lock"


def _read_lock_metadata(path: Path) -> Optional[dict[str, Any]]:
    try:
        with path.open("r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


def is_pid_alive(pid: Optional[int]) -> bool:
    """Check if a PID is still running.

    Single source of truth for PID-aliveness probes (daemon state checks and
    stale-lock recovery both delegate here).
    """
    if pid is None:
        return False
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


# Backwards-compatible alias for internal callers.
_is_pid_alive = is_pid_alive


def _write_metadata_inplace(fd: int, payload: dict[str, Any]) -> None:
    """Write metadata onto the already-locked fd. Keeps inode + flock stable."""
    encoded = json.dumps(payload).encode("utf-8")
    os.ftruncate(fd, 0)
    os.lseek(fd, 0, os.SEEK_SET)
    while encoded:
        written = os.write(fd, encoded)
        encoded = encoded[written:]


@contextmanager
def scanner_lock(name: str, timeout: float = 300.0) -> Iterator[None]:
    """Acquire a file lock for a named scanner.

    If a previous scan is still running, waits up to `timeout` seconds.
    If the previous scan has died (crash), reclaims the lock immediately.

    Usage:
        with scanner_lock("scan", timeout=300):
            run_scan()  # only one scan at a time
    """
    lock_path = _lock_path(name)
    acquired = False
    deadline = time.time() + timeout
    start_time = time.time()
    metadata = {
        "pid": os.getpid(),
        "start_iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(start_time)),
        "name": name,
        "hostname": os.uname().nodename if hasattr(os, 'uname') else "unknown",
    }

    fd = None
    try:
        while time.time() < deadline:
            try:
                fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o644)
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                # Got the lock
                acquired = True
                # Write metadata in-place
                _write_metadata_inplace(fd, metadata)
                logger.debug(f"[lock] Acquired lock '{name}' (PID {os.getpid()})")
                break
            except (IOError, OSError):
                # Lock held by someone else
                if fd is not None:
                    os.close(fd)
                    fd = None

                # Check if previous holder is alive
                prev = _read_lock_metadata(lock_path)
                if prev and not _is_pid_alive(prev.get("pid")):
                    logger.info(f"[lock] Reclaiming stale lock '{name}' (prev PID {prev.get('pid')} is dead)")
                    continue  # try again, we should get it now

                # Previous holder is alive — wait a bit
                logger.debug(f"[lock] Waiting for lock '{name}' (held by PID {prev.get('pid') if prev else '?'})")
                time.sleep(0.5)

        if not acquired:
            raise TimeoutError(f"Could not acquire lock '{name}' within {timeout}s")

        # Yield control — caller's scan runs here
        yield

    finally:
        if fd is not None:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
                os.close(fd)
            except OSError:
                pass
        logger.debug(f"[lock] Released lock '{name}'")


class EntryOrderLock:
    """Non-blocking cross-process lock serialising ENTRY order placement.

    G-P1-3 (audit 2026-09-10): the API server (manual /api/hl/place-order and
    /api/agent/execute) and the autonomous trading loop run as SEPARATE
    processes, so the in-process ``threading.Lock`` + in-flight sets in
    executor.py cannot mutually exclude them. Both can observe "no position"
    and both place an entry → double-open. This single flock on a shared
    sidecar serialises the check-then-place window across every process.

    Intentionally NON-blocking (``LOCK_NB``): an entry path that finds the
    lock held refuses immediately (manual → HTTP 409, auto → skips this tick)
    rather than queueing an order behind an unknown amount of exchange I/O.
    The kernel releases the flock on process death, so a crash never wedges
    it (unlike a PID-stamped lock file, no stale recovery is needed).

    Usage::

        _ENTRY_LOCK = EntryOrderLock()
        if not _ENTRY_LOCK.acquire():
            raise ...  # another entry is mid-flight
        try:
            ...re-check position, place order, register tracker...
        finally:
            _ENTRY_LOCK.release()
    """

    def __init__(self, name: str = "entry-order", lock_dir: Optional[str] = None) -> None:
        self._path = _lock_path(name, lock_dir)
        self._fd: Optional[int] = None

    def acquire(self) -> bool:
        """Try once, never waits. Returns True if the lock was acquired."""
        if self._fd is not None:
            return True
        fd = None
        try:
            fd = os.open(str(self._path), os.O_CREAT | os.O_RDWR, 0o644)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (IOError, OSError):
            # Held by another process/thread.
            try:
                if fd is not None:
                    os.close(fd)
            except OSError:
                pass
            return False
        self._fd = fd
        return True

    def release(self) -> None:
        if self._fd is None:
            return
        fd, self._fd = self._fd, None
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            os.close(fd)
        except OSError:
            pass

    @contextmanager
    def hold(self) -> Iterator[None]:
        """Context manager that raises ``BlockingIOError`` if already held."""
        if not self.acquire():
            raise BlockingIOError(f"entry order lock held: {self._path}")
        try:
            yield
        finally:
            self.release()


def check_lock_status(name: str, lock_dir: Optional[str] = None) -> dict[str, Any]:
    """Check if a lock is currently held and by whom."""
    lock_path = _lock_path(name, lock_dir)
    prev = _read_lock_metadata(lock_path)

    if prev is None:
        return {"held": False, "lock_file": False}

    pid = prev.get("pid")
    alive = _is_pid_alive(pid)
    return {
        "held": True,
        "lock_file": lock_path.exists(),
        "pid": pid,
        "pid_alive": alive,
        "start_iso": prev.get("start_iso"),
        "name": prev.get("name"),
        "age_seconds": round(time.time() - calendar.timegm(time.strptime(prev.get("start_iso", ""), "%Y-%m-%dT%H:%M:%SZ")), 1),
    }
