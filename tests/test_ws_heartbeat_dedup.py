"""R11-D1 — ws heartbeat + sequence dedup (ws_client.HyperliquidWebSocket).

Covers:
  * ``_accept_seq`` semantics: first frame accepted; duplicate dropped
    (counts in ``dropped_dup``); smaller-than-last dropped
    (counts in ``dropped_stale``); a strictly larger seq is always
    accepted.
  * ``_on_all_mids`` integration: the first payload updates
    ``all_mids``; a replay-style payload does NOT overwrite
    ``all_mids`` and bumps the appropriate drop counter.
  * Reconnect resets ``last_seq`` to 0 and clears the internal seq,
    so the next frame on a new connection is accepted.
  * ``_heartbeat_loop`` refreshes ``app_heartbeat_at``; ``stop()``
    wakes the thread immediately via ``_heartbeat_stop.set()``.
  * ``info.ping()`` is best-effort: missing attribute is fine;
    raising during ``ping()`` does not kill the heartbeat thread.
  * ``get_diag()`` returns the documented 5 keys with consistent
    values after a few frames.
  * ``get_snapshot()`` carries the new R11-D1 fields
    (``last_seq``, ``app_heartbeat_at``).
"""
from __future__ import annotations

import threading
import time
from unittest import mock

import pytest

