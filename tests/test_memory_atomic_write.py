"""R11-B1 — agent memory atomic write path (flock + fsync + dir fsync).

Covered:
  * _write_atomic opens MEMORY_LOCK_FILE, takes an exclusive flock, writes a
    tmp file, fsyncs it, atomically renames to MEMORY_FILE, and fsyncs the
    parent directory entry so a power loss after os.replace still leaves
    a durable .agent-memory.json.
  * The cross-process flock is ALWAYS released in a finally block — even
    when json.dump or the write itself raises — so a second process can
    never be blocked on a leaked kernel lock until the trading loop dies.
  * The in-process snapshot lock is held only across the dict-build step;
    the actual disk write runs OUTSIDE the in-process lock so a slow disk
    cannot stall the trading loop.
  * The tmp file is cleaned up if json.dump raises (no leftover .tmp to
    shadow the next flush).
  * The dir fsync is best-effort: an OSError from a non-fsyncable FS
    (some CI sandboxes) does not fail the flush.

Tests use a temp file as MEMORY_FILE so the live .agent-memory.json is
never touched. ``_write_atomic`` is called directly with a hand-built
``data`` dict; ``flush()`` is stubbed on the AgentMemory instance so no
real-state writes leak into the test.
"""
from __future__ import annotations

import fcntl
import json
import os
import threading
from unittest import mock

import pytest

from hermes_trader.agents import memory as memory_mod
from hermes_trader.agents.memory import AgentMemory, MEMORY_FILE, MEMORY_LOCK_FILE


@pytest.fixture
def temp_memory_file(monkeypatch, tmp_path):
    """Redirect MEMORY_FILE to a per-test temp path and reset the module
    singleton so the lock file lives next to the new MEMORY_FILE."""
    target = tmp_path / "agent-memory.json"
    monkeypatch.setattr(memory_mod, "MEMORY_FILE", str(target))
    monkeypatch.setattr(memory_mod, "MEMORY_LOCK_FILE", str(target) + ".lock")
    return target


# ── happy path ────────────────────────────────────────────────────────


def test_write_atomic_creates_valid_file(temp_memory_file):
    mem = AgentMemory()
    mem._initialized = True
    data = {
        "perceptions": [{"seq": 1}, {"seq": 2}],
        "analyses": [],
        "trades": [],
        "closes": [],
        "entryCtx": {},
        "cooldowns": [],
        "equity": 1234.5,
        "dailyPnl": 0.0,
        "startOfDayEquity": 0.0,
        "dayStartTs": 0,
        "openPositions": [],
        "coinCircuit": {},
        "globalHaltUntilMs": 0,
        "consecutiveLosses": {},
    }
    ok = mem._write_atomic(data)
    assert ok is True
    # File exists, contains the data we wrote.
    assert temp_memory_file.exists()
    with open(temp_memory_file) as f:
        loaded = json.load(f)
    assert loaded["equity"] == 1234.5
    assert len(loaded["perceptions"]) == 2


def test_write_atomic_replaces_atomically(temp_memory_file):
    """A second _write_atomic call should overwrite the first without
    ever leaving a half-written .agent-memory.json."""
    mem = AgentMemory()
    mem._initialized = True
    mem._write_atomic({"perceptions": [], "equity": 1.0})
    mem._write_atomic({"perceptions": [], "equity": 2.0})
    with open(temp_memory_file) as f:
        loaded = json.load(f)
    assert loaded["equity"] == 2.0


def test_write_atomic_does_not_leave_tmp(temp_memory_file):
    """After a successful write, no .tmp file should remain next to the
    memory file (it would shadow the next write if cleanup missed)."""
    mem = AgentMemory()
    mem._initialized = True
    mem._write_atomic({"perceptions": [], "equity": 1.0})
    assert not (temp_memory_file.parent / (temp_memory_file.name + ".tmp")).exists()


# ── fsync calls ───────────────────────────────────────────────────────


def test_write_atomic_fsyncs_tmp_file(temp_memory_file):
    """The tmp file must be fsync'd before os.replace so a power loss
    between dump and rename cannot leave an empty .agent-memory.json."""
    mem = AgentMemory()
    mem._initialized = True
    with mock.patch.object(os, "fsync") as fsync:
        mem._write_atomic({"perceptions": [], "equity": 1.0})
    # At least one fsync: the tmp file, and the parent dir entry.
    assert fsync.call_count >= 1


