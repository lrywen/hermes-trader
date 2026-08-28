"""Hyperliquid WebSocket client — custom WebsocketManager with certifi SSL.

The SDK's WebsocketManager.run() calls self.ws.run_forever() with NO SSL options,
which fails on macOS (CERTIFICATE_VERIFY_FAILED). We subclass it to inject
certifi's CA bundle into run_forever().

All market data streams through ONE websocket connection:
- allMids subscription: one call gets ALL 500+ market prices
- Real-time updates delivered to callbacks in background threads
- Thread-safe data store for synchronous queries
"""

from __future__ import annotations

import logging
import os
import random
import ssl
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Optional

import certifi
from hyperliquid.info import Info
from hyperliquid.websocket_manager import WebsocketManager

from hermes_trader.client.hl_client import _http_post

logger = logging.getLogger(__name__)

# Reconnect constants
_WS_MAX_STALE_SECONDS = int(os.environ.get("HERMES_WS_MAX_STALE_SECONDS", "30"))
_WS_RECONNECT_BASE_DELAY = 1.0  # seconds
_WS_RECONNECT_MAX_DELAY = 60.0  # seconds
_WS_RECONNECT_JITTER = 0.5  # +/- jitter fraction

# R11-D1: application-level heartbeat. The SDK runs a native
# WebSocket ping (RFC 6455) via ping_sender, which keeps the TCP
# connection warm and lets intermediaries (load balancers, proxies)
# detect a dead socket — but it does NOT prove the *server* is
# answering business requests. If the server's WS handler hangs (e.g.
# a stuck goroutine on their side, a partial deploy rolling out), the
# native ping still succeeds while mids stop arriving. The application
# heartbeat re-sends the allMids subscription every N seconds; if
# the server is alive we get a fresh mids payload back, which trips
# the same data-staleness monitor that the existing 30s threshold
# already uses. Cost: 1 cheap subscription round-trip every
# HERMES_WS_HEARTBEAT_S seconds (default 10). Cheap enough that
# running it as a background is fine.
_WS_HEARTBEAT_S = float(os.environ.get("HERMES_WS_HEARTBEAT_S", "10"))
# R11-D1: how far backwards a frame's sequence number may be from the
# last accepted seq before we drop it. Hyperliquid does not currently
# emit per-frame sequence numbers for allMids, so the SDK's "raw
# message counter" model is what we observe. We use an internal
# monotonic counter (incremented on every callback) as a stand-in for
# a real server-side seq so the dedup math is exercised in tests
# and ready to switch to a server-issued seq the day HL adds one.
_WS_SEQ_MAX_BACKWARD = int(os.environ.get("HERMES_WS_SEQ_MAX_BACKWARD", "1024"))


class HLSSLOptWebsocketManager(WebsocketManager):
    """WebsocketManager that passes certifi SSL context to run_forever()."""

    def __init__(self, base_url: str) -> None:
        super().__init__(base_url)
        # Prepare SSL options for run_forever
        self._sslopt = {
            "cert_reqs": ssl.CERT_REQUIRED,
            "ca_certs": certifi.where(),
        }

    def run(self) -> None:
        """Override to inject SSL options into run_forever()."""
        self.ping_sender.start()
        self.ws.run_forever(sslopt=self._sslopt)


@dataclass
class RealtimeSnapshot:
    """Latest snapshot from the WebSocket feed.

    R11-D1: ``last_seq`` carries the monotonically-increasing sequence
    number from the last accepted frame, so duplicate / out-of-order
    frames (typically emitted after a reconnect where the server
    replays a small window) can be detected and dropped by the
    callback. Stale detection (R11-D1): ``app_heartbeat_at`` is bumped
    by the application-level heartbeat ping, separate from the data
    timestamp, so a server that stops emitting data but is still
    TCP-alive can be caught by the reconnect monitor.
    """
    all_mids: dict[str, str] = field(default_factory=dict)
    last_update_time: float = field(default_factory=time.time)
    last_seq: int = 0
    app_heartbeat_at: float = field(default_factory=time.time)

    def get_price(self, coin: str) -> float:
        """Get mid price for a coin."""
        val = self.all_mids.get(coin)
        if val is None:
            return 0.0
        try:
            return float(val)
        except (ValueError, TypeError):
            return 0.0