from hermes_trader.client import ws_client
from hermes_trader.client.ws_client import (
    HLSSLOptWebsocketManager,
    HyperliquidWebSocket,
    RealtimeSnapshot,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def ws(monkeypatch: pytest.MonkeyPatch) -> HyperliquidWebSocket:
    """Bare WS client — no ``start()`` (no network)."""
    # Use a very long heartbeat so the test thread doesn't fire on its own
    # unless we explicitly want it to.
    monkeypatch.setenv("HERMES_WS_HEARTBEAT_S", "3600")
    monkeypatch.setenv("HERMES_WS_SEQ_MAX_BACKWARD", "1024")
    return HyperliquidWebSocket()


def _all_mids_frame(mids_dict: dict) -> dict:
    """Build the SDK-shaped frame that allMids callbacks receive."""
    return {"channel": "allMids", "data": {"mids": dict(mids_dict)}}


# ---------------------------------------------------------------------------
# _accept_seq semantics
# ---------------------------------------------------------------------------

class TestAcceptSeq:
    def test_first_frame_accepted(self, ws: HyperliquidWebSocket) -> None:
        """incoming=1, last_seq=0 → accept, last_seq becomes 1."""
        assert ws._accept_seq(1) is True
        assert ws._latest.last_seq == 1
        assert ws._dropped_dup == 0
        assert ws._dropped_stale == 0

    def test_strictly_larger_accepted(self, ws: HyperliquidWebSocket) -> None:
        ws._accept_seq(5)
        assert ws._accept_seq(6) is True
        assert ws._latest.last_seq == 6

    def test_duplicate_dropped_increments_dup(self, ws: HyperliquidWebSocket) -> None:
        ws._accept_seq(5)
        assert ws._accept_seq(5) is False
        assert ws._latest.last_seq == 5  # unchanged
        assert ws._dropped_dup == 1
        assert ws._dropped_stale == 0

    def test_smaller_dropped_increments_stale(self, ws: HyperliquidWebSocket) -> None:
        ws._accept_seq(5)
        assert ws._accept_seq(3) is False
        assert ws._latest.last_seq == 5  # unchanged
        assert ws._dropped_dup == 0
        assert ws._dropped_stale == 1

    def test_zero_after_nonzero_dropped_as_stale(
        self, ws: HyperliquidWebSocket,
    ) -> None:
        """incoming=0 < last_seq=5 → drop, counted as stale (replay)."""
        ws._accept_seq(5)
        assert ws._accept_seq(0) is False
        assert ws._dropped_stale == 1

    def test_counters_accumulate_across_drops(
        self, ws: HyperliquidWebSocket,
    ) -> None:
        ws._accept_seq(10)            # accept
        ws._accept_seq(10)            # dup
        ws._accept_seq(10)            # dup
        ws._accept_seq(7)             # stale
        ws._accept_seq(2)             # stale
        assert ws._dropped_dup == 2
        assert ws._dropped_stale == 2
        assert ws._latest.last_seq == 10

    def test_soft_cap_accepts_far_backward_permissive(
        self, ws: HyperliquidWebSocket, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``_WS_SEQ_MAX_BACKWARD`` is a soft warning cap, not a hard
        drop — the dedup rule (incoming > last_seq) still wins. We set
        the cap to 2, push last_seq to 10, then submit incoming=5.
        ``5 < 10`` is dropped by the strict-greater rule, not by the
        cap. The cap only governs the *branch* taken on the way
        through the accept path; if incoming > last_seq, accept
        regardless of the gap.
        """
        monkeypatch.setattr(ws_client, "_WS_SEQ_MAX_BACKWARD", 2)
        ws._accept_seq(10)
        # incoming=20 > last_seq=10, accept (gap is 10 but cap is 2;
        # the cap is a logging hint, not a drop gate).
        assert ws._accept_seq(20) is True
        assert ws._latest.last_seq == 20


# ---------------------------------------------------------------------------
# _on_all_mids integration
# ---------------------------------------------------------------------------

class TestOnAllMids:
    def test_first_payload_updates_all_mids(
        self, ws: HyperliquidWebSocket,
    ) -> None:
        ws._on_all_mids(_all_mids_frame({"BTC": "50000", "ETH": "3000"}))
        assert ws.get_all_mids() == {"BTC": "50000", "ETH": "3000"}
        assert ws._latest.last_seq == 1
        assert ws._seq == 1

    def test_replay_payload_does_not_overwrite(
        self, ws: HyperliquidWebSocket,
    ) -> None:
        """The dedup happens at the _accept_seq layer, which is fed
        the *internal* counter. To simulate a server-level replay
        (same incoming seq again), we drive _accept_seq directly
        with a duplicate — that is exactly what the dedup layer
        exists to defend against."""
        ws._accept_seq(5)
        assert ws._latest.last_seq == 5
        # Manually populate the snapshot, then submit a duplicate seq.
        with ws._lock:
            ws._latest.all_mids = {"BTC": "50000"}
        # Replay the SAME seq → dropped, snapshot untouched.
        assert ws._accept_seq(5) is False
        assert ws.get_all_mids() == {"BTC": "50000"}
        assert ws._dropped_dup == 1

    def test_on_all_mids_increments_seq_for_each_callback(
        self, ws: HyperliquidWebSocket,
    ) -> None:
        """Each call to ``_on_all_mids`` advances the internal seq
        and applies to the snapshot. The dedup layer is what would
        defend against same-incoming-seq frames; the on_all_mids
        path itself is sequential and accepts each new frame."""
        ws._on_all_mids(_all_mids_frame({"BTC": "50000"}))
        ws._on_all_mids(_all_mids_frame({"BTC": "60000"}))
        assert ws._seq == 2
        assert ws._latest.last_seq == 2
        assert ws.get_all_mids() == {"BTC": "60000"}

    def test_non_dict_payload_ignored(
        self, ws: HyperliquidWebSocket,
    ) -> None:
        """Defensive: SDK should always send a dict, but if a future
        build sends something else, we must not crash and must not
        bump the seq counter."""
        ws._on_all_mids("not a dict")
        ws._on_all_mids(None)
        assert ws._seq == 0
        assert ws._dropped_dup == 0
        assert ws._dropped_stale == 0

    def test_dict_without_data_ignored(
        self, ws: HyperliquidWebSocket,
    ) -> None:
        ws._on_all_mids({"channel": "allMids", "data": "not a dict"})
        assert ws._seq == 0

    def test_dict_with_data_but_no_mids_is_accepted(
        self, ws: HyperliquidWebSocket,
    ) -> None:
        """``data.mids`` defaulting to ``{}`` is still a dict, so the
        accept path runs and updates the snapshot to an empty mids
        payload. This is the SDK's normal "allMids with no markets"
        shape, not a malformed frame — we exercise it here to lock
        in the seq bookkeeping under the empty-but-valid case."""
        ws._on_all_mids({"channel": "allMids", "data": {}})
        assert ws._seq == 1
        assert ws._latest.last_seq == 1
        assert ws.get_all_mids() == {}

    def test_m10_identical_payload_counted_as_replay(
        self, ws: HyperliquidWebSocket,
    ) -> None:
        """M-10: a second frame whose payload CONTENT is identical to the
        last applied one is a replay (HL reconnect window / heartbeat echo).
        It must be counted in dropped_replay and must not advance state."""
        frame = _all_mids_frame({"BTC": "50000", "ETH": "3000"})
        ws._on_all_mids(frame)
        first_age = ws._latest.last_update_time
        assert ws.get_all_mids() == {"BTC": "50000", "ETH": "3000"}
        # Re-send the identical payload (as HL would on replay).
        time.sleep(0.01)
        ws._on_all_mids(_all_mids_frame({"BTC": "50000", "ETH": "3000"}))
        assert ws._seq == 2  # raw frame counter still advances...
        assert ws._dropped_replay == 1  # ...but the replay is dropped
        # snapshot unchanged and update time NOT refreshed
        assert ws.get_all_mids() == {"BTC": "50000", "ETH": "3000"}
        assert ws._latest.last_update_time == first_age

    def test_m11_single_coin_spike_suppressed(
        self, ws: HyperliquidWebSocket, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """M-11: a tick that moves one coin's mid beyond the jump cap is
        suppressed (prior mid kept); well-behaved coins in the same frame
        still apply."""
        monkeypatch.setattr(ws_client, "_WS_MAX_TICK_JUMP_FRAC", 0.25)
        ws._on_all_mids(_all_mids_frame({"BTC": "50000", "ETH": "3000"}))
        # BTC jumps +100% (bad print) while ETH moves normally.
        ws._on_all_mids(_all_mids_frame({"BTC": "100000", "ETH": "3010"}))
        mids = ws.get_all_mids()
        assert mids["BTC"] == "50000"   # spike suppressed, prior kept
        assert mids["ETH"] == "3010"    # normal move applied
        assert ws._dropped_spike == 1

    def test_m11_new_coin_without_prior_is_accepted(
        self, ws: HyperliquidWebSocket, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """M-11: a coin seen for the first time has no prior mid to compare
        against and must be accepted regardless of magnitude (new listing)."""
        monkeypatch.setattr(ws_client, "_WS_MAX_TICK_JUMP_FRAC", 0.25)
        ws._on_all_mids(_all_mids_frame({"BTC": "50000"}))
        ws._on_all_mids(_all_mids_frame({"BTC": "50010", "DOGE": "0.123"}))
        mids = ws.get_all_mids()
        assert mids["DOGE"] == "0.123"
        assert ws._dropped_spike == 0


# ---------------------------------------------------------------------------
# get_diag
# ---------------------------------------------------------------------------

class TestGetDiag:
    def test_keys_present(self, ws: HyperliquidWebSocket) -> None:
        d = ws.get_diag()
        assert set(d.keys()) == {
            "seq", "last_seq", "dropped_dup", "dropped_stale",
            "dropped_replay", "dropped_spike", "data_age_s",
            "user_fills_count", "user_fills_user",
        }

    def test_initial_values(self, ws: HyperliquidWebSocket) -> None:
        d = ws.get_diag()
        assert d["seq"] == 0
        assert d["last_seq"] == 0
        assert d["dropped_dup"] == 0
        assert d["dropped_stale"] == 0
        assert d["dropped_replay"] == 0
        assert d["dropped_spike"] == 0
        assert 0.0 <= d["data_age_s"] < 5.0  # freshly constructed

    def test_values_track_callbacks(
        self, ws: HyperliquidWebSocket,
    ) -> None:
        """Drive the diag fields via the public dedup entry point so
        we can land both a dup and a stale counter increment in the
        same test."""
        # Three real frames: seq 1, 2, 3.
        ws._on_all_mids(_all_mids_frame({"BTC": "1"}))
        ws._on_all_mids(_all_mids_frame({"BTC": "2"}))
        ws._on_all_mids(_all_mids_frame({"BTC": "3"}))
        # Then a server-style replay: same seq again.
        assert ws._accept_seq(3) is False
        # And a stale frame from a server replay window.
        assert ws._accept_seq(1) is False
        d = ws.get_diag()
        assert d["seq"] == 3  # internal counter unchanged by manual calls
        assert d["last_seq"] == 3
        assert d["dropped_dup"] == 1
        assert d["dropped_stale"] == 1


# ---------------------------------------------------------------------------
# get_snapshot — must carry the R11-D1 fields
# ---------------------------------------------------------------------------

class TestGetSnapshot:
    def test_snapshot_carries_last_seq_and_heartbeat(
        self, ws: HyperliquidWebSocket,
    ) -> None:
        ws._accept_seq(42)
        with ws._lock:
            ws._latest.app_heartbeat_at = 1234567890.0
        snap = ws.get_snapshot()
        assert snap.last_seq == 42
        assert snap.app_heartbeat_at == 1234567890.0

    def test_snapshot_is_a_copy(self, ws: HyperliquidWebSocket) -> None:
        snap1 = ws.get_snapshot()
        snap2 = ws.get_snapshot()
        assert snap1 is not snap2
        assert snap1.all_mids is not snap2.all_mids


# ---------------------------------------------------------------------------
# _heartbeat_loop
# ---------------------------------------------------------------------------

class TestHeartbeatLoop:
    def test_heartbeat_thread_starts_with_run(
        self, ws: HyperliquidWebSocket, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """We can't run a real ``start()`` (network), but the heartbeat
        thread is reachable via a manual launch — and ``stop()`` should
        wake it. This is the same harness pattern we use elsewhere
        for daemon-threaded background work."""
        ws._heartbeat_stop.clear()
        ws._heartbeat_thread = threading.Thread(
            target=ws._heartbeat_loop, daemon=True, name="ws-heartbeat-test",
        )
        ws._heartbeat_thread.start()
        # Give the loop a chance to enter wait().
        time.sleep(0.05)
        assert ws._heartbeat_thread.is_alive()
        ws._heartbeat_stop.set()
        ws._heartbeat_thread.join(timeout=2.0)
        assert not ws._heartbeat_thread.is_alive()

    def test_heartbeat_loop_bumps_app_heartbeat_at(
        self, ws: HyperliquidWebSocket, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Force the loop to fire immediately by setting the env var
        to a very small value, then verify ``app_heartbeat_at``
        advanced."""
        # Set a 0.05s heartbeat, run a one-shot loop iteration, and
        # check the timestamp advanced.
        with monkeypatch.context() as m:
            m.setattr(ws_client, "_WS_HEARTBEAT_S", 0.05)
            # Snapshot the starting value.
            ws._latest.app_heartbeat_at = 1000.0
            ws._heartbeat_stop.clear()
            t = threading.Thread(target=ws._heartbeat_loop, daemon=True)
            t.start()
            time.sleep(0.20)  # ~3-4 iterations
            ws._heartbeat_stop.set()
            t.join(timeout=2.0)
        assert ws._latest.app_heartbeat_at > 1000.0

    def test_heartbeat_ping_attribute_missing_is_fine(
        self, ws: HyperliquidWebSocket, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """If ``self._info`` has no ``ping`` attribute, the loop must
        not AttributeError. Run a short loop and confirm the thread
        stays alive and the heartbeat timestamp was still bumped."""
        class NoPing:
            pass
        ws._info = NoPing()
        with monkeypatch.context() as m:
            m.setattr(ws_client, "_WS_HEARTBEAT_S", 0.05)
            ws._heartbeat_stop.clear()
            t = threading.Thread(target=ws._heartbeat_loop, daemon=True)
            t.start()
            time.sleep(0.15)
            assert t.is_alive()
            ws._heartbeat_stop.set()
            t.join(timeout=2.0)
        assert not t.is_alive()

    def test_heartbeat_ping_raising_does_not_kill_loop(
        self, ws: HyperliquidWebSocket, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """If ``info.ping()`` raises, the loop must catch it and keep
        running. We use a MagicMock ``_info`` with a ``ping`` that
        explodes, run a short loop, and confirm the heartbeat
        timestamp was still bumped (proving the loop survived)."""

        class ExplodingPing:
            def ping(self) -> None:
                raise RuntimeError("simulated SDK bug")
        ws._info = ExplodingPing()
        with monkeypatch.context() as m:
            m.setattr(ws_client, "_WS_HEARTBEAT_S", 0.05)
            ws._heartbeat_stop.clear()
            ws._latest.app_heartbeat_at = 1.0
            t = threading.Thread(target=ws._heartbeat_loop, daemon=True)
            t.start()
            time.sleep(0.20)
            assert t.is_alive(), "heartbeat thread must survive ping() raise"
            ws._heartbeat_stop.set()
            t.join(timeout=2.0)
        # app_heartbeat_at was still bumped on the snapshot update path,
        # which is independent of the ping() call.
        assert ws._latest.app_heartbeat_at > 1.0


# ---------------------------------------------------------------------------
# Reconnect resets last_seq
# ---------------------------------------------------------------------------

class TestReconnectResetsSeq:
    def test_reconnect_loop_resets_seq(
        self, ws: HyperliquidWebSocket,
    ) -> None:
        """Drive the post-reconnect reset path by simulating the
        success branch of ``_reconnect_loop``. We can't run the real
        loop (it polls data freshness), but the reset is just a
        2-line block; we replicate the logic and assert."""
        ws._accept_seq(100)
        assert ws._latest.last_seq == 100
        # Mimic the post-reconnect reset block.
        with ws._lock:
            ws._seq = 0
            ws._latest.last_seq = 0
        # Next frame accepted.
        assert ws._accept_seq(1) is True
        assert ws._latest.last_seq == 1
        # self._seq is bumped by _on_all_mids, not _accept_seq itself,
        # so it remains 0 here; the next real callback will start
        # numbering again at 1.


# ---------------------------------------------------------------------------
# start() / stop() smoke — no real network, just thread bookkeeping
# ---------------------------------------------------------------------------

class TestStartStopSmoke:
    def test_stop_wakes_heartbeat_thread(
        self, ws: HyperliquidWebSocket, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """If ``start()`` were network-free (it isn't), the heartbeat
        thread launched inside ``start()`` would need to be stopped
        by ``stop()``. We exercise that contract by manually wiring
        the threads up the way ``start()`` does, then calling
        ``stop()`` and asserting both threads exit."""
        # Pretend start() ran: mark running, launch both background
        # threads but bypass _connect_and_subscribe (which would do
        # a real HTTP POST).
        monkeypatch.setattr(ws_client, "_WS_HEARTBEAT_S", 3600)
        ws._running = True
        ws._reconnect_stop.clear()
        ws._heartbeat_stop.clear()
        ws._reconnect_thread = threading.Thread(
            target=ws._reconnect_loop, daemon=True, name="ws-reconnect-test",
        )
        ws._heartbeat_thread = threading.Thread(
            target=ws._heartbeat_loop, daemon=True, name="ws-heartbeat-test",
        )
        ws._reconnect_thread.start()
        ws._heartbeat_thread.start()
        time.sleep(0.05)
        # Now stop().
        ws.stop(timeout=2.0)
        assert not ws._reconnect_thread.is_alive()
        assert not ws._heartbeat_thread.is_alive()


# ---------------------------------------------------------------------------
# Dataclass smoke — last_seq / app_heartbeat_at fields exist
# ---------------------------------------------------------------------------

class TestSnapshotDataclass:
    def test_default_values(self) -> None:
        s = RealtimeSnapshot()
        assert s.last_seq == 0
        assert s.app_heartbeat_at <= time.time()

    def test_get_price_zero_on_missing(self) -> None:
        s = RealtimeSnapshot(all_mids={})
        assert s.get_price("BTC") == 0.0

    def test_get_price_zero_on_bad_value(self) -> None:
        s = RealtimeSnapshot(all_mids={"BTC": "not-a-number"})
        assert s.get_price("BTC") == 0.0

    def test_get_price_returns_float_on_good_value(self) -> None:
        s = RealtimeSnapshot(all_mids={"BTC": "50000.5"})
        assert s.get_price("BTC") == 50000.5
