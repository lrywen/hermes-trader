"""Phase 4 P0-2 — low-latency user-fill wake (ws_client + hl_client wrappers).

The WS callback thread enqueues a fill and sets ``_fills_wake``; the main
trading loop waits on that Event between cycles/batches, then drains and
reports the fill on the MAIN thread (single-writer rule preserved: the
callback thread never touches the session log / SSE).

Three required paths are covered:
  * normal  — a fill frame arriving mid-wait wakes the waiter immediately,
              and the fill is drained intact; multiple fills coalesce into
              one wake.
  * degraded/timeout — no fill means the wait returns False after the
              timeout; ``timeout <= 0`` is a pure state probe that never
              sleeps; when the WS is not running the hl_client wrapper
              degrades into a plain sleep and returns False (no crash).
  * failure/race — clear-before-drain is wake-loss-free: a fill whose
              ``set()`` lands after ``clear()`` (item put after the drain's
              final Empty observation) leaves the Event set so the next
              wait returns immediately and the fill is never lost; a
              spurious wake (item already caught) just drains an empty
              queue; concurrent producers under a hammer loop deliver
              every unique fill; a replayed duplicate tid wakes once and
              is enqueued only once.
"""
from __future__ import annotations

import threading
import time

import pytest

from hermes_trader.client import hl_client
from hermes_trader.client import ws_client as ws_client_mod
from hermes_trader.client.ws_client import HyperliquidWebSocket


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

@pytest.fixture
def ws(monkeypatch: pytest.MonkeyPatch) -> HyperliquidWebSocket:
    """Bare WS client — no start() (no network)."""
    monkeypatch.setenv("HERMES_WS_HEARTBEAT_S", "3600")
    monkeypatch.setenv("HERMES_WS_SEQ_MAX_BACKWARD", "1024")
    return HyperliquidWebSocket()


def _fill_frame(tid: str, coin: str = "BTC", side: str = "A",
                sz: str = "0.1", px: str = "50000",
                closed_pnl: str = "0", f_dir: str = "Open") -> dict:
    """SDK-shaped userFills frame with one fill."""
    return {
        "channel": "userFills",
        "data": {
            "fills": [
                {
                    "coin": coin,
                    "side": side,
                    "sz": sz,
                    "px": px,
                    "dir": f_dir,
                    "closedPnl": closed_pnl,
                    "tid": tid,
                    "time": 1700000000000,
                }
            ]
        },
    }


# ---------------------------------------------------------------------------
# Normal path — callback enqueues + sets; waiter wakes; drain gets the fill
# ---------------------------------------------------------------------------

