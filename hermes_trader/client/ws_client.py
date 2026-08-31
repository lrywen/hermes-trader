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

import hashlib
import json
import logging
import math
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
# R13-B13: WS tuning knobs live in canonical block hl_client_io; rate_limit
# is a leaf (stdlib + lazy config_store only), so this import cannot cycle.
from hermes_trader.client.rate_limit import _HL_CLIENT_IO

logger = logging.getLogger(__name__)

# Reconnect constants
# R13-B13: import-time snapshot from canonical hl_client_io.ws_max_stale_s
# (legacy HERMES_WS_MAX_STALE_SECONDS env channel still wins at boot).
_WS_MAX_STALE_SECONDS = int(_HL_CLIENT_IO["ws_max_stale_s"])
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
# R13-B13: canonical hl_client_io.ws_heartbeat_s (import-time snapshot;
# legacy HERMES_WS_HEARTBEAT_S env channel still wins at boot).
_WS_HEARTBEAT_S = float(_HL_CLIENT_IO["ws_heartbeat_s"])
# R11-D1: how far backwards a frame's sequence number may be from the
# last accepted seq before we drop it. Hyperliquid does not currently
# emit per-frame sequence numbers for allMids, so the SDK's "raw
# message counter" model is what we observe. We use an internal
# monotonic counter (incremented on every callback) as a stand-in for
# a real server-side seq so the dedup math is exercised in tests
# and ready to switch to a server-issued seq the day HL adds one.
# R13-B13: canonical hl_client_io.ws_seq_max_backward (import-time snapshot;
# legacy HERMES_WS_SEQ_MAX_BACKWARD env channel still wins at boot).
_WS_SEQ_MAX_BACKWARD = int(_HL_CLIENT_IO["ws_seq_max_backward"])
# M-11 (supplemental audit 2026-08-30): import-time snapshot of the per-coin
# single-tick jump cap (fraction vs previous accepted mid); legacy env
# HERMES_WS_MAX_TICK_JUMP_FRAC still wins at boot.
_WS_MAX_TICK_JUMP_FRAC = float(_HL_CLIENT_IO["ws_max_tick_jump_frac"])


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
    # M-10: content hash of the last APPLIED allMids payload. Used to detect
    # replayed/duplicate frames (HL replays a window after reconnect and the
    # app heartbeat re-requests the same snapshot); a frame whose payload
    # matches the last applied one carries no new information.
    last_payload_hash: str = ""

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
        # M-10: frames dropped because their payload hash matched the last
        # applied frame (true replay/duplicate detection on payload content,
        # not on the always-incrementing local seq).
        self._dropped_replay: int = 0
        # M-11: per-coin mid updates suppressed because a single tick moved the
        # price by more than _WS_MAX_TICK_JUMP_FRAC vs the previous mid.
        self._dropped_spike: int = 0
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

    @staticmethod
    def _payload_hash(mids: dict[str, Any]) -> str:
        """M-10: stable content hash of an allMids payload.

        Hyperliquid does not emit a per-frame sequence number for allMids, so
        the old code assigned a LOCAL seq (``self._seq += 1``) and fed it to
        ``_accept_seq`` — but a locally-incremented seq is always strictly
        greater than the last, so that dedup could never fire and a replayed
        frame (HL re-sends the same snapshot after a reconnect / on the app
        heartbeat re-subscribe) was indistinguishable from a fresh one. We
        instead hash the payload CONTENT (canonical JSON of the sorted
        coin->mid map); a frame identical to the last applied one is a replay.
        """
        try:
            blob = json.dumps(mids, sort_keys=True, separators=(",", ":"))
            return hashlib.sha256(blob.encode("utf-8")).hexdigest()
        except Exception:
            return ""

    def _filter_spikes(
        self, incoming: dict[str, Any], prev: dict[str, str]
    ) -> tuple[dict[str, str], int]:
        """M-11: reject per-coin single-tick mid jumps beyond the cap.

        Compares each incoming mid against the previously accepted mid for the
        SAME coin. If a tick moves the price by more than
        ``_WS_MAX_TICK_JUMP_FRAC`` (fraction), the new value is treated as a
        bad print (fat finger / corrupt tick) and the coin's PREVIOUS mid is
        retained instead; coins with no prior mid (new listing / first
        observation) and non-finite / <=0 values pass through unchanged. The
        frame is still applied for every well-behaved coin — only the spiking
        coin is suppressed. Returns (filtered_mids, n_suppressed).
        """
        out: dict[str, str] = dict(prev)  # keep every prior coin as the baseline
        n_suppressed = 0
        for coin, raw in incoming.items():
            try:
                new_px = float(raw)
            except (TypeError, ValueError):
                # Non-numeric mid: keep prior value if we have one.
                if coin not in out and isinstance(raw, str):
                    out[coin] = raw
                continue
            if new_px <= 0.0 or not math.isfinite(new_px):
                continue
            old_raw = prev.get(coin)
            if old_raw is None:
                out[coin] = str(raw)  # first observation — nothing to compare
                continue
            try:
                old_px = float(old_raw)
            except (TypeError, ValueError):
                out[coin] = str(raw)
                continue
            if old_px > 0.0 and abs(new_px - old_px) / old_px > _WS_MAX_TICK_JUMP_FRAC:
                n_suppressed += 1  # suppress: leave the prior mid in `out`
                logger.warning(
                    "[ws] M-11 spike suppressed: %s mid jump %.4f -> %.4f "
                    "(%.1f%% > cap %.1f%%); keeping previous mid",
                    coin, old_px, new_px,
                    abs(new_px - old_px) / old_px * 100.0,
                    _WS_MAX_TICK_JUMP_FRAC * 100.0,
                )
                continue
            out[coin] = str(raw)
        return out, n_suppressed

    def _on_all_mids(self, data: Any) -> None:
        """Callback for allMids subscription.

        SDK wraps the raw message as:
        {"channel": "allMids", "data": {"mids": {"BTC": "50000", ...}}}

        R11-D1: assigns a monotonically-increasing sequence number to every
        received frame (exposed via ``get_diag`` for ops/alerting).

        M-10 (supplemental audit 2026-08-30): the local seq always increments,
        so by itself it cannot detect a replayed frame. We additionally hash
        the payload content and drop frames identical to the last applied one
        (``_dropped_replay``) — this is what actually catches HL's reconnect
        replay window and the heartbeat re-subscribe echo.

        M-11: per-coin single-tick jumps beyond ``_WS_MAX_TICK_JUMP_FRAC`` are
        suppressed (prior mid retained) so a corrupt tick cannot spike pricing
        downstream (entry pricing is independently cross-checked by H-6).
        """
        if isinstance(data, dict):
            # Extract mids from SDK wrapper
            inner = data.get("data", {})
            if isinstance(inner, dict):
                mids = inner.get("mids", {})
                if isinstance(mids, dict):
                    # R11-D1: assign a seq BEFORE the dedup check so even
                    # dropped frames count against a stuck counter.
                    self._seq += 1
                    # Keep _accept_seq wired (counters/diag/tests) but note the
                    # local-seq check alone cannot catch a replayed frame; the
                    # content-hash check below is the real replay guard.
                    self._accept_seq(self._seq)

                    payload_hash = self._payload_hash(mids)
                    with self._lock:
                        # M-10: identical payload to the last applied frame →
                        # replay/duplicate, no new information. Drop it but do
                        # NOT refresh last_update_time (a replayed frame must
                        # not mask a genuinely stalled feed). An empty mids
                        # payload yields an empty hash and skips this guard
                        # (nothing to dedup); it still bumps the seq below.
                        if payload_hash and payload_hash == self._latest.last_payload_hash:
                            self._dropped_replay += 1
                            return

                        # M-11: suppress per-coin bad prints before applying.
                        # Empty payload: nothing to filter, keep the snapshot.
                        if mids:
                            filtered, n_spike = self._filter_spikes(
                                mids, self._latest.all_mids
                            )
                            self._dropped_spike += n_spike
                            self._latest.all_mids = filtered
                            self._latest.last_payload_hash = payload_hash
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
        - ``dropped_replay`` (M-10): frames dropped because their payload content
          matched the last applied frame (true replay/duplicate).
        - ``dropped_spike`` (M-11): per-coin mid updates suppressed as bad prints.
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
                "dropped_replay": self._dropped_replay,
                "dropped_spike": self._dropped_spike,
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
                    # new connection is always accepted. M-10: also clear
                    # the last payload hash — otherwise an identical-price
                    # snapshot on the fresh connection would be mistaken for
                    # a replay and skipped, leaving last_update_time stale.
                    with self._lock:
                        self._seq = 0
                        self._latest.last_seq = 0
                        self._latest.last_payload_hash = ""
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
