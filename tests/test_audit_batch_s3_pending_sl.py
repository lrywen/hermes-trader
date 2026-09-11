"""Batch S3 guards — pending-SL retry queue persistence (Q6/Q8).

Before this fix, ``_pending_sl_retries`` was purely in-memory: a process restart
dropped naked positions and rehydrate_from_exchange never actively re-arms a
missing stop, so a position could run with no exchange-side stop for the rest of
its life (the DSL soft loop dies with the same process — common-cause). The
queue is now mirrored to a flock+atomic JSON file and reloaded by
``retry_pending_sl`` on the first cycle after startup.
"""

from __future__ import annotations

import json
import uuid

import pytest

from hermes_trader.agents import executor
from hyperliquid.utils.types import Cloid


@pytest.fixture()
def isolated_queue(tmp_path, monkeypatch):
    state = tmp_path / "pending-sl.json"
    monkeypatch.setattr(executor, "_PENDING_SL_FILE", str(state))
    monkeypatch.setattr(executor, "_PENDING_SL_LOCK_FILE", f"{state}.lock")
    monkeypatch.setattr(executor, "_pending_sl_loaded", False)
    with executor._EXEC_LOCK:
        executor._pending_sl_retries.clear()
    yield state
    with executor._EXEC_LOCK:
        executor._pending_sl_retries.clear()


def _entry(coin="ETH", cloid=None):
    return {
        "is_buy": True,
        "size": 1.5,
        "sl_px": 2400.0,
        "coin": coin,
        "side": "long",
        "limit_band_pct": 0.0,
        "cloid": cloid or Cloid.from_int(uuid.uuid4().int),
        "retry_count": 2,
        "last_attempt": 1000.0,
    }


def test_roundtrip_persists_entries_and_rebuilds_cloid(isolated_queue):
    state = isolated_queue
    cloid = Cloid.from_int(uuid.uuid4().int)
    with executor._EXEC_LOCK:
        executor._pending_sl_retries["ETH"] = _entry("ETH", cloid)
    executor._persist_pending_sl()
    assert state.exists()
    doc = json.loads(state.read_text())
    # The Cloid object must not leak as a non-serializable value; stored as raw hex.
    assert doc["entries"]["ETH"]["cloid_raw"] == cloid.to_raw()
    assert "cloid" not in doc["entries"]["ETH"]

    # Simulate fresh process: reload into an empty queue.
    with executor._EXEC_LOCK:
        executor._pending_sl_retries.clear()
    executor._pending_sl_loaded = False
    n = executor.load_pending_sl()
    assert n == 1
    restored = executor._pending_sl_retries["ETH"]
    assert restored["coin"] == "ETH"
    assert restored["size"] == 1.5
    assert restored["cloid"].to_raw() == cloid.to_raw()
    # Restored entries are immediately retry-eligible (the naked position has
    # already waited through the restart).
    assert restored["last_attempt"] == 0.0


def test_load_is_one_shot(isolated_queue):
    with executor._EXEC_LOCK:
        executor._pending_sl_retries["ETH"] = _entry()
    executor._persist_pending_sl()
    executor._pending_sl_loaded = False
    assert executor.load_pending_sl() == 1
    # Second call in the same process is a no-op even though the file exists.
    assert executor.load_pending_sl() == 0


def test_missing_file_loads_zero(isolated_queue):
    executor._pending_sl_loaded = False
    assert executor.load_pending_sl() == 0


def test_corrupt_file_does_not_raise(isolated_queue):
    isolated_queue.write_text("{not valid json")
    executor._pending_sl_loaded = False
    assert executor.load_pending_sl() == 0


def test_retry_pending_sl_invokes_load(monkeypatch):
    # Source-level contract: the periodic consumer rehydrates the queue first.
    import inspect
    src = inspect.getsource(executor.retry_pending_sl)
    assert "load_pending_sl()" in src


def test_metrics_names_exposed():
    from fastapi.testclient import TestClient
    from hermes_trader.server import app
    client = TestClient(app)
    body = client.get("/metrics").text
    for name in (
        "hermes_pending_sl_retries",
        "hermes_pending_sl_missing_total",
        "hermes_pending_sl_rearm_failures_total",
    ):
        assert name in body
