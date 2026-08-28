"""Token-bucket rate limiter for the Hyperliquid /info + /exchange endpoints.

HL allows ~1200 request-weight per minute per IP. Different endpoints cost
different weight (candleSnapshot=20, allMids=2, etc.). Previously the scan
loop paced itself with a crude fixed `time.sleep(0.3)` between batches, which
either left throughput on the table or — during backtests / dense scans —
fired straight into 429s (200+ "no candles" skips observed).

A single shared bucket meters every outbound request by its weight, so bursts
are smoothed against the real per-minute budget regardless of which code path
(live scan, dashboard, backtest, treasury) is making the call.

Cross-process sharing
---------------------
HL's limit is per-IP, but the live trading loop and the dashboard/server run
as SEPARATE processes. A per-process bucket let each one independently assume
it owned the whole 1200 weight/min budget, so together they routinely doubled
the real IP usage and tripped "rate budget exhausted ... skipping request".
When `HERMES_HL_RATE_SHARED=1` (default) the bucket state is kept in a small
file under `/dev/shm` (tmpfs, no disk wear) and guarded by an flock so all
processes on the host draw from one token pool. The in-process bucket remains
as an automatic fallback if the shared state can't be opened.
"""
from __future__ import annotations

import os
import threading
import time
from typing import Union

# Per-endpoint weights from HL docs. Default 20 (the expensive bucket) for
# anything unknown so we never under-count and trip a 429.
_ENDPOINT_WEIGHT = {
    "candleSnapshot": 20,
    "metaAndAssetCtxs": 20,
    "spotMetaAndAssetCtxs": 20,
    "meta": 20,
    "spotMeta": 20,
    "allMids": 2,
    "clearinghouseState": 2,
    "spotClearinghouseState": 2,
    "l2Book": 2,
    "userNonFundingLedgerUpdates": 2,
    "perpDexs": 2,
    "portfolio": 2,
    "userFills": 2,
    "openOrders": 2,
}


def endpoint_weight(req_type: str | None) -> int:
    return _ENDPOINT_WEIGHT.get(req_type or "", 20)


def _refill_rate() -> float:
    # 1200 weight/min = 20 weight/s. Env-overridable.
    return float(os.environ.get("HERMES_HL_RATE_REFILL_PER_SEC", "20"))


def _capacity() -> int:
    # Burst capacity. A full cold scan fans out candleSnapshot (weight 20)
    # calls; 600 weight absorbs the immediate burst and smooths the rest via
    # refill. Must stay well under the per-minute budget.
    return int(os.environ.get("HERMES_HL_RATE_CAPACITY", "600"))


class TokenBucket:
    """Thread-safe in-process token bucket. `acquire(weight)` blocks (up to
    max_wait) until enough tokens have refilled, then deducts them."""

    def __init__(self, capacity: int, refill_per_sec: float) -> None:
        self._capacity = float(capacity)
        self._tokens = float(capacity)
        self._refill = float(refill_per_sec)
        self._last = time.monotonic()
        self._lock = threading.Lock()

    @property
    def refill_per_sec(self) -> float:
        return self._refill

    def acquire(self, weight: int = 20, max_wait: float = 10.0) -> bool:
        """Block until `weight` tokens are available. Returns False if the
        wait would exceed `max_wait` (caller should back off / skip)."""
        deadline = time.monotonic() + max_wait
        while True:
            sleep_for = 0.0
            with self._lock:
                now = time.monotonic()
                self._tokens = min(
                    self._capacity,
                    self._tokens + (now - self._last) * self._refill,
                )
                self._last = now
                if self._tokens >= weight:
                    self._tokens -= weight
                    return True
                if self._refill <= 0:
                    return False  # no refill → will never recover
                deficit = weight - self._tokens
                sleep_for = deficit / self._refill
            if time.monotonic() + sleep_for > deadline:
                return False
            time.sleep(min(sleep_for, 0.5))

    def penalize(self, weight: float) -> None:
        """Deduct extra tokens after a server-side rejection (429 / Retry-After).

        Used for adaptive backoff: when HL tells us we overshot, drain the
        bucket so subsequent callers queue instead of immediately re-hitting
        the limit. Never drops below zero."""
        with self._lock:
            self._tokens = max(0.0, self._tokens - float(weight))
            self._last = time.monotonic()