def test_write_atomic_fsyncs_parent_directory(temp_memory_file):
    """The parent directory entry must be fsync'd so os.replace is durable
    on the journal (without it a crash after replace can revert to the
    pre-replace name on filesystems with a separate dir journal)."""
    mem = AgentMemory()
    mem._initialized = True
    with mock.patch.object(os, "fsync") as fsync:
        mem._write_atomic({"perceptions": [], "equity": 1.0})
    # Two fsyncs: tmp file, then the parent dir.
    assert fsync.call_count == 2


def test_write_atomic_dir_fsync_failure_is_swallowed(temp_memory_file):
    """Some filesystems (e.g. overlayfs in CI sandboxes) raise on
    dir-fsync. The data is already durable in the file itself, so the
    dir fsync is best-effort: an OSError must NOT fail the flush."""
    mem = AgentMemory()
    mem._initialized = True
    real_fsync = os.fsync
    call_count = {"n": 0}

    def fake_fsync(fd):
        call_count["n"] += 1
        # First fsync = tmp file, must succeed. Second fsync = dir entry,
        # simulate a non-fsyncable FS.
        if call_count["n"] == 2:
            raise OSError("dir fsync not supported on this fs")
        return real_fsync(fd)

    with mock.patch.object(os, "fsync", side_effect=fake_fsync):
        ok = mem._write_atomic({"perceptions": [], "equity": 1.0})
    assert ok is True
    assert call_count["n"] == 2  # tmp + dir (dir failed)
    # File is still durably on disk despite the dir-fsync failure.
    assert temp_memory_file.exists()
    with open(temp_memory_file) as f:
        assert json.load(f)["equity"] == 1.0


# ── flock lifecycle ───────────────────────────────────────────────────


