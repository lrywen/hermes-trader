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
import queue
import random
import ssl
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Optional

import certifi
from hyperliquid.info import Info
from hyperliquid.websocket_manager import WebsocketManager

from hermes_trader.agents import atomic_io
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
# P1-6 (2026-09-02): M-11 spike-suppression WARNING rate limiting. A single
# bad-print coin fires the suppress path on EVERY allMids frame (HL pushes ~1
# frame/5s), so the unconditional per-tick WARNING flooded the log (observed
# 117k+ identical lines). The suppression BEHAVIOR is unchanged (prior mid is
# still kept every time); only the log volume is throttled:
#   - the detailed per-coin WARNING logs only ONCE per coin per process (the
#     first time that coin spikes) — enough to surface a NEW bad feed;
#   - every _WS_SPIKE_WARN_INTERVAL_S a single aggregate SUMMARY reports the
#     total suppressed ticks and affected coins, so an ongoing feed problem
#     still surfaces without per-frame spam.
# Import-time snapshot; legacy env channel still wins at boot. Default 30 min
# keeps a continuously-spiking feed at ~48 summary lines/day plus one line per
# newly-bad coin — comfortably under the 100 lines/day target.
_WS_SPIKE_WARN_INTERVAL_S = float(
    os.environ.get("HERMES_WS_SPIKE_WARN_INTERVAL_S", "1800")
)