class TestFillWakeNormal:
    def test_fill_arriving_mid_wait_wakes_immediately(self, ws: HyperliquidWebSocket) -> None:
        """A fill delivered ~30ms into a long wait must return well before it."""
        def producer() -> None:
            time.sleep(0.03)
            ws._on_user_fills(_fill_frame("tid-1"))

        t = threading.Thread(target=producer, daemon=True)
        t.start()
        t0 = time.monotonic()
        woken = ws.wait_for_fills(5.0)
        elapsed = time.monotonic() - t0
        t.join(timeout=1.0)

        assert woken is True
        assert elapsed < 1.0, f"wait did not wake promptly: {elapsed:.2f}s"

        fills = ws.drain_user_fills()
        assert len(fills) == 1
        assert fills[0]["tid"] == "tid-1"
        assert fills[0]["coin"] == "BTC"
        # After a drain the Event is re-armed.
        assert ws._fills_wake.is_set() is False

    def test_multiple_fills_coalesce_into_one_wake(self, ws: HyperliquidWebSocket) -> None:
        """Two fills in one frame wake once and drain both in FIFO order."""
        frame = {
            "channel": "userFills",
            "data": {
                "fills": [
                    {"coin": "BTC", "side": "A", "sz": "0.1", "px": "50000",
                     "dir": "Open", "closedPnl": "0", "tid": "tid-a",
                     "time": 1700000000000},
                    {"coin": "ETH", "side": "B", "sz": "1.0", "px": "3000",
                     "dir": "Close", "closedPnl": "12.5", "tid": "tid-b",
                     "time": 1700000001000},
                ]
            },
        }

        def producer() -> None:
            time.sleep(0.03)
            ws._on_user_fills(frame)

        t = threading.Thread(target=producer, daemon=True)
        t.start()
        assert ws.wait_for_fills(5.0) is True
        t.join(timeout=1.0)

        fills = ws.drain_user_fills()
        assert [f["tid"] for f in fills] == ["tid-a", "tid-b"]
        assert fills[1]["closedPnl"] == "12.5"

    def test_hl_wrapper_wakes_when_ws_running(
        self, ws: HyperliquidWebSocket, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """hl_client.wait_for_ws_user_fills delegates to the live WS."""
        monkeypatch.setattr(hl_client, "_ws_mids_instance", ws)

        def producer() -> None:
            time.sleep(0.03)
            ws._on_user_fills(_fill_frame("tid-wrap-1"))

        t = threading.Thread(target=producer, daemon=True)
        t.start()
        t0 = time.monotonic()
        woken = hl_client.wait_for_ws_user_fills(5.0)
        elapsed = time.monotonic() - t0
        t.join(timeout=1.0)

        assert woken is True
        assert elapsed < 1.0
        assert len(hl_client.drain_ws_user_fills()) == 1


# ---------------------------------------------------------------------------
# Degraded / timeout path
# ---------------------------------------------------------------------------

class TestFillWakeTimeout:
    def test_no_fill_returns_false_after_timeout(self, ws: HyperliquidWebSocket) -> None:
        t0 = time.monotonic()
        assert ws.wait_for_fills(0.1) is False
        elapsed = time.monotonic() - t0
        assert 0.08 <= elapsed < 0.5, f"timeout misbehaved: {elapsed:.2f}s"
        assert ws.drain_user_fills() == []

    def test_zero_timeout_is_state_probe(self, ws: HyperliquidWebSocket) -> None:
        """timeout <= 0 must never sleep and return the Event state."""
        t0 = time.monotonic()
        assert ws.wait_for_fills(0) is False
        assert time.monotonic() - t0 < 0.05

        ws._fills_wake.set()
        assert ws.wait_for_fills(0) is True

    def test_hl_wrapper_without_ws_degrades_to_sleep(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """WS not started → plain sleep + False (no exception, no wake)."""
        monkeypatch.setattr(hl_client, "_ws_mids_instance", None)
        t0 = time.monotonic()
        assert hl_client.wait_for_ws_user_fills(0.05) is False
        elapsed = time.monotonic() - t0
        assert 0.04 <= elapsed < 0.3
        # Drain with no WS is an empty list, not an error.
        assert hl_client.drain_ws_user_fills() == []

    def test_hl_wrapper_swallows_ws_exception(
        self, ws: HyperliquidWebSocket, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A WS that raises inside wait_for_fills degrades to sleep + False."""
        monkeypatch.setattr(hl_client, "_ws_mids_instance", ws)

        def boom(_timeout: float) -> bool:
            raise RuntimeError("simulated ws failure")

        monkeypatch.setattr(ws, "wait_for_fills", boom)
        t0 = time.monotonic()
        assert hl_client.wait_for_ws_user_fills(0.05) is False
        assert time.monotonic() - t0 >= 0.04


# ---------------------------------------------------------------------------
# Failure / race path — clear-before-drain must lose no wake
# ---------------------------------------------------------------------------

class TestFillWakeRaces:
    def test_fill_after_drain_keeps_event_set(self, ws: HyperliquidWebSocket) -> None:
        """The documented no-loss race: put happens after the drain's final
        Empty observation (simulated directly). clear() already ran, the
        fill is enqueued, then set() fires → the Event stays set and the
        next wait returns immediately with the fill intact."""
        # Simulate the main thread's drain re-arm point...
        ws._fills_wake.clear()
        # ...then a producer races in AFTER the drain loop observed Empty.
        ws._fills_queue.put_nowait({"tid": "tid-race", "coin": "BTC"})
        ws._fills_wake.set()

        assert ws.wait_for_fills(0) is True
        assert ws.wait_for_fills(0.05) is True  # a real wait would also fire
        fills = ws.drain_user_fills()
        assert len(fills) == 1
        assert fills[0]["tid"] == "tid-race"

    def test_spurious_wake_drains_empty_without_error(self, ws: HyperliquidWebSocket) -> None:
        """set() lands after clear() but the item was already caught →
        the next wake drains an empty queue (cheap no-op, never raises)."""
        ws._on_user_fills(_fill_frame("tid-spur"))
        assert ws.wait_for_fills(0) is True
        # Drain catches the fill AND re-arms (clear). Then a late set()
        # arrives for the already-consumed fill.
        fills = ws.drain_user_fills()
        assert len(fills) == 1
        ws._fills_wake.set()  # late/duplicate signal

        assert ws.wait_for_fills(0.02) is True  # spurious immediate wake
        assert ws.drain_user_fills() == []       # empty drain is safe

    def test_concurrent_producers_lose_no_fill(self, ws: HyperliquidWebSocket) -> None:
        """Hammer the callback from multiple threads while a consumer loop
        waits/drains; every unique tid must be delivered exactly once."""
        n_producers = 4
        per_producer = 25
        stop = threading.Event()
        delivered: list[str] = []
        delivered_lock = threading.Lock()

        def producer(pid: int) -> None:
            for i in range(per_producer):
                ws._on_user_fills(_fill_frame(f"tid-p{pid}-{i}"))
                time.sleep(0.001)

        def consumer() -> None:
            while not stop.is_set():
                if ws.wait_for_fills(0.05):
                    for f in ws.drain_user_fills():
                        with delivered_lock:
                            delivered.append(f["tid"])

        threads = [threading.Thread(target=producer, args=(p,), daemon=True)
                   for p in range(n_producers)]
        cthread = threading.Thread(target=consumer, daemon=True)
        cthread.start()
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5.0)
        # Allow the consumer to catch the tail.
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            with delivered_lock:
                if len(delivered) >= n_producers * per_producer:
                    break
            time.sleep(0.02)
        # Final drain for anything still queued.
        for f in ws.drain_user_fills():
            with delivered_lock:
                delivered.append(f["tid"])
        stop.set()
        cthread.join(timeout=2.0)

        expected = {f"tid-p{p}-{i}"
                    for p in range(n_producers)
                    for i in range(per_producer)}
        assert set(delivered) == expected
        assert len(delivered) == len(expected), (
            f"lost/duplicate fills: got {len(delivered)}, "
            f"expected {len(expected)}"
        )

    def test_replayed_duplicate_tid_enqueued_once(self, ws: HyperliquidWebSocket) -> None:
        """A reconnect replay of the same tid is deduped: one enqueue, one
        fill drained; the wake semantics are unaffected by the drop."""
        ws._on_user_fills(_fill_frame("tid-dup"))
        ws.drain_user_fills()  # consume + re-arm
        ws._fills_wake.clear()

        # Replay of the same fill (as HL sends a small window on reconnect).
        ws._on_user_fills(_fill_frame("tid-dup"))
        assert ws.drain_user_fills() == []
        # The deduped frame did NOT set the wake (put is skipped before set).
        assert ws._fills_wake.is_set() is False
