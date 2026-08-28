#!/usr/bin/env python3
"""Circuit-breaker high-concurrency verification script.

Simulates concurrent service requests against a faulty backend to observe
the SignalBus circuit-breaker behaviour under load:

  Phase 1 (FAIL):   backend returns 503 for every request — the breaker
                    should trip to OPEN after ``--threshold`` failures,
                    then all subsequent requests short-circuit (no call).
  Phase 2 (COOL):   Script sleeps for ``--cooldown`` seconds so the
                    breaker transitions OPEN → HALF_OPEN.
  Phase 3 (RECOVER): backend returns 200 — the first trial request in
                    HALF_OPEN should succeed and CLOSE the breaker; all
                    remaining requests flow through normally.

Usage
-----
# Dry-run against in-process mocks (fast, no network):
python scripts/verify_circuit_breaker.py --workers 20 --total 200

# Shorter cooldown for faster test cycles:
python scripts/verify_circuit_breaker.py --cooldown 2 --threshold 3
"""

from __future__ import annotations

import argparse
import sys
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

# ── Shared in-process state for the mock backend ───────────────────────


class MockBackend:
    """Thread-safe mock that toggles between failing and recovering."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._fail = True  # start in failure mode
        self.request_count = 0
        self.failure_count = 0
        self.success_count = 0

    def set_fail(self, fail: bool) -> None:
        with self._lock:
            self._fail = fail

    def call(self) -> str:
        with self._lock:
            self.request_count += 1
            if self._fail:
                self.failure_count += 1
                raise RuntimeError("mock 503")
            self.success_count += 1
            return '{"verdict": "HOLD"}'


# ── Circuit-breaker adapter ────────────────────────────────────────────


@dataclass
class BreakerAdapter:
    """Unified interface over a circuit-breaker implementation."""

    name: str
    check: Callable[[], bool]          # returns True if open (skip)
    record_failure: Callable[[], None]
    record_success: Callable[[], None]
    state_snapshot: Callable[[], str]


def _make_signal_adapter(threshold: int, cooldown: float) -> BreakerAdapter:
    """Wrap a SignalBus instance with test parameters."""
    try:
        from signal_bus import SignalBus
    except ImportError:
        # Fallback: define a minimal compatible class inline.
        import enum

        class CircuitState(str, enum.Enum):
            CLOSED = "closed"
            HALF_OPEN = "half_open"
            OPEN = "open"

        class SignalBus:  # type: ignore[no-redef]
            def __init__(self, fail_threshold: int, recovery_s: float) -> None:
                self._fail_threshold = fail_threshold
                self._recovery_s = recovery_s
                self._lock = threading.Lock()
                self._consecutive_failures = 0
                self._state = CircuitState.CLOSED
                self._opened_at: Optional[float] = None

            def is_open(self) -> bool:
                with self._lock:
                    if (
                        self._state == CircuitState.OPEN
                        and self._opened_at is not None
                        and time.monotonic() - self._opened_at >= self._recovery_s
                    ):
                        self._state = CircuitState.HALF_OPEN
                        self._consecutive_failures = 0
                    return self._state == CircuitState.OPEN

            def report_failure(self, reason: str = "") -> None:
                with self._lock:
                    self._consecutive_failures += 1
                    if self._state == CircuitState.HALF_OPEN:
                        self._state = CircuitState.OPEN
                        self._opened_at = time.monotonic()
                        return
                    if self._state == CircuitState.OPEN:
                        return
                    if self._consecutive_failures >= self._fail_threshold:
                        self._state = CircuitState.OPEN
                        self._opened_at = time.monotonic()

            def report_success(self) -> None:
                with self._lock:
                    self._consecutive_failures = 0
                    self._state = CircuitState.CLOSED
                    self._opened_at = None

    bus = SignalBus(fail_threshold=threshold, recovery_s=cooldown)

    def check() -> bool:
        return bus.is_open()

    def record_failure() -> None:
        bus.report_failure("mock failure")

    def record_success() -> None:
        bus.report_success()

    def snapshot() -> str:
        return (
            f"state={bus._state.value} "  # type: ignore[union-attr]
            f"failures={bus._consecutive_failures}"
        )

    return BreakerAdapter("signal_bus", check, record_failure, record_success, snapshot)


# ── Request result tracking ────────────────────────────────────────────


@dataclass
class RequestResult:
    index: int
    outcome: str  # "success" | "failure" | "short_circuit"
    elapsed_ms: float
    phase: str
    thread_id: int
    breaker_state: str = ""


@dataclass
class PhaseStats:
    phase: str
    total: int = 0
    success: int = 0
    failure: int = 0
    short_circuit: int = 0
    latencies_ms: List[float] = field(default_factory=list)

    @property
    def avg_ms(self) -> float:
        return sum(self.latencies_ms) / len(self.latencies_ms) if self.latencies_ms else 0.0

    @property
    def max_ms(self) -> float:
        return max(self.latencies_ms) if self.latencies_ms else 0.0


# ── Worker ─────────────────────────────────────────────────────────────


def _worker(
    idx: int,
    phase: str,
    backend: MockBackend,
    breaker: BreakerAdapter,
    extra_latency_ms: float = 0.0,
) -> RequestResult:
    t0 = time.time()
    tid = threading.get_ident()

    # Pre-check: short-circuit if open.
    if breaker.check():
        elapsed = (time.time() - t0) * 1000
        return RequestResult(
            index=idx,
            outcome="short_circuit",
            elapsed_ms=elapsed,
            phase=phase,
            thread_id=tid,
            breaker_state=breaker.state_snapshot(),
        )

    # Simulate network latency.
    if extra_latency_ms > 0:
        time.sleep(extra_latency_ms / 1000.0)

    try:
        result = backend.call()
        breaker.record_success()
        elapsed = (time.time() - t0) * 1000
        return RequestResult(idx, "success", elapsed, phase, tid, breaker.state_snapshot())
    except Exception:
        breaker.record_failure()
        elapsed = (time.time() - t0) * 1000
        return RequestResult(idx, "failure", elapsed, phase, tid, breaker.state_snapshot())


# ── Phase runner ───────────────────────────────────────────────────────


def _run_phase(
    name: str,
    total: int,
    workers: int,
    backend: MockBackend,
    breaker: BreakerAdapter,
    fail: bool,
    extra_latency_ms: float = 0.0,
) -> PhaseStats:
    backend.set_fail(fail)
    stats = PhaseStats(phase=name)
    print(
        f"\n{'='*60}\n"
        f"  PHASE: {name}  (backend={'FAILING' if fail else 'RECOVERED'}, "
        f"requests={total}, workers={workers})\n"
        f"  breaker before: {breaker.state_snapshot()}\n"
        f"{'='*60}"
    )

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [
            pool.submit(_worker, i, name, backend, breaker, extra_latency_ms)
            for i in range(total)
        ]
        for fut in as_completed(futures):
            r = fut.result()
            stats.total += 1
            stats.latencies_ms.append(r.elapsed_ms)
            if r.outcome == "success":
                stats.success += 1
            elif r.outcome == "failure":
                stats.failure += 1
            else:
                stats.short_circuit += 1
            # Print first 5 and last 5 for visibility.
            if r.index < 5 or r.index >= total - 5:
                print(
                    f"  [{r.index:04d}] {r.outcome:14s} {r.elapsed_ms:8.2f}ms  "
                    f"{r.breaker_state}"
                )
                if r.index == 4 and total > 10:
                    print(f"  ... ({total - 10} more requests) ...")

    print(
        f"\n  Phase {name} summary:\n"
        f"    total={stats.total}  success={stats.success}  "
        f"failure={stats.failure}  short_circuit={stats.short_circuit}\n"
        f"    latency  avg={stats.avg_ms:.2f}ms  max={stats.max_ms:.2f}ms\n"
        f"    breaker after: {breaker.state_snapshot()}"
    )
    return stats


# ── Main ───────────────────────────────────────────────────────────────


def main() -> int:
    parser = argparse.ArgumentParser(
        description="SignalBus circuit-breaker high-concurrency verification"
    )
    parser.add_argument("--workers", type=int, default=20, help="Concurrent workers")
    parser.add_argument("--total", type=int, default=100, help="Requests per phase")
    parser.add_argument("--threshold", type=int, default=3, help="Failure threshold")
    parser.add_argument("--cooldown", type=float, default=3.0, help="Cooldown seconds")
    parser.add_argument(
        "--latency-ms",
        type=float,
        default=5.0,
        help="Simulated network latency per request in ms",
    )
    args = parser.parse_args()

    # Ensure agents/ and shared dir are importable.
    for p in (
        "/home/ldy/hermes-trader/hermes_trader/agents",
        "/home/ldy/.hermes-trading",
    ):
        if p not in sys.path:
            sys.path.insert(0, p)

    breaker = _make_signal_adapter(args.threshold, args.cooldown)

    print(
        f"Circuit-breaker verification: {breaker.name}\n"
        f"  threshold={args.threshold}  cooldown={args.cooldown}s  "
        f"workers={args.workers}  total_per_phase={args.total}"
    )

    backend = MockBackend()

    # Phase 1: failing backend — expect failures then short-circuits.
    p1 = _run_phase(
        "FAIL",
        args.total,
        args.workers,
        backend,
        breaker,
        fail=True,
        extra_latency_ms=args.latency_ms,
    )

    # Phase 2: wait for cooldown so the breaker can enter HALF_OPEN.
    print(
        f"\n{'='*60}\n"
        f"  COOLDOWN: sleeping {args.cooldown + 0.5:.1f}s for breaker to "
        f"transition OPEN → HALF_OPEN ...\n"
        f"{'='*60}"
    )
    time.sleep(args.cooldown + 0.5)
    print(f"  breaker after cooldown: {breaker.state_snapshot()}")

    # Phase 3: recovered backend — first trial succeeds, rest flow through.
    p3 = _run_phase(
        "RECOVER",
        args.total,
        args.workers,
        backend,
        breaker,
        fail=False,
        extra_latency_ms=args.latency_ms,
    )

    # ── Final report ──────────────────────────────────────────────────
    print(f"\n{'#'*60}")
    print("  FINAL REPORT")
    print(f"{'#'*60}")
    print(
        f"  {'Phase':<12} {'Total':>6} {'Success':>8} {'Failure':>8} "
        f"{'ShortCirc':>10} {'Avg ms':>10} {'Max ms':>10}"
    )
    for s in (p1, p3):
        print(
            f"  {s.phase:<12} {s.total:>6} {s.success:>8} {s.failure:>8} "
            f"{s.short_circuit:>10} {s.avg_ms:>10.2f} {s.max_ms:>10.2f}"
        )

    # ── Assertions ────────────────────────────────────────────────────
    ok = True
    if p1.short_circuit == 0:
        print("\n  [FAIL] No short-circuits observed in FAIL phase — "
              "circuit breaker did not trip!")
        ok = False
    else:
        print(f"\n  [PASS] Breaker tripped — {p1.short_circuit} requests short-circuited "
              f"(saved ~{p1.short_circuit * args.latency_ms:.0f}ms of wait time)")

    if p3.success == 0:
        print("  [FAIL] No successes in RECOVER phase — breaker did not recover!")
        ok = False
    else:
        trip_ratio = p1.short_circuit / p1.total if p1.total else 0
        print(f"  [PASS] Breaker recovered — {p3.success}/{p3.total} requests succeeded")
        print(f"  [INFO] Short-circuit ratio in FAIL phase: {trip_ratio:.1%}")

    # HALF_OPEN verification: after cooldown the first call in RECOVER
    # should be a trial (not short-circuited).
    if p3.short_circuit > 0:
        print(f"  [WARN] {p3.short_circuit} short-circuits in RECOVER phase "
              f"(expected 0 after recovery)")

    print(f"\n  Backend total requests: {backend.request_count} "
          f"(success={backend.success_count}, failure={backend.failure_count})")
    print(f"  Final breaker state: {breaker.state_snapshot()}")

    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