# WS userFills: dedup-set persistence + historical-replay filtering.
#
# Hyperliquid replays a backlog window of recent fills EVERY time a
# ``userFills`` subscription is (re)established — including on every
# process restart / container rebuild. The in-memory ``_seen_tids`` dedup
# set alone cannot survive a restart, so the same historical closes get
# re-enqueued and re-emitted as ``ws_user_fill`` SSE events on every boot
# (observed: 16 real Aug fills fanned out into 240 events across ~15
# restarts). Two complementary guards:
#
#   1. Persist seen tids to disk (atomic replace, throttled) and reload on
#      startup, so a fill already reported in a previous process is never
#      reported again.
#   2. A subscription watermark: any fill whose HL ``time`` is older than
#      the FIRST successful subscribe in this process (minus a small clock
#      skew / subscription-latency grace) is treated as historical replay
#      and dropped — even before the dedup check. The watermark is set
#      once and is NOT reset on a reconnect, so a fill that genuinely
#      happened during a brief disconnect (newer than first subscribe)
#      still passes through to dedup and is processed once.
_USER_FILLS_SEEN_FILE = os.environ.get(
    "HERMES_WS_USER_FILLS_SEEN_FILE",
    os.path.join(os.environ.get("HERMES_DATA_DIR", "/data"), ".ws-user-fills-seen.json"),
)
_USER_FILLS_PERSIST_MIN_INTERVAL_S = 5.0
_USER_FILLS_HIST_GRACE_MS = 30_000  # clock-skew / subscribe-latency slack


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
        # 2026-09-02 (P3)：run_forever 返回意味着底层 socket 已关闭（断线）。
        # 此前此处静默返回、SDK 线程退出，生产环境只能靠"数据陈旧"间接发现
        # 断连。现在记录关闭码/原因，供排障定位断连来源。
        _close = getattr(self.ws, "last_close", None)
        logger.info(
            "[ws] socket closed, run_forever exited (close_code=%s, reason=%r)",
            getattr(_close, "status_code", None) if _close is not None else None,
            getattr(_close, "reason", None) if _close is not None else None,
        )


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
        # 2026-09-02 (P3)：连续重连尝试计数（成功后归零），用于日志展示
        # "第 N 次重连/重连失败"，排障时量化断连严重程度。
        self._reconnect_attempts = 0
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
        # P1-6: rate-limit state for the M-11 suppression WARNING. All fields
        # are touched only from _on_all_mids -> _filter_spikes, which runs
        # under self._lock, so no separate lock is needed.
        # coins that have already emitted a first-time detailed WARNING this
        # process (a persistently bad-print coin re-spikes every frame, so its
        # detailed line logs only ONCE; ongoing activity is covered by summary).
        self._spike_warned_coins: set[str] = set()
        # Window-accumulated stats for the aggregate summary.
        self._spike_suppressed_window: int = 0
        self._spike_coins_window: set[str] = set()
        self._spike_summary_at: float = 0.0
        self._heartbeat_thread: Optional[threading.Thread] = None
        self._heartbeat_stop = threading.Event()
        # Phase 1 (WS user-fills feasibility): persistent subscription
        # state so a reconnect re-subscribes automatically. ``_user_fills_user``
        # is the wallet address we're subscribed to (or None when not
        # subscribed). ``_user_fills_sub_id`` is the SDK subscription id
        # returned by ``info.subscribe``; used only for diagnostics — the
        # SDK's own subscription registry is the source of truth and is
        # torn down by ``info.disconnect_websocket`` on stop.
        # Callback is INTENTIONALLY log-only in Phase 1: no decision
        # integration, no shared-state mutation, so a misbehaving feed
        # cannot affect trading. Phase 2 will route fills to a queue.
        self._user_fills_user: Optional[str] = None
        self._user_fills_sub_id: Optional[int] = None
        self._user_fills_count: int = 0
        # Phase 2: thread-safe queue + dedup set so the WS callback
        # thread can hand fills to the main trading loop without data
        # races. ``_fills_queue`` is the handoff channel (queue.Queue
        # is internally synchronized so put_nowait / get_nowait are
        # safe across threads). ``_seen_tids`` is a bounded dedup set
        # so a fill replayed after a reconnect (HL re-sends a small
        # window) is dropped instead of re-processed. Both fields are
        # touched ONLY from inside ``_on_user_fills`` (put) and
        # ``drain_user_fills`` (get) — no other access paths exist.
        # ``_seen_tids_lock`` guards the dedup set; the queue itself
        # needs no extra lock.
        self._fills_queue: "queue.Queue[dict[str, Any]]" = queue.Queue()
        self._seen_tids: set[str] = set()
        self._seen_tids_lock = threading.Lock()
        # Phase 4 (low-latency wake): set by the WS callback thread right
        # after a fill is enqueued, waited on by the MAIN thread between
        # cycles/batches. threading.Event is internally synchronized, so
        # the callback thread still touches NOTHING but this Event + the
        # queue + dedup set (single-writer rule preserved: the main thread
        # is still the only writer of the session log / SSE).
        self._fills_wake = threading.Event()
        # Soft cap on the dedup set so a long-running session doesn't
        # grow it unboundedly. 10k entries ~ 1MB; well below concern.
        # When exceeded we drop the oldest half (set is unordered so
        # we rebuild from the queue's not-yet-drained state).
        self._seen_tids_cap: int = 10000
        # Dedup-set persistence + historical-replay watermark (see the
        # module-level comment above). ``_user_fills_first_sub_at`` is the
        # epoch-seconds of the FIRST successful userFills subscribe in this
        # process; 0.0 means "never subscribed" and disables the watermark
        # filter (so direct-callback unit tests are unaffected). It is set
        # once and deliberately NOT reset on reconnect.
        self._user_fills_first_sub_at: float = 0.0
        self._user_fills_seen_dirty: bool = False
        self._user_fills_last_persist: float = 0.0
        self._user_fills_hist_dropped: int = 0
        # Warm the in-memory dedup set from the on-disk snapshot so a fill
        # already reported by a previous process is never re-emitted.
        self._load_seen_tids()

    def _load_seen_tids(self) -> None:
        """Best-effort load of persisted seen-tids into the dedup set.

        Missing / corrupt / unreadable files are ignored: the watermark
        filter and in-memory dedup still work, only cross-process dedup is
        lost. Runs under ``_seen_tids_lock`` so it is safe versus the WS
        callback thread (in practice it runs from ``__init__`` before any
        subscription exists).
        """
        try:
            with open(_USER_FILLS_SEEN_FILE, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            tids = data.get("tids") if isinstance(data, dict) else None
            if not isinstance(tids, list):
                return
            with self._seen_tids_lock:
                for tid in tids:
                    if isinstance(tid, str) and tid:
                        self._seen_tids.add(tid)
            logger.info(
                "[ws:user-fills] loaded %d persisted seen tids from %s",
                len(self._seen_tids), _USER_FILLS_SEEN_FILE,
            )
        except FileNotFoundError:
            pass
        except Exception as exc:  # noqa: BLE001 — persistence is best-effort
            logger.warning(
                "[ws:user-fills] could not load seen-tids file %s: %s",
                _USER_FILLS_SEEN_FILE, exc,
            )

    def _persist_seen_tids(self, force: bool = False) -> None:
        """Best-effort atomic snapshot of the seen-tids set to disk.

        Throttled to at most once per ``_USER_FILLS_PERSIST_MIN_INTERVAL_S``
        unless ``force`` (used by ``stop()``). Keeps only the newest
        ``_seen_tids_cap // 2`` tids (set iteration order is insertion
        order in CPython) so the file cannot grow without bound. Any
        failure is swallowed — persistence must never break the feed.
        """
        now = time.time()
        if not force and (
            not self._user_fills_seen_dirty
            or (now - self._user_fills_last_persist) < _USER_FILLS_PERSIST_MIN_INTERVAL_S
        ):
            return
        try:
            with self._seen_tids_lock:
                tids = list(self._seen_tids)
            keep = max(100, self._seen_tids_cap // 2)
            if len(tids) > keep:
                tids = tids[-keep:]
            # Best-effort atomic rename (regenerable dedup cache → fsync=False);
            # atomic_io creates the parent dir and owns the tmp+replace.
            atomic_io.write_json_atomic(
                _USER_FILLS_SEEN_FILE,
                {"version": 1, "tids": tids},
                indent=None,
                fsync=False,
            )
            self._user_fills_seen_dirty = False
            self._user_fills_last_persist = now
        except Exception as exc:  # noqa: BLE001 — persistence is best-effort
            logger.debug(
                "[ws:user-fills] could not persist seen-tids to %s: %s",
                _USER_FILLS_SEEN_FILE, exc,
            )

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
                self._note_spike_suppressed(coin, old_px, new_px)
                continue
            out[coin] = str(raw)
        return out, n_suppressed

    def _note_spike_suppressed(
        self, coin: str, old_px: float, new_px: float
    ) -> None:
        """P1-6: record a suppressed M-11 spike with a throttled WARNING.

        Called from ``_filter_spikes`` (under ``self._lock``). The suppression
        itself has already happened (prior mid kept); this only decides what to
        LOG so a persistently bad-print coin cannot flood the log:

        - the DETAILED line (old/new price, magnitude) logs only ONCE per coin
          per process for TRADEABLE coins — enough to surface a newly-bad feed.
          HL's allMids also carries ~700 internal-index entries (``#NNNNN`` /
          ``@NNN``) for delisted/expired markets whose mids are stale garbage;
          they are never traded and constantly flip >cap, so their suppressions
          are counted in the aggregate ONLY (a detailed line per junk id would
          flood the log with entries nobody can act on);
        - every ``_WS_SPIKE_WARN_INTERVAL_S`` a single aggregate SUMMARY reports
          suppressed ticks / affected coins over the preceding window, so an
          ongoing problem remains visible at a bounded volume.
        """
        now = time.time()
        # End-of-window aggregate FIRST (covers the preceding window, excludes
        # this tick) so a continuously-spiking feed emits exactly one summary
        # per interval regardless of how many coins spike.
        if self._spike_summary_at and (
            now - self._spike_summary_at
        ) >= _WS_SPIKE_WARN_INTERVAL_S:
            logger.warning(
                "[ws] M-11 spike summary: %d suppressed ticks across %d coins "
                "in last %.0fs (jump cap %.1f%%); most recent: %s",
                self._spike_suppressed_window,
                len(self._spike_coins_window),
                _WS_SPIKE_WARN_INTERVAL_S,
                _WS_MAX_TICK_JUMP_FRAC * 100.0,
                coin,
            )
            self._spike_suppressed_window = 0
            self._spike_coins_window = set()
            self._spike_summary_at = now
        elif not self._spike_summary_at:
            self._spike_summary_at = now

        # Accumulate window stats (covers ongoing spikes between summaries).
        self._spike_suppressed_window += 1
        self._spike_coins_window.add(coin)

        # Detailed per-coin line only for a TRADEABLE coin (HL internal-index
        # entries like "#12090" / "@226" are delisted/expired markets that are
        # never traded; their garbage mids are covered by the summary above),
        # and only the FIRST time that tradeable coin spikes this process.
        is_tradeable = coin[:1] not in ("#", "@")
        if is_tradeable and coin not in self._spike_warned_coins:
            self._spike_warned_coins.add(coin)
            logger.warning(
                "[ws] M-11 spike suppressed: %s mid jump %.4f -> %.4f "
                "(%.1f%% > cap %.1f%%); keeping previous mid "
                "(further suppression of this coin logged in the periodic summary)",
                coin, old_px, new_px,
                abs(new_px - old_px) / old_px * 100.0,
                _WS_MAX_TICK_JUMP_FRAC * 100.0,
            )

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

    # ── Phase 2: userFills subscription (log + enqueue, drives exit) ────
    # The Hyperliquid ``userFills`` channel pushes a ``Fill`` (or list of
    # fills) every time the subscribed wallet's orders match. Phase 1
    # proved the subscription works (LOG ONLY). Phase 2 additionally
    # enqueues each deduped fill into ``_fills_queue`` so the trading
    # loop can drain it and trigger an immediate exit scan when a CLOSE
    # fill lands (closedPnl != 0 or dir contains "Close"), bypassing the
    # 15s scan_interval wait. This shaves the exit-polling blind window
    # during fast crashes.
    #
    # Auth: the SDK's ``info.subscribe`` accepts a plain ``user`` address
    # for ``userFills`` (no L2 agent signature required — the server
    # filters by the public address). If the server rejects the
    # subscription (e.g. requires L2 auth in a future SDK version), the
    # callback simply never fires and Phase 1's diagnostic counter stays
    # at 0; the trading loop is unaffected (REST-based monitor_exits
    # still backs us up).
    def _on_user_fills(self, data: Any) -> None:
        """Phase 2 callback for ``userFills`` subscription — log + enqueue.

        SDK wrapper shape:
            {"channel": "userFills", "data": {"fills": [ {Fill}, ... ]}}

        A ``Fill`` (per hyperliquid/utils/types.py) includes:
            tid, side ("A" buy / "B" sell), price, sz, coin, dir,
            closedPnl (str), fee (str), feeToken, hash, time, ...

        Phase 2:
        - Logs a compact one-liner per fill (Phase 1 diagnostic retained)
        - Enqueues each deduped fill into ``_fills_queue`` so the main
          trading loop can drain it and drive exit decisions without
          polling. ``tid`` is the dedup key — HL's per-fill trade id,
          stable across reconnect replays.

        Thread safety: this method runs in the SDK's WS callback thread.
        The queue is internally synchronized (queue.Queue). The dedup
        set is guarded by ``_seen_tids_lock``. No other shared state
        is touched.
        """
        try:
            self._user_fills_count += 1
            inner = data.get("data", {}) if isinstance(data, dict) else {}
            fills = inner.get("fills", []) if isinstance(inner, dict) else []
            n = len(fills) if isinstance(fills, list) else 0
            if n == 0:
                logger.info(
                    "[ws:user-fills] frame #%d user=%s n_fills=0 "
                    "(empty payload — possibly a subscription ack)",
                    self._user_fills_count,
                    self._user_fills_user,
                )
                return
            for f in fills:
                if not isinstance(f, dict):
                    continue
                coin = f.get("coin", "?")
                side = f.get("side", "?")  # "A"=buy, "B"=sell
                sz = f.get("sz", "?")
                px = f.get("px", f.get("price", "?"))
                closed_pnl = f.get("closedPnl", "0")
                f_dir = f.get("dir", "?")  # "Open"/"Close"/"Liquidation"
                t = f.get("time", 0)
                tid = f.get("tid", "")
                logger.info(
                    "[ws:user-fills] frame #%d user=%s coin=%s side=%s "
                    "sz=%s px=%s dir=%s closedPnl=%s t=%s tid=%s",
                    self._user_fills_count, self._user_fills_user,
                    coin, side, sz, px, f_dir, closed_pnl, t, tid,
                )
                # Dedup by tid. If tid is missing, fall back to a synthetic
                # key (coin+side+sz+px+t) so a non-conforming payload still
                # has SOME dedup. Without a tid we have to assume the SDK
                # always emits one; if it doesn't, the synthetic key is the
                # best we can do.
                if not tid:
                    tid = f"_synthetic:{coin}:{side}:{sz}:{px}:{t}"
                # Historical-replay guard. HL replays a backlog window of
                # recent fills on EVERY (re)subscribe — including process
                # restarts. A fill older than this process's first
                # successful subscribe (minus clock-skew grace) is replay,
                # not a live event: drop it BEFORE dedup so it is neither
                # enqueued nor added to the seen set. The watermark is set
                # once and never reset on reconnect, so a fill that truly
                # happened during a brief disconnect (newer than first
                # subscribe) still flows through dedup and is handled once.
                # Watermark == 0.0 means "never subscribed" (e.g. direct
                # callback in unit tests) → filter disabled.
                if self._user_fills_first_sub_at > 0 and isinstance(t, (int, float)) and t > 0:
                    wm_ms = int(self._user_fills_first_sub_at * 1000)
                    if t < wm_ms - _USER_FILLS_HIST_GRACE_MS:
                        self._user_fills_hist_dropped += 1
                        if self._user_fills_hist_dropped <= 3 or self._user_fills_hist_dropped % 50 == 0:
                            logger.info(
                                "[ws:user-fills] dropping historical replay "
                                "(fill_t=%s < watermark=%s, grace=%dms) "
                                "coin=%s tid=%s total_hist_dropped=%d",
                                t, wm_ms, _USER_FILLS_HIST_GRACE_MS,
                                coin, tid, self._user_fills_hist_dropped,
                            )
                        continue
                with self._seen_tids_lock:
                    if tid in self._seen_tids:
                        # Already enqueued — a replayed frame from a
                        # reconnect. Skip the put so the main loop doesn't
                        # process the same fill twice.
                        continue
                    if len(self._seen_tids) >= self._seen_tids_cap:
                        # Cap reached: drop the whole set and start fresh.
                        # Rare path (10k+ fills without a process restart).
                        # During the rebuild window a fast re-fill could
                        # double-process — acceptable at this volume.
                        logger.warning(
                            "[ws:user-fills] dedup set cap %d reached — "
                            "resetting (rare path)",
                            self._seen_tids_cap,
                        )
                        self._seen_tids.clear()
                    self._seen_tids.add(tid)
                    self._user_fills_seen_dirty = True
                # Persist the new tid (throttled; best-effort) so a restart
                # won't re-report this fill.
                self._persist_seen_tids()
                # Enqueue a NORMALIZED copy of the fill so the main loop
                # doesn't have to re-parse the raw SDK shape. We pull
                # only what the consumer needs (closedPnl/dir/side/coin/
                # sz/px/t/tid) — anything else can be re-fetched from
                # the exchange if needed downstream.
                enqueued = {
                    "coin": str(coin),
                    "side": str(side),
                    "sz": str(sz),
                    "px": str(px),
                    "dir": str(f_dir),
                    "closedPnl": str(closed_pnl),
                    "tid": str(tid),
                    "t": int(t) if isinstance(t, (int, float)) else 0,
                }
                try:
                    self._fills_queue.put_nowait(enqueued)
                    # Wake a main-thread wait_for_fills() immediately.
                    # set() is idempotent and thread-safe; the wait side
                    # clears it after draining.
                    self._fills_wake.set()
                except Exception as put_err:
                    # queue.Queue.put_nowait only raises Full if a maxsize
                    # was set — we never set one, so this is defensive.
                    logger.warning(
                        "[ws:user-fills] queue put failed (non-fatal): %s",
                        put_err,
                    )
        except Exception as e:
            # Log path must never raise into the SDK's WS thread —
            # an exception in a callback would silently kill the
            # subscription. Swallow and continue.
            logger.warning("[ws:user-fills] callback error (non-fatal): %s", e)

    def drain_user_fills(self) -> list[dict[str, Any]]:
        """Drain ALL queued user fills (NON-BLOCKING, Phase 2).

        Returns a list of normalized fill dicts (see ``_on_user_fills``
        for the shape) in FIFO order. Empty list if no fills queued.

        Called from the main trading loop at the start of each cycle
        (and from the intra-cycle ``_exit_checkpoint``), so close fills
        are reported to the dashboard at batch granularity instead of
        waiting for the next full cycle. The drain itself does NOT make
        exit decisions — exit re-evaluation is the job of the regular
        ``monitor_exits`` pass and ``_exit_checkpoint``. The drain is
        non-blocking (``get_nowait`` raises ``queue.Empty`` when the
        queue is empty) so the main loop never stalls waiting for a fill.

        Thread safety: the queue is internally synchronized; we hold
        NO lock here. The dedup set is NOT touched by drain (a tid
        stays in the dedup set even after the fill has been drained,
        so a reconnect replay of an already-processed fill is still
        suppressed). The set is bounded by ``_seen_tids_cap`` and is
        only reset by cap pressure, never by drain.
        """
        # Re-arm BEFORE draining. The callback's ordering is put_nowait()
        # THEN set(), so clearing the Event first and then draining until
        # empty is wake-loss-free: any fill whose item is NOT caught by this
        # drain (put after the final Empty observation) also signals set()
        # after this clear() → the Event stays set and the next
        # wait_for_fills() returns immediately. A fill whose item IS caught
        # but whose set() lands after clear() leaves the Event set too —
        # worst case one spurious immediate wake that drains an empty queue.
        self._fills_wake.clear()
        out: list[dict[str, Any]] = []
        while True:
            try:
                out.append(self._fills_queue.get_nowait())
            except queue.Empty:
                break
        return out

    def wait_for_fills(self, timeout: float) -> bool:
        """Block up to ``timeout`` seconds until a fill is enqueued.

        Returns True if woken by a fill (queue may be drained immediately),
        False on timeout. The caller — ALWAYS the main thread — then runs
        ``drain_user_fills`` and logs/sends SSE itself, so the WS callback
        thread never touches the session log. ``timeout`` <= 0 returns
        immediately with the Event's current state without sleeping.
        """
        if timeout <= 0:
            return self._fills_wake.is_set()
        return self._fills_wake.wait(timeout=timeout)

    def subscribe_user_fills(self, user: str) -> bool:
        """Subscribe to ``userFills`` for ``user`` (Phase 1 — log only).

        Idempotent: a second call with the same ``user`` is a no-op so
        the trading loop can call it on every start without leaking
        subscriptions. Returns True on success, False on failure (the
        caller logs and continues; the trading loop is unaffected).

        Stores ``user`` in ``_user_fills_user`` so ``_connect_and_subscribe``
        can re-subscribe automatically after a reconnect.
        """
        if not user:
            logger.warning("[ws:user-fills] subscribe skipped — empty user")
            return False
        if not self._info:
            logger.warning("[ws:user-fills] subscribe skipped — no Info (not started?)")
            return False
        # Idempotent guard: same user already subscribed.
        if self._user_fills_user == user and self._user_fills_sub_id is not None:
            return True
        try:
            sub_id = self._info.subscribe(
                {"type": "userFills", "user": user},
                self._on_user_fills,
            )
            self._user_fills_user = user
            self._user_fills_sub_id = sub_id
            # Set the historical-replay watermark on the FIRST successful
            # subscribe only. Reconnects re-enter this method (sub_id was
            # reset by _connect_and_subscribe) but the watermark stays at
            # the original first-subscribe time, so fills genuinely made
            # during a disconnect gap are not mistaken for replay.
            if self._user_fills_first_sub_at == 0.0:
                self._user_fills_first_sub_at = time.time()
                logger.info(
                    "[ws:user-fills] replay watermark set at %s "
                    "(fills older than %dms before this are dropped)",
                    self._user_fills_first_sub_at, _USER_FILLS_HIST_GRACE_MS,
                )
            logger.info(
                "[ws:user-fills] subscribed user=%s sub_id=%s (Phase 1 log-only)",
                user, sub_id,
            )
            return True
        except Exception as e:
            # Common failure modes:
            #  - SDK requires agent wallet (L2 auth) → caught here
            #  - 429 from /info meta during a freshly-killed reconnect
            #  - subscription channel rejected by server
            # All are non-fatal: trading loop keeps running on REST.
            logger.warning(
                "[ws:user-fills] subscribe FAILED user=%s err=%s "
                "(non-fatal — trading loop unaffected)",
                user, e,
            )
            # Reset so a reconnect can retry cleanly.
            self._user_fills_user = None
            self._user_fills_sub_id = None
            return False

    def unsubscribe_user_fills(self) -> None:
        """Clear the Phase 1 user-fills subscription state.

        The SDK's own subscription registry is torn down by
        ``info.disconnect_websocket`` on ``stop()``, so we don't need
        to call an explicit unsubscribe on the wire. This method just
        clears our persistent state so a later ``subscribe_user_fills``
        on a NEW connection starts fresh.
        """
        if self._user_fills_user is not None:
            logger.info(
                "[ws:user-fills] clearing subscription state user=%s sub_id=%s",
                self._user_fills_user, self._user_fills_sub_id,
            )
        self._user_fills_user = None
        self._user_fills_sub_id = None

    def start(self) -> None:
        """Start the WebSocket connection and subscribe to allMids."""
        if self._running:
            return

        logger.info("[ws] Connecting to Hyperliquid...")

        self._connect_and_subscribe()

        self._running = True

        # Launch background reconnect monitor.
        self._reconnect_delay = _WS_RECONNECT_BASE_DELAY
        self._reconnect_attempts = 0  # 2026-09-02 (P3): 全新启动，计数清零
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
            # 2026-09-02 (P3)：SDK 创建 WebSocketApp 时只设了 on_message/on_open，
            # 断连/错误完全静默。包装 on_open 追加连接成功日志，并补挂
            # on_close/on_error，让连接/断连/异常都有 INFO/WARNING 记录（含端点、
            # 关闭码与异常类型）。包装器先调原 SDK 回调（保持 queued 订阅重放
            # 等原行为），再打日志。
            _ws_app = self._ws_manager.ws
            _ep = getattr(_ws_app, "url", self._info.base_url)
            _sdk_on_open = _ws_app.on_open

            def _on_open(_w):
                _sdk_on_open(_w)
                logger.info(f"[ws] WebSocket connected: {_ep}")

            def _on_close(_w, close_status_code, close_msg):
                logger.info(f"[ws] WebSocket disconnected: {_ep} "
                            f"(close_code={close_status_code}, reason={close_msg!r})")

            def _on_error(_w, err):
                logger.warning(f"[ws] WebSocket error on {_ep}: {type(err).__name__}: {err}")

            _ws_app.on_open = _on_open
            _ws_app.on_close = _on_close
            _ws_app.on_error = _on_error
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

        # Phase 1: if we previously had a userFills subscription, re-establish
        # it on the fresh connection. On the very first start this is a no-op
        # (``_user_fills_user`` is None); on a reconnect it re-subscribes the
        # wallet so the feed resumes without the trading loop having to call
        # ``subscribe_user_fills`` again. We reset sub_id BEFORE re-subscribing
        # so the idempotent guard in ``subscribe_user_fills`` doesn't see a
        # stale sub_id from the dead connection and skip the re-subscribe.
        if self._user_fills_user is not None:
            self._user_fills_sub_id = None
            try:
                self.subscribe_user_fills(self._user_fills_user)
            except Exception as e:
                # Non-fatal: allMids is already up; user-fills is best-effort.
                logger.warning(f"[ws:user-fills] re-subscribe on reconnect failed (non-fatal): {e}")

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
                # Phase 1: user-fills liveness counters. ``user_fills_count``
                # is incremented OUTSIDE the lock from the WS callback thread
                # — the read here is a snapshot, may be one behind; that's
                # fine for a "is the feed alive?" diagnostic. A non-zero
                # count after the trading loop has placed/closed an order
                # proves the subscription is wired correctly.
                "user_fills_count": self._user_fills_count,
                "user_fills_user": self._user_fills_user,
            }

    def _reconnect_loop(self) -> None:
        """Background loop: monitor data freshness and reconnect when stale."""
        while not self._reconnect_stop.is_set():
            self._reconnect_stop.wait(5.0)  # check every 5s
            if self._reconnect_stop.is_set():
                break
            if self.get_data_age_seconds() > _WS_MAX_STALE_SECONDS:
                self._reconnect_attempts += 1  # 2026-09-02 (P3)
                logger.warning(
                    f"[ws] Data stale for {self.get_data_age_seconds():.0f}s "
                    f"(threshold={_WS_MAX_STALE_SECONDS}s) — reconnecting in "
                    f"{self._reconnect_delay:.0f}s (attempt {self._reconnect_attempts})..."
                )
                # Exponential backoff with jitter
                self._reconnect_stop.wait(self._reconnect_delay)
                if self._reconnect_stop.is_set():
                    break
                logger.info(f"[ws] Reconnect attempt {self._reconnect_attempts}: "
                            f"tearing down and re-subscribing...")
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
                    logger.info(f"[ws] Reconnect successful after "
                                f"{self._reconnect_attempts} attempt(s)")
                    self._reconnect_attempts = 0  # 成功后清零（日志之后，保留序号）
                except Exception as e:
                    logger.error(f"[ws] Reconnect attempt {self._reconnect_attempts} failed "
                                 f"({type(e).__name__}: {e}); backing off "
                                 f"{self._reconnect_delay:.0f}s")
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
        # Final flush of the seen-tids set so a restart inherits every tid
        # seen this process (best-effort; never blocks shutdown).
        self._persist_seen_tids(force=True)
        self._stop_internal()
        # Phase 1: clear user-fills subscription state. This is a FINAL
        # stop (vs ``_stop_internal`` which is also called from the
        # reconnect loop and must PRESERVE state so the new connection
        # re-subscribes). Clearing here means a subsequent ``start()``
        # + ``subscribe_user_fills(user)`` starts from a clean slate.
        self.unsubscribe_user_fills()
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
                        except OSError as _se:
                            # 2026-09-02 (P3)：socket 可能已关闭（ENOTCONN 等），
                            # 属于正常竞态；降为 debug 保留排障痕迹，不再静默。
                            logger.debug(f"[ws] sock.shutdown during stop: {type(_se).__name__}: {_se}")
                        sock.close()
            except Exception as _e:
                # 2026-09-02 (P3)：teardown 失败不再静默吞掉，debug 记录异常类型。
                logger.debug(f"[ws] manager teardown error during stop: {type(_e).__name__}: {_e}")
        if self._info:
            try:
                self._info.disconnect_websocket()
            except Exception as _e:
                logger.debug(f"[ws] disconnect_websocket during stop: {type(_e).__name__}: {_e}")

    def __enter__(self) -> "HyperliquidWebSocket":
        self.start()
        return self

    def __exit__(self, *args: Any) -> None:
        self.stop()
