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

import logging
import os
import threading
import time
from typing import Any, Optional, Union

logger = logging.getLogger(__name__)

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


# ── R13-B13: canonical config wiring ──────────────────────────────────
#
# The client layer's numeric knobs used to be scattered ``os.environ.get``
# literals across rate_limit.py / hl_client.py / exchange.py / ws_client.py:
# not visible in the dashboard config dump, not validated by
# validate_config_updates, and not overridable via .agent-config.json. They
# now register in CANONICAL_DEFAULTS (blocks ``hl_client_io`` /
# ``hl_rate_limit``) and resolve through two helpers hosted here (rate_limit
# is the bottom leaf of the client import graph; config_store has zero
# hermes_trader imports, so the lazy cfg_get import below cannot cycle).
#
# Resolution per leaf: legacy env var (if set to a non-empty string — the
# historical env channel, HIGHEST priority) → cfg_get("block.leaf")
# (HERMES_CFG_* canonical env + agent-config + CANONICAL_DEFAULTS) → the
# literal table. Every value is coerced to its kind ("i"nt / "f"loat /
# "b"ool) and range-guarded; any failure returns an independent copy of the
# literal defaults so the trading hot path never crashes on a bad config.
_HL_CLIENT_IO_DEFAULTS: dict[str, Any] = {
    "sdk_timeout_s": 30.0,
    "default_leverage": 5,
    "max_slippage_pct": 1.5,
    "max_slippage_close_pct": 5.0,
    "meta_ttl_s": 3600.0,
    "atr_ttl_s": 60.0,
    "candle_cache_ttl_s": 90.0,
    "candle_cache_max": 512,
    "funding_cache_ttl_s": 300.0,
    "ws_max_stale_s": 30,
    "ws_heartbeat_s": 10.0,
    "ws_seq_max_backward": 1024,
    # M-11 (supplemental audit 2026-08-30): per-coin mid jump filter. A single
    # allMids tick that moves a coin's price by more than this fraction vs the
    # previous accepted mid is treated as a bad print: that coin's stale value
    # is kept (frame still applied for every other coin) and the jump is
    # counted/alerted. Fraction of 1.0 = 100%; default 0.25 = 25% — wide enough
    # to admit genuine crypto volatility, narrow enough to reject a fat-finger
    # / corrupt tick (H-6 already cross-checks entry pricing against Binance).
    "ws_max_tick_jump_frac": 0.25,
}

# leaf -> (legacy env or None, kind "i"/"f"/"b", min guard value).
_HL_CLIENT_IO_SPEC: dict[str, tuple[Optional[str], str, float]] = {
    "sdk_timeout_s": ("HERMES_HL_SDK_TIMEOUT_S", "f", 0.0),
    "default_leverage": ("HERMES_DEFAULT_LEVERAGE", "i", 1.0),
    "max_slippage_pct": ("HERMES_MAX_SLIPPAGE_PCT", "f", 0.0),
    "max_slippage_close_pct": ("HERMES_MAX_SLIPPAGE_CLOSE_PCT", "f", 0.0),
    "meta_ttl_s": ("HERMES_META_TTL_S", "f", 0.0),
    "atr_ttl_s": ("HERMES_ATR_TTL_S", "f", 0.0),
    "candle_cache_ttl_s": ("HERMES_CANDLE_CACHE_TTL_S", "f", 0.0),
    "candle_cache_max": ("HERMES_CANDLE_CACHE_MAX", "i", 1.0),
    "funding_cache_ttl_s": ("HERMES_FUNDING_CACHE_TTL_S", "f", 0.0),
    "ws_max_stale_s": ("HERMES_WS_MAX_STALE_SECONDS", "i", 1.0),
    "ws_heartbeat_s": ("HERMES_WS_HEARTBEAT_S", "f", 0.0),
    "ws_seq_max_backward": ("HERMES_WS_SEQ_MAX_BACKWARD", "i", 1.0),
    "ws_max_tick_jump_frac": ("HERMES_WS_MAX_TICK_JUMP_FRAC", "f", 0.0),
}

_HL_RATE_LIMIT_DEFAULTS: dict[str, Any] = {
    "rate_refill_per_sec": 20.0,
    "rate_capacity": 600,
    "rate_max_wait_s": 30.0,
    "rate_429_retries": 2,
    "rate_opportunistic_wait_s": 2.0,
    "rate_shared": True,
    "rate_per_endpoint_gate": True,
}