class SharedTokenBucket:
    """Cross-process token bucket backed by an flock-guarded state file.

    State is two floats (`tokens`, `last_monotonic`) written atomically. The
    file lives on tmpfs (`/dev/shm`) by default so there's no disk I/O. Every
    acquire takes an exclusive lock only for the microseconds needed to
    read/refill/deduct; the blocking sleep happens WITHOUT the lock so other
    processes/threads aren't stalled.

    `monotonic()` is per-process but advances at the same wall rate on the
    same host, and the file is rewritten by whichever process holds the lock,
    so cross-process elapsed-time math is consistent (a fixed offset between
    processes cancels out because we always compare two timestamps produced
    by the SAME writer on consecutive writes).
    """

    def __init__(self, capacity: int, refill_per_sec: float, path: str) -> None:
        self._capacity = float(capacity)
        self._refill = float(refill_per_sec)
        self._path = path
        self._thread_lock = threading.Lock()
        try:
            # Ensure the file exists; rw for owner only.
            fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
            os.close(fd)
            self._available = True
        except OSError:
            self._available = False

    @property
    def available(self) -> bool:
        return self._available

    @property
    def refill_per_sec(self) -> float:
        return self._refill

    def _read_state(self, fd: int) -> tuple[float, float]:
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            raw = os.read(fd, 64)
            if not raw:
                return self._capacity, time.monotonic()
            parts = raw.decode("utf-8", "ignore").split()
            if len(parts) != 2:
                return self._capacity, time.monotonic()
            return float(parts[0]), float(parts[1])
        except (OSError, ValueError):
            return self._capacity, time.monotonic()

    @staticmethod
    def _write_state(fd: int, tokens: float, last: float) -> None:
        # In-place write while the caller holds the exclusive flock. The lock
        # guarantees no other process/thread reads mid-write, so an atomic
        # rename isn't needed (and would invalidate the held fd). Keep the
        # write short (<64 bytes, one syscall on tmpfs).
        data = f"{tokens:.6f} {last:.6f}\n".encode("utf-8")
        os.lseek(fd, 0, os.SEEK_SET)
        os.ftruncate(fd, 0)
        os.write(fd, data)

    def _transact(self, weight: float, deduct: bool) -> tuple[bool, float]:
        """With the file lock, refill and optionally deduct `weight`.

        Returns (granted, sleep_for). When granted, tokens were deducted.
        When not granted, sleep_for is how long to wait before retrying.
        """
        if self._refill <= 0:
            return False, 0.0
        with self._thread_lock:
            try:
                fd = os.open(self._path, os.O_RDWR)
            except OSError:
                # Can't lock — grant rather than hard-block the trade path.
                return True, 0.0
            try:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_EX)
                try:
                    tokens, last = self._read_state(fd)
                    now = time.monotonic()
                    tokens = min(
                        self._capacity,
                        tokens + (now - last) * self._refill,
                    )
                    if tokens >= weight:
                        if deduct:
                            tokens -= weight
                        self._write_state(fd, tokens, now)
                        return True, 0.0
                    deficit = weight - tokens
                    # Persist the refilled (but not deducted) state.
                    self._write_state(fd, tokens, now)
                    return False, deficit / self._refill
                finally:
                    fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                return True, 0.0
            finally:
                try:
                    os.close(fd)
                except OSError:
                    pass

    def acquire(self, weight: int = 20, max_wait: float = 10.0) -> bool:
        if not self._available:
            return True
        deadline = time.monotonic() + max_wait
        while True:
            granted, sleep_for = self._transact(weight, deduct=True)
            if granted:
                return True
            if time.monotonic() + sleep_for > deadline:
                return False
            # Sleep without holding any lock so other processes can proceed.
            time.sleep(min(sleep_for, 0.5))

    def penalize(self, weight: float) -> None:
        """Drain `weight` tokens cross-process after a 429."""
        if not self._available or weight <= 0:
            return

        def _drain() -> None:
            try:
                fd = os.open(self._path, os.O_RDWR)
            except OSError:
                return
            try:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_EX)
                try:
                    tokens, last = self._read_state(fd)
                    now = time.monotonic()
                    tokens = min(self._capacity, tokens + (now - last) * self._refill)
                    tokens = max(0.0, tokens - float(weight))
                    self._write_state(fd, tokens, now)
                finally:
                    fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
            finally:
                try:
                    os.close(fd)
                except OSError:
                    pass

        self._thread_lock.acquire()
        try:
            _drain()
        finally:
            self._thread_lock.release()


def _build_limiter() -> Union["TokenBucket", "SharedTokenBucket"]:
    cap = _capacity()
    rate = _refill_rate()
    shared = os.environ.get("HERMES_HL_RATE_SHARED", "1").strip().lower() in (
        "1", "true", "yes", "on",
    )
    if shared:
        path = os.environ.get(
            "HERMES_HL_RATE_STATE_FILE", "/dev/shm/hermes_hl_rate.state"
        )
        try:
            bucket = SharedTokenBucket(cap, rate, path)
            if bucket.available:
                return bucket
        except Exception:
            pass
    return TokenBucket(cap, rate)


HL_LIMITER = _build_limiter()