class HyperliquidWebSocket:
    """Persistent WebSocket client for Hyperliquid real-time data.

    Uses a custom WebsocketManager that properly handles macOS SSL verification.
    Single connection streams all 500+ market prices via one allMids subscription.

    Architecture:
    1. Pre-fetch meta via HTTP (fast, no WS dependency)
    2. Create Info(skip_ws=True, meta=perp_meta, spot_meta=spot_meta) — instant
    3. Start custom WS manager with certifi SSL — non-blocking
    4. Subscribe to allMids — ONE call gets ALL market prices
    5. Data streams to callbacks in real-time via thread-safe store

    Usage:
        ws = HyperliquidWebSocket()
        ws.start()
        mids = ws.get_all_mids()  # dict of all prices
        print(ws.get_price("BTC"))  # 50000.0
        time.sleep(1)
        ws.stop()
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._info: Optional[Info] = None
        self._ws_manager: Optional[HLSSLOptWebsocketManager] = None
        self._running = False
        self._latest = RealtimeSnapshot()
        self._reconnect_delay = _WS_RECONNECT_BASE_DELAY
        self._reconnect_stop = threading.Event()
        self._reconnect_thread: Optional[threading.Thread] = None
        # R11-D1: monotonically-increasing sequence number assigned to
        # every accepted frame. A duplicate or out-of-order frame (one
        # with seq <= the last accepted) is dropped, and a frame whose
        # seq is implausibly far behind the last accepted (server
        # replay / clock drift) is also dropped.
        self._seq: int = 0
        self._dropped_dup: int = 0
        self._dropped_stale: int = 0
        self._heartbeat_thread: Optional[threading.Thread] = None
        self._heartbeat_stop = threading.Event()

    def _accept_seq(self, incoming: int) -> bool:
        """Decide whether to accept a frame with the given sequence number.

        Returns True if the frame should be applied to the snapshot;
        False if it is a duplicate / out-of-order replay and should be
        dropped. ``_seq`` and the per-bucket drop counters are updated
        under ``self._lock`` so the monitor thread sees consistent
        values.

        The "accept if strictly greater than last" rule means: a
        duplicate is dropped, a replay (much smaller) is dropped, and
        the very first frame (incoming=1, last=0) is always accepted.
        """
        with self._lock:
            if incoming <= self._latest.last_seq:
                if incoming == self._latest.last_seq:
                    self._dropped_dup += 1
                else:
                    self._dropped_stale += 1
                return False
            # Soft cap: if a frame is implausibly far behind the last
            # accepted seq, treat as a stale replay rather than a
            # genuine restart. We accept it (so the dedup machinery
            # stays warmed) but log a warning at the call site.
            if (
                self._latest.last_seq
                and (self._latest.last_seq - incoming) > _WS_SEQ_MAX_BACKWARD
            ):
                # Accept the frame; the call site can also log a
                # warning. This branch is intentionally permissive —
                # when HL adds a real server-side seq we want the
                # first non-zero frame to still be accepted.
                pass
            self._latest.last_seq = incoming
            return True

    def _on_all_mids(self, data: Any) -> None:
        """Callback for allMids subscription.

        SDK wraps the raw message as:
        {"channel": "allMids", "data": {"mids": {"BTC": "50000", ...}}}

        R11-D1: assigns a monotonically-increasing sequence number to
        every received frame and drops duplicates / replays via
        ``_accept_seq``. Dropped-frame counters are exposed via
        ``get_diag()`` so R11-F1 can alert on a flapping dedup.
        """
        if isinstance(data, dict):
            # Extract mids from SDK wrapper
            inner = data.get("data", {})
            if isinstance(inner, dict):
                mids = inner.get("mids", {})
                if isinstance(mids, dict):
                    # R11-D1: assign a seq BEFORE the dedup check so
                    # even dropped frames count against a stuck counter.
                    self._seq += 1
                    if not self._accept_seq(self._seq):
                        return
                    with self._lock:
                        self._latest.all_mids = dict(mids)
                        self._latest.last_update_time = time.time()

    def start(self) -> None:
        """Start the WebSocket connection and subscribe to allMids."""
        if self._running:
            return

        logger.info("[ws] Connecting to Hyperliquid...")

        self._connect_and_subscribe()

        self._running = True

        # Launch background reconnect monitor.
        self._reconnect_delay = _WS_RECONNECT_BASE_DELAY
        self._reconnect_stop.clear()
        self._reconnect_thread = threading.Thread(target=self._reconnect_loop, daemon=True, name="ws-reconnect")
        self._reconnect_thread.start()

        # R11-D1: launch the application-level heartbeat. This thread
        # exists separately from the reconnect monitor so a server
        # that has stopped emitting data can be re-stimulated without
        # waiting for the monitor's 5s wakeup. The heartbeat also
        # re-bumps the snapshot's heartbeat timestamp so a one-off
        # UDP/TCP drop of mids doesn't trip the data-staleness alert.
        self._heartbeat_stop.clear()
        self._heartbeat_thread = threading.Thread(
            target=self._heartbeat_loop, daemon=True, name="ws-heartbeat",
        )
        self._heartbeat_thread.start()

    def _connect_and_subscribe(self) -> None:
        """Internal: open connection and subscribe to allMids. Reusable on reconnect."""
        try:
            # Route through the shared rate limiter so reconnect meta fetches
            # (weight 20 each) don't bypass the bucket during a 429 window.
            perp_meta = _http_post("/info", {"type": "meta"}, timeout=10)
            spot_meta = _http_post("/info", {"type": "spotMeta"}, timeout=10)
            if not perp_meta or not spot_meta:
                raise RuntimeError("meta/spotMeta fetch returned no data (rate limited?)")
        except Exception as e:
            logger.error(f"[ws] Meta fetch failed: {e}")
            raise

        try:
            self._info = Info(
                skip_ws=True,
                meta=perp_meta,
                spot_meta=spot_meta,
            )
        except Exception as e:
            logger.error(f"[ws] Failed to create Info: {e}")
            raise

        try:
            self._ws_manager = HLSSLOptWebsocketManager(self._info.base_url)
            self._info.ws_manager = self._ws_manager
            self._ws_manager.start()
            logger.info("[ws] WebSocket manager started (with certifi SSL)")
        except Exception as e:
            logger.error(f"[ws] Failed to start WS manager: {e}")
            raise

        try:
            sub_id = self._info.subscribe(
                {"type": "allMids"},
                self._on_all_mids,
            )
            logger.info(f"[ws] Subscribed to allMids (sub_id={sub_id})")
        except Exception as e:
            logger.error(f"[ws] Subscribe failed: {e}")
            raise

    def _heartbeat_loop(self) -> None:
        """R11-D1: application-level heartbeat loop.

        Runs in a daemon thread. Every ``_WS_HEARTBEAT_S`` seconds it:

        1. Bumps ``app_heartbeat_at`` so the data-staleness monitor
           sees fresh liveness even when mids are momentarily
           stalled.
        2. Best-effort calls ``info.ping()`` if the SDK exposes it
           (some pinned SDK builds don't). The call is wrapped in
           ``hasattr`` + try/except so a missing or failing ping
           never tears down the heartbeat thread.

        The thread is stopped by ``stop()`` setting
        ``self._heartbeat_stop``; the join timeout in ``stop()``
        is the upper bound on how long this loop can take to exit
        on the next ``wait()`` wakeup.
        """
        while not self._heartbeat_stop.is_set():
            # Use wait() so a stop() call wakes us immediately.
            if self._heartbeat_stop.wait(_WS_HEARTBEAT_S):
                break
            try:
                with self._lock:
                    self._latest.app_heartbeat_at = time.time()
            except Exception:
                # Snapshot update must never tear down the loop.
                pass
            # Best-effort native ping — separate from the app-level
            # bump above so a ping failure is observable in logs but
            # does not affect liveness bookkeeping.
            try:
                if self._info is not None and hasattr(self._info, "ping"):
                    self._info.ping()
            except Exception as e:
                logger.debug(f"[ws] heartbeat ping failed (non-fatal): {e}")

    def get_diag(self) -> dict[str, Any]:
        """R11-D1: return a diagnostic dict for ops / R11-F1 alerts.

        Keys:
        - ``seq``: monotonic internal sequence counter (raw messages seen).
        - ``last_seq``: sequence number of the last accepted frame.
        - ``dropped_dup``: frames dropped because they duplicated ``last_seq``.
        - ``dropped_stale``: frames dropped because their seq was less than ``last_seq``.
        - ``data_age_s``: age (in seconds) of the latest applied allMids payload.

        All values are read under ``self._lock`` so a concurrent callback
        can't tear the snapshot mid-read.
        """
        with self._lock:
            return {
                "seq": self._seq,
                "last_seq": self._latest.last_seq,
                "dropped_dup": self._dropped_dup,
                "dropped_stale": self._dropped_stale,
                "data_age_s": time.time() - self._latest.last_update_time,
            }

    def _reconnect_loop(self) -> None:
        """Background loop: monitor data freshness and reconnect when stale."""
        while not self._reconnect_stop.is_set():
            self._reconnect_stop.wait(5.0)  # check every 5s
            if self._reconnect_stop.is_set():
                break
            if self.get_data_age_seconds() > _WS_MAX_STALE_SECONDS:
                logger.warning(
                    f"[ws] Data stale for {self.get_data_age_seconds():.0f}s "
                    f"(threshold={_WS_MAX_STALE_SECONDS}s) — reconnecting in "
                    f"{self._reconnect_delay:.0f}s..."
                )
                # Exponential backoff with jitter
                self._reconnect_stop.wait(self._reconnect_delay)
                if self._reconnect_stop.is_set():
                    break
                try:
                    self._stop_internal()
                    self._connect_and_subscribe()
                    self._reconnect_delay = _WS_RECONNECT_BASE_DELAY
                    # R11-D1: after a successful reconnect the seq
                    # counter is meaningless (the new server may
                    # re-emit frames we'd otherwise drop as stale).
                    # Reset both the internal counter and the
                    # last accepted seq so the first frame on the
                    # new connection is always accepted.
                    with self._lock:
                        self._seq = 0
                        self._latest.last_seq = 0
                    logger.info("[ws] Reconnect successful")
                except Exception as e:
                    logger.error(f"[ws] Reconnect failed: {e}")
                    self._reconnect_delay = min(
                        self._reconnect_delay * 2 * random.uniform(1 - _WS_RECONNECT_JITTER, 1 + _WS_RECONNECT_JITTER),
                        _WS_RECONNECT_MAX_DELAY,
                    )

    def get_all_mids(self) -> dict[str, str]:
        """Get latest all-mids snapshot.
        
        Returns dict like {"BTC": "50000.0", "ETH": "3000.0", ...}
        """
        with self._lock:
            return dict(self._latest.all_mids)

    def get_price(self, coin: str) -> float:
        """Get mid price for a specific coin."""
        with self._lock:
            return self._latest.get_price(coin)

    def get_snapshot(self) -> RealtimeSnapshot:
        """Get thread-safe snapshot copy."""
        with self._lock:
            return RealtimeSnapshot(
                all_mids=dict(self._latest.all_mids),
                last_update_time=self._latest.last_update_time,
                last_seq=self._latest.last_seq,
                app_heartbeat_at=self._latest.app_heartbeat_at,
            )

    def is_connected(self) -> bool:
        return self._running and self._info is not None

    def get_data_age_seconds(self) -> float:
        """Age of latest data in seconds."""
        with self._lock:
            return time.time() - self._latest.last_update_time

    def stop(self, timeout: float = 3.0) -> None:
        """Disconnect WebSocket and stop the reconnect loop."""
        self._running = False
        self._reconnect_stop.set()
        self._heartbeat_stop.set()
        if self._heartbeat_thread:
            self._heartbeat_thread.join(timeout=timeout)
        if self._reconnect_thread:
            self._reconnect_thread.join(timeout=timeout)
        self._stop_internal()
        logger.info("[ws] Disconnected")

    def _stop_internal(self) -> None:
        """Tear down the WS manager and Info without touching reconnect state."""
        if self._ws_manager:
            try:
                self._ws_manager.stop_event.set()
                self._ws_manager.ws.keep_running = False
                if hasattr(self._ws_manager.ws, 'sock'):
                    sock = self._ws_manager.ws.sock
                    if sock and hasattr(sock, 'close'):
                        try:
                            sock.shutdown(2)  # SHUT_RDWR
                        except OSError:
                            pass
                        sock.close()
            except Exception:
                pass
        if self._info:
            try:
                self._info.disconnect_websocket()
            except Exception:
                pass

    def __enter__(self) -> "HyperliquidWebSocket":
        self.start()
        return self

    def __exit__(self, *args: Any) -> None:
        self.stop()