_HL_RATE_LIMIT_SPEC: dict[str, tuple[Optional[str], str, float]] = {
    "rate_refill_per_sec": ("HERMES_HL_RATE_REFILL_PER_SEC", "f", 0.0),
    "rate_capacity": ("HERMES_HL_RATE_CAPACITY", "i", 1.0),
    "rate_max_wait_s": ("HERMES_HL_RATE_MAX_WAIT_S", "f", 0.0),
    "rate_429_retries": ("HERMES_HL_429_RETRIES", "i", 0.0),
    "rate_opportunistic_wait_s": ("HERMES_HL_RATE_OPPORTUNISTIC_WAIT_S", "f", 0.0),
    "rate_shared": ("HERMES_HL_RATE_SHARED", "b", 0.0),
    "rate_per_endpoint_gate": ("HERMES_HL_RATE_PER_ENDPOINT_GATE", "b", 0.0),
}

_TRUE_TOKENS = ("1", "true", "yes", "on")


def _resolve_hl_block(
    block: str,
    defaults: dict[str, Any],
    spec: dict[str, tuple[Optional[str], str, float]],
    *,
    config: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Resolve one canonical block: legacy env (top priority) → cfg_get →
    literals. Never raises — on any error a fresh copy of the literals is
    returned so the trading hot path degrades to the old behaviour."""
    try:
        # Lazy import: keeps rate_limit importable even if the config layer
        # is unavailable (and doubly breaks any potential import cycle).
        from hermes_trader.agents.config_store import cfg_get

        p = dict(defaults)
        for leaf, (legacy_env, kind, min_v) in spec.items():
            raw: Any = None
            if legacy_env is not None:
                raw = os.environ.get(legacy_env)
            if raw is None or raw == "":
                raw = cfg_get(f"{block}.{leaf}", config=config)
            if raw is None:
                continue
            if kind == "b":
                v = str(raw).strip().lower() in _TRUE_TOKENS
            elif kind == "i":
                v = int(raw)
            else:
                v = float(raw)
            if kind == "b" or v >= min_v:
                p[leaf] = v
        return p
    except Exception as e:  # noqa: BLE001 — config must never break HTTP
        logger.debug("[hl] %s params read failed, using literals: %s", block, e)
        return dict(defaults)


def _hl_client_io_params(*, config: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """R13-B13: resolved ``hl_client_io`` block (client I/O knobs)."""
    return _resolve_hl_block(
        "hl_client_io", _HL_CLIENT_IO_DEFAULTS, _HL_CLIENT_IO_SPEC, config=config
    )


def _hl_rate_limit_params(*, config: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """R13-B13: resolved ``hl_rate_limit`` block (rate-limiter knobs)."""
    return _resolve_hl_block(
        "hl_rate_limit", _HL_RATE_LIMIT_DEFAULTS, _HL_RATE_LIMIT_SPEC, config=config
    )


# Import-time resolution: the historical env channel was read at import
# time (or on the hot path) for every one of these knobs, and no production
# path toggles them at runtime; resolving once here keeps the request hot
# path config-free while preserving the "env must be set before boot"
# deployment semantics. Module symbols keep their old names as fallbacks.
_HL_CLIENT_IO = _hl_client_io_params(config={})
_HL_RATE_LIMIT = _hl_rate_limit_params(config={})


def _refill_rate() -> float:
    # 1200 weight/min = 20 weight/s. Import-time resolved (R13-B13).
    return float(_HL_RATE_LIMIT["rate_refill_per_sec"])


def _capacity() -> int:
    # Burst capacity. A full cold scan fans out candleSnapshot (weight 20)
    # calls; 600 weight absorbs the immediate burst and smooths the rest via
    # refill. Must stay well under the per-minute budget.
    return int(_HL_RATE_LIMIT["rate_capacity"])


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
    # R13-B13: shared-bucket switch resolved at import time (like the
    # bucket sizing); the state FILE path stays an env-only deployment knob.
    shared = bool(_HL_RATE_LIMIT["rate_shared"])
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


# ── per-endpoint serialization gate (R11-C1) ─────────────────────────
#
# Shared / per-process token buckets meter global budget well, but they do
# nothing about THUNDERING-HERD within one endpoint. A typical scan loop
# fans out N concurrent ``candleSnapshot`` workers; each acquires weight
# from the shared bucket (so the total is bounded) but they all stampede
# the same endpoint at the same wall-clock instant. HL's per-endpoint
# throttling then 429s them as a herd rather than as a fair share of the
# IP budget.
#
# Solution: one in-process ``threading.Lock`` per endpoint name. A caller
# must hold that endpoint's lock while it acquires the rate budget + does
# the HTTP request + releases the budget. Different endpoints (e.g.
# ``candleSnapshot`` vs ``allMids``) lock independently so they can still
# run in parallel — only SAME-endpoint calls are serialized.
#
# Net effect: the burst pattern into any one endpoint becomes
# FIFO-at-token-bucket-pace, which is exactly what HL's per-endpoint
# limiter expects. Cross-endpoint parallelism (the common case: scan +
# mids + portfolio in flight simultaneously) is preserved.
#
# The gate is opt-in via ``HERMES_HL_RATE_PER_ENDPOINT_GATE=1`` (default
# ON) so a single-process host with no concurrency doesn't pay the lock
# cost. ``HERMES_HL_RATE_PER_ENDPOINT_GATE=0`` disables it (e.g. for
# the dashboard which already serializes per UI request).

import contextlib  # noqa: E402


def _per_endpoint_gate_enabled() -> bool:
    """R13-B13: the gate switch keeps its historical CALL-TIME env read —
    tests/ops toggle ``HERMES_HL_RATE_PER_ENDPOINT_GATE`` after import and
    expect the change to take effect immediately. A non-empty env value
    wins (legacy channel, highest priority); unset/empty falls back to the
    import-time canonical snapshot (``hl_rate_limit.rate_per_endpoint_gate``)."""
    raw = os.environ.get("HERMES_HL_RATE_PER_ENDPOINT_GATE")
    if raw is None or raw.strip() == "":
        return bool(_HL_RATE_LIMIT["rate_per_endpoint_gate"])
    return raw.strip().lower() in _TRUE_TOKENS


@contextlib.contextmanager
def per_endpoint_gate(endpoint: str):
    """Acquire (and release) the in-process serialization lock for one
    endpoint name. No-op when the gate is disabled via env or for the
    sentinel ``"unknown"`` endpoint (avoids one big lock for every
    un-typed probe).
    """
    if not _per_endpoint_gate_enabled():
        yield
        return
    if not endpoint or endpoint == "unknown":
        yield
        return
    gate = _PER_ENDPOINT_GATES.get(endpoint)
    if gate is None:
        # Double-checked lock pattern: the dict assignment is atomic
        # under CPython's GIL, but a rare race could let two threads
        # build two Lock objects for the same endpoint. The first one
        # stored wins; the second one is dropped — both are still
        # valid Locks so correctness is preserved, just a tiny memory
        # leak that's bounded by the number of distinct endpoint names
        # observed.
        gate = threading.Lock()
        existing = _PER_ENDPOINT_GATES.setdefault(endpoint, gate)
        if existing is not gate:
            gate = existing
    with gate:
        yield


_PER_ENDPOINT_GATES: dict = {}


# Probe metrics so we can graph "this endpoint is contended" without
# making a new metric per endpoint (label cardinality is bounded by the
# known endpoint set, ~16 names).
try:
    from hermes_trader import metrics  # type: ignore

    _GATE_WAIT_S = metrics.HL_RATE_GATE_WAIT  # Histogram {endpoint}
except Exception:  # noqa: BLE001 — never break module import
    metrics = None  # type: ignore
    _GATE_WAIT_S = None


@contextlib.contextmanager
def timed_per_endpoint_gate(endpoint: str):
    """Like per_endpoint_gate but records a wait-time histogram so we
    can alert on an endpoint whose gate is consistently held long (R11-F1).
    Falls back to per_endpoint_gate if the metric isn't available."""
    import time as _t
    if not endpoint or endpoint == "unknown":
        with per_endpoint_gate(endpoint):
            yield
        return
    if _GATE_WAIT_S is None:
        with per_endpoint_gate(endpoint):
            yield
        return
    if not _per_endpoint_gate_enabled():
        yield
        return
    # Lazily create the lock so the timed variant can record the wait
    # on the FIRST entry to a new endpoint.
    gate = _PER_ENDPOINT_GATES.get(endpoint)
    if gate is None:
        gate = threading.Lock()
        existing = _PER_ENDPOINT_GATES.setdefault(endpoint, gate)
        if existing is not gate:
            gate = existing
    t0 = _t.monotonic()
    with gate:
        wait = _t.monotonic() - t0
        try:
            _GATE_WAIT_S.labels(endpoint=endpoint).observe(wait)
        except Exception:
            pass
        yield


def _reset_per_endpoint_gates() -> None:
    """Test helper: drop the per-endpoint gate table so a fresh
    ``monkeypatch.setenv`` takes effect without a stale lock."""
    _PER_ENDPOINT_GATES.clear()


def gate_endpoint_names() -> list:
    """Inspect the set of endpoints that have been observed since the
    last reset. Used by tests to confirm a new endpoint creates a gate."""
    return sorted(_PER_ENDPOINT_GATES.keys())


HL_LIMITER = _build_limiter()
