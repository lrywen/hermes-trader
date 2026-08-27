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
from typing import Any, Dict, Optional

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


class HLSSLOptWebsocketManager(WebsocketManager):
    """WebsocketManager that passes certifi SSL context to run_forever()."""

    def __init__(self, base_url: str):
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
    """Latest snapshot from the WebSocket feed."""
    all_mids: Dict[str, str] = field(default_factory=dict)
    last_update_time: float = field(default_factory=time.time)

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

    def __init__(self):
        self._lock = threading.Lock()
        self._info: Optional[Info] = None
        self._ws_manager: Optional[HLSSLOptWebsocketManager] = None
        self._running = False
        self._latest = RealtimeSnapshot()
        self._reconnect_delay = _WS_RECONNECT_BASE_DELAY
        self._reconnect_stop = threading.Event()
        self._reconnect_thread: Optional[threading.Thread] = None

    def _on_all_mids(self, data: Any) -> None:
        """Callback for allMids subscription.
        
        SDK wraps the raw message as:
        {"channel": "allMids", "data": {"mids": {"BTC": "50000", ...}}}
        """
        if isinstance(data, dict):
            # Extract mids from SDK wrapper
            inner = data.get("data", {})
            if isinstance(inner, dict):
                mids = inner.get("mids", {})
                if isinstance(mids, dict):
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
                    logger.info("[ws] Reconnect successful")
                except Exception as e:
                    logger.error(f"[ws] Reconnect failed: {e}")
                    self._reconnect_delay = min(
                        self._reconnect_delay * 2 * random.uniform(1 - _WS_RECONNECT_JITTER, 1 + _WS_RECONNECT_JITTER),
                        _WS_RECONNECT_MAX_DELAY,
                    )

    def get_all_mids(self) -> Dict[str, str]:
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

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *args):
        self.stop()