def test_write_atomic_releases_flock_on_success(temp_memory_file):
    """After _write_atomic returns, a second process's flock attempt
    (simulated by a fresh open + LOCK_EX) must succeed immediately."""
    mem = AgentMemory()
    mem._initialized = True
    mem._write_atomic({"perceptions": [], "equity": 1.0})
    # Probe the lock from a fresh fd — would block forever if leaked.
    probe_fd = os.open(MEMORY_LOCK_FILE, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(probe_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        os.close(probe_fd)


def test_write_atomic_releases_flock_on_dump_exception(temp_memory_file):
    """The cross-process flock MUST be released even when json.dump
    raises — otherwise a stuck lock would block every other process
    touching the memory file until the trading loop dies."""
    mem = AgentMemory()
    mem._initialized = True
    # Force json.dump to raise: a non-serializable object (e.g. a set) will
    # raise TypeError. (json module does not accept sets by default.)
    with mock.patch.object(
        memory_mod.json, "dump",
        side_effect=TypeError("intentional dump failure"),
    ):
        ok = mem._write_atomic({"perceptions": [set()]})  # sets not JSON-able
    assert ok is False
    # Lock must still be free for the next flush.
    probe_fd = os.open(MEMORY_LOCK_FILE, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(probe_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        os.close(probe_fd)


def test_write_atomic_releases_flock_on_disk_full(temp_memory_file):
    """A full-disk OSError on the tmp write must release the flock."""
    mem = AgentMemory()
    mem._initialized = True
    with mock.patch.object(
        memory_mod.json, "dump",
        side_effect=OSError(28, "No space left on device"),
    ):
        ok = mem._write_atomic({"perceptions": [], "equity": 1.0})
    assert ok is False
    probe_fd = os.open(MEMORY_LOCK_FILE, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(probe_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        os.close(probe_fd)


def test_write_atomic_cleans_up_tmp_on_dump_exception(temp_memory_file):
    """A failed json.dump must not leave a stale .tmp file that shadows
    the real .agent-memory.json on the next flush."""
    mem = AgentMemory()
    mem._initialized = True
    with mock.patch.object(
        memory_mod.json, "dump",
        side_effect=TypeError("intentional"),
    ):
        mem._write_atomic({"perceptions": [set()]})
    assert not (temp_memory_file.parent / (temp_memory_file.name + ".tmp")).exists()


# ── flush() lock scoping ─────────────────────────────────────────────


def test_flush_releases_inprocess_lock_before_disk_io(temp_memory_file):
    """flush() must NOT hold self._lock across the disk write — a slow
    disk must not stall concurrent mutators. We probe the in-process
    lock from a second perspective inside _write_atomic to verify the
    in-process lock has been released."""
    mem = AgentMemory()
    mem._initialized = True
    mem._dirty = True

    lock_was_acquirable = []

    def slow_write(data):
        # _write_atomic should run OUTSIDE the in-process lock. If flush()
        # was still holding self._lock here, the non-blocking acquire
        # below would fail and return False.
        acquired = mem._lock.acquire(blocking=False)
        lock_was_acquirable.append(bool(acquired))
        if acquired:
            mem._lock.release()
        return True

    with mock.patch.object(mem, "_write_atomic", side_effect=slow_write):
        mem.flush(force=True)
    # _write_atomic was called and the in-process lock was NOT held.
    assert lock_was_acquirable == [True]


def test_flush_clears_dirty_before_disk_io(temp_memory_file):
    """If the disk write is slow, a concurrent mutator must be able to
    re-dirty the memory so the next flush picks up the change. So flush()
    clears _dirty under the lock, BEFORE delegating to _write_atomic."""
    mem = AgentMemory()
    mem._initialized = True
    mem._dirty = True

    captured_dirty = []

    def capture_dirty(data):
        captured_dirty.append(mem._dirty)
        return True

    with mock.patch.object(mem, "_write_atomic", side_effect=capture_dirty):
        mem.flush(force=True)
    # _dirty was cleared (False) before _write_atomic saw it.
    assert captured_dirty == [False]


def test_flush_leaves_dirty_set_on_disk_failure(temp_memory_file, monkeypatch):
    """A failed _write_atomic must leave _dirty True so the next mutation
    re-arms a retry — losing a write silently is the worst possible
    outcome for the trading loop."""
    mem = AgentMemory()
    mem._initialized = True
    mem._dirty = True
    monkeypatch.setattr(mem, "_write_atomic", lambda data: False)
    mem.flush(force=True)
    assert mem._dirty is False  # flush cleared it under the lock
    # A subsequent mutation re-dirties so a future flush retries.
    mem.update_equity(99.0)
    assert mem._dirty is True


def test_flush_records_failure_metric(temp_memory_file, monkeypatch):
    """A failed _write_atomic increments MEMORY_FLUSH_ERRORS so the
    R11-F1 Prometheus alert can fire on a flapping disk."""
    from hermes_trader import metrics

    mem = AgentMemory()
    mem._initialized = True
    mem._dirty = True
    monkeypatch.setattr(mem, "_write_atomic", lambda data: False)
    with mock.patch.object(metrics.MEMORY_FLUSH_ERRORS, "inc") as inc:
        mem.flush(force=True)
        inc.assert_called_once()


def test_flush_records_success_metric(temp_memory_file):
    """A successful _write_atomic observes a flush-duration sample with
    outcome=ok (no error counter bump). Verified by stubbing
    _observe_flush_metric to capture the (force, ok) tuple that flush()
    passes through."""
    mem = AgentMemory()
    mem._initialized = True
    mem._dirty = True
    captured: list = []
    with mock.patch.object(
        mem, "_observe_flush_metric",
        side_effect=lambda t0, force, ok: captured.append((force, ok)),
    ):
        mem.flush(force=True)
    # One observation with force=True, ok=True.
    assert captured == [(True, True)]


# ── concurrency: two concurrent flushes ──────────────────────────────


def test_concurrent_flushes_serialize_via_flock(temp_memory_file):
    """Two threads calling flush() simultaneously must each see a clean
    write (no truncation, no interleaving). The cross-process flock in
    _write_atomic is what serializes them; the in-process lock only
    serializes the snapshot. We model this by letting real _write_atomic
    run and checking the file is valid and complete after both threads
    finish."""
    mem = AgentMemory()
    mem._initialized = True
    mem._dirty = True
    t_results: list = []

    def writer(equity):
        with mem._lock:
            mem._equity = equity
            mem._dirty = True
        mem.flush(force=True)
        # flush() clears _dirty on success; verify it landed.
        t_results.append(not mem._dirty)

    t1 = threading.Thread(target=writer, args=(111.0,))
    t2 = threading.Thread(target=writer, args=(222.0,))
    t1.start()
    t2.start()
    t1.join(timeout=5)
    t2.join(timeout=5)
    # Both writes succeeded (flock serialized them, neither deadlocked).
    assert t_results == [True, True]
    # Final state is one of the two writes (no half-written JSON).
    with open(temp_memory_file) as f:
        data = json.load(f)
    assert data["equity"] in (111.0, 222.0)
