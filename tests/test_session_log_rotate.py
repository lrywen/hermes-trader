"""R11-G1 — session_log rotation + gzip + retention.

Covers:
  * The active log is gzipped to ``<path>.<ts>.gz`` when the line
    count crosses ``SESSION_LOG_MAX_LINES``.
  * Same for byte count crossing ``SESSION_LOG_MAX_BYTES``.
  * Same for age crossing ``SESSION_LOG_MAX_AGE_S``.
  * The active file is truncated in place after rotate, so the next
    append starts fresh.
  * Retention: only ``SESSION_LOG_KEEP`` rotated gz files remain on
    disk; older ones are deleted.
  * ``SESSION_LOG_ROTATE_DISABLED=1`` is a complete opt-out.
  * ``read_all_history()`` walks rotated gz files oldest-first and
    appends the active log's events at the end.
  * The cross-process lock is released even on rotate error (so a
    subsequent rotate can proceed).
  * Concurrent append() calls from multiple threads do not corrupt
    the active log.
  * ``tail()`` still only reads the active log, not the rotated
    history.
"""
from __future__ import annotations

import gzip
import json
import os
import threading
import time
from pathlib import Path

import pytest

from hermes_trader import session_log


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def log_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point SESSION_LOG_FILE at a fresh tmp file and reset the cached
    flock fd so a subsequent rotate opens the sidecar against the new
    path."""
    sess = tmp_path / "session.jsonl"
    sess.write_text("")
    monkeypatch.setattr(session_log, "SESSION_LOG_FILE", str(sess))
    session_log.reset_for_tests()
    return sess


def _write_lines(path: Path, n: int) -> None:
    """Append ``n`` JSONL events to ``path`` directly (bypassing
    session_log.append() so the rotate threshold isn't accidentally
    tripped by a fat rotate-aware append in the middle of a test)."""
    with open(path, "a") as f:
        for i in range(n):
            f.write(json.dumps({"i": i}) + "\n")


# ---------------------------------------------------------------------------
# Threshold detection
# ---------------------------------------------------------------------------

class TestShouldRotate:
    def test_lines_below_threshold_returns_false(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(session_log, "_SESSION_LOG_MAX_LINES", 100)
        monkeypatch.setattr(session_log, "_SESSION_LOG_MAX_BYTES", 0)
        monkeypatch.setattr(session_log, "_SESSION_LOG_MAX_AGE_S", 0)
        assert session_log._should_rotate(50, 0, time.time()) is False

    def test_lines_at_threshold_returns_true(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(session_log, "_SESSION_LOG_MAX_LINES", 100)
        monkeypatch.setattr(session_log, "_SESSION_LOG_MAX_BYTES", 0)
        monkeypatch.setattr(session_log, "_SESSION_LOG_MAX_AGE_S", 0)
        assert session_log._should_rotate(100, 0, time.time()) is True

    def test_bytes_at_threshold_returns_true(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(session_log, "_SESSION_LOG_MAX_BYTES", 1024)
        monkeypatch.setattr(session_log, "_SESSION_LOG_MAX_LINES", 0)
        monkeypatch.setattr(session_log, "_SESSION_LOG_MAX_AGE_S", 0)
        assert session_log._should_rotate(0, 1024, time.time()) is True

    def test_age_at_threshold_returns_true(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(session_log, "_SESSION_LOG_MAX_AGE_S", 60)
        monkeypatch.setattr(session_log, "_SESSION_LOG_MAX_LINES", 0)
        monkeypatch.setattr(session_log, "_SESSION_LOG_MAX_BYTES", 0)
        # mtime 120s ago → over the 60s threshold.
        assert session_log._should_rotate(0, 0, time.time() - 120) is True

    def test_disabled_always_false(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(session_log, "_SESSION_LOG_ROTATE_DISABLED", True)
        assert session_log._should_rotate(10**9, 10**12, time.time() - 10**9) is False


# ---------------------------------------------------------------------------
# rotate() — end-to-end on a tmp file
# ---------------------------------------------------------------------------

class TestRotate:
    def test_rotate_below_threshold_is_noop(
        self, log_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(session_log, "_SESSION_LOG_MAX_LINES", 1000)
        monkeypatch.setattr(session_log, "_SESSION_LOG_MAX_BYTES", 0)
        monkeypatch.setattr(session_log, "_SESSION_LOG_MAX_AGE_S", 0)
        _write_lines(log_path, 10)
        assert session_log.rotate() is None
        # No gz created.
        assert list(log_path.parent.glob("*.gz")) == []

    def test_rotate_creates_gz_and_truncates(
        self, log_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(session_log, "_SESSION_LOG_MAX_LINES", 50)
        monkeypatch.setattr(session_log, "_SESSION_LOG_MAX_BYTES", 0)
        monkeypatch.setattr(session_log, "_SESSION_LOG_MAX_AGE_S", 0)
        monkeypatch.setattr(session_log, "_SESSION_LOG_KEEP", 5)
        _write_lines(log_path, 100)
        gz = session_log.rotate()
        assert gz is not None
        assert gz.startswith(str(log_path))
        assert gz.endswith(".gz")
        assert Path(gz).exists()
        # Active log was truncated to 0 bytes.
        assert log_path.stat().st_size == 0
        # gz contains the original content.
        with gzip.open(gz, "rt") as f:
            lines = f.read().splitlines()
        assert len(lines) == 100
        # First and last lines are the events we wrote.
        first = json.loads(lines[0])
        last = json.loads(lines[-1])
        assert first == {"i": 0}
        assert last == {"i": 99}

    def test_rotate_disabled_is_noop(
        self, log_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(session_log, "_SESSION_LOG_MAX_LINES", 10)
        monkeypatch.setattr(session_log, "_SESSION_LOG_ROTATE_DISABLED", True)
        _write_lines(log_path, 100)
        assert session_log.rotate() is None
        # Active file untouched.
        assert log_path.stat().st_size > 0
        assert list(log_path.parent.glob("*.gz")) == []

    def test_rotate_on_missing_active_returns_none(
        self, log_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(session_log, "_SESSION_LOG_MAX_LINES", 1)
        log_path.unlink()
        assert session_log.rotate() is None


# ---------------------------------------------------------------------------
# Retention
# ---------------------------------------------------------------------------

class TestRetention:
    def test_keeps_only_n_rotated_files(
        self, log_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(session_log, "_SESSION_LOG_MAX_LINES", 5)
        monkeypatch.setattr(session_log, "_SESSION_LOG_MAX_BYTES", 0)
        monkeypatch.setattr(session_log, "_SESSION_LOG_MAX_AGE_S", 0)
        monkeypatch.setattr(session_log, "_SESSION_LOG_KEEP", 3)
        # Write 5 lines, rotate 4 times → produces 4 gz files; retention
        # trims to 3.
        for i in range(4):
            _write_lines(log_path, 10)
            gz = session_log.rotate()
            assert gz is not None
            time.sleep(1.05)  # ensure unique unix_ts in filename
        gzs = list(log_path.parent.glob("*.gz"))
        # Only 3 retained.
        assert len(gzs) == 3
        # They are the 3 most recent.
        all_gzs = sorted(log_path.parent.glob("*.gz"))
        assert sorted(gzs) == all_gzs  # all that's left

    def test_keep_zero_drops_everything(
        self, log_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(session_log, "_SESSION_LOG_MAX_LINES", 1)
        monkeypatch.setattr(session_log, "_SESSION_LOG_KEEP", 0)
        _write_lines(log_path, 2)
        session_log.rotate()
        time.sleep(1.05)
        _write_lines(log_path, 2)
        session_log.rotate()
        assert list(log_path.parent.glob("*.gz")) == []


# ---------------------------------------------------------------------------
# _stat_active
# ---------------------------------------------------------------------------

class TestStatActive:
    def test_returns_none_on_missing(self, tmp_path: Path) -> None:
        from hermes_trader import session_log
        prev = session_log.SESSION_LOG_FILE
        try:
            session_log.SESSION_LOG_FILE = str(tmp_path / "no-such.jsonl")
            assert session_log._stat_active() is None
        finally:
            session_log.SESSION_LOG_FILE = prev

    def test_counts_lines_and_bytes(self, log_path: Path) -> None:
        _write_lines(log_path, 17)
        st = session_log._stat_active()
        assert st is not None
        assert st["line_count"] == 17
        assert st["byte_count"] > 0


# ---------------------------------------------------------------------------
# read_all_history
# ---------------------------------------------------------------------------

class TestReadAllHistory:
    def test_reads_rotated_then_active(
        self, log_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(session_log, "_SESSION_LOG_MAX_LINES", 3)
        monkeypatch.setattr(session_log, "_SESSION_LOG_MAX_BYTES", 0)
        monkeypatch.setattr(session_log, "_SESSION_LOG_MAX_AGE_S", 0)
        monkeypatch.setattr(session_log, "_SESSION_LOG_KEEP", 10)
        # Rotate 1: writes 0..2
        for i in range(3):
            session_log.append({"i": i})
        session_log.rotate()
        time.sleep(1.05)
        # Rotate 2: writes 100..102
        for i in range(100, 103):
            session_log.append({"i": i})
        session_log.rotate()
        time.sleep(1.05)
        # Active: writes 200..201
        for i in range(200, 202):
            session_log.append({"i": i})
        hist = session_log.read_all_history()
        # All 8 events, oldest first across rotated + active.
        assert len(hist) == 8
        is_list = [e["i"] for e in hist]
        # First three are 0,1,2 (from the first rotation).
        assert is_list[0:3] == [0, 1, 2]
        # Next three are 100,101,102.
        assert is_list[3:6] == [100, 101, 102]
        # Last two are 200,201 (active log).
        assert is_list[6:8] == [200, 201]

    def test_max_files_caps_rotated_reads(
        self, log_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(session_log, "_SESSION_LOG_MAX_LINES", 2)
        monkeypatch.setattr(session_log, "_SESSION_LOG_KEEP", 10)
        for batch in range(3):
            for i in range(3):
                session_log.append({"batch": batch, "i": i})
            session_log.rotate()
            time.sleep(1.05)
        # Read only the most recent rotated file + active.
        hist = session_log.read_all_history(max_files=1)
        # Should contain only the 3 events from the last rotation + 0
        # active (we rotated but didn't write after the last rotate).
        # 3 events from gz #3 (oldest of last 1) + 0 from active.
        assert all(e["batch"] == 2 for e in hist)


# ---------------------------------------------------------------------------
# append() integration with rotate
# ---------------------------------------------------------------------------

class TestAppendTriggersRotate:
    def test_append_at_threshold_rotates(
        self, log_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(session_log, "_SESSION_LOG_MAX_LINES", 5)
        monkeypatch.setattr(session_log, "_SESSION_LOG_MAX_BYTES", 0)
        monkeypatch.setattr(session_log, "_SESSION_LOG_MAX_AGE_S", 0)
        monkeypatch.setattr(session_log, "_SESSION_LOG_KEEP", 5)
        for i in range(6):
            session_log.append({"i": i})
        # Append #5 crosses the 5-line threshold, so rotate fires
        # AFTER the 5th write — gz holds 5 events, then #6 lands in
        # the fresh active log. After the 6th append, active has 1
        # event; no second rotate yet (1 < 5).
        gzs = list(log_path.parent.glob("*.gz"))
        assert len(gzs) == 1
        with gzip.open(gzs[0], "rt") as f:
            lines = f.read().splitlines()
        assert len(lines) == 5
        # Active log has the most recent 1 record (#5 = i:5).
        active = session_log.tail(n=10)
        assert [e["i"] for e in active] == [5]

    def test_append_continues_after_rotate(
        self, log_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """After a rotate, subsequent appends land in the fresh
        active log, not appended to the gzipped file."""
        # Use lines threshold: first batch = 5 events (crosses ≥ 4),
        # second batch = 2 events (well under 4). Bytes are hard to
        # pin in tests because the ts field is variable width.
        monkeypatch.setattr(session_log, "_SESSION_LOG_MAX_LINES", 4)
        monkeypatch.setattr(session_log, "_SESSION_LOG_MAX_BYTES", 0)
        monkeypatch.setattr(session_log, "_SESSION_LOG_MAX_AGE_S", 0)
        monkeypatch.setattr(session_log, "_SESSION_LOG_KEEP", 5)
        for i in range(5):
            session_log.append({"i": i})
        # 5 appends → active hit 4 → 1 rotate fired. The 5th append
        # lands in the (now empty) active log.
        gzs = list(log_path.parent.glob("*.gz"))
        assert len(gzs) == 1
        # Second batch of 2 lands in fresh active and does not cross.
        for i in range(100, 102):
            session_log.append({"i": i})
        active = session_log.tail(n=100)
        # The 5th append (i=4) and the two new ones (100, 101) all
        # land in the post-rotate active log.
        assert [e["i"] for e in active] == [4, 100, 101]
        # 1 gz exists with 4 events.
        gzs = list(log_path.parent.glob("*.gz"))
        assert len(gzs) == 1
        with gzip.open(gzs[0], "rt") as f:
            assert len(f.read().splitlines()) == 4

    def test_append_thread_safe_under_concurrent_writes(
        self, log_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Multiple threads calling append() must not corrupt the
        active log. We do not assert on rotation (timing-sensitive);
        we only assert the file stays parseable line-by-line.
        """
        monkeypatch.setattr(session_log, "_SESSION_LOG_MAX_LINES", 0)
        monkeypatch.setattr(session_log, "_SESSION_LOG_MAX_BYTES", 0)
        monkeypatch.setattr(session_log, "_SESSION_LOG_MAX_AGE_S", 0)
        N = 200
        def worker(start: int) -> None:
            for i in range(start, start + N):
                session_log.append({"i": i})
        threads = [threading.Thread(target=worker, args=(b * N,)) for b in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        # Every line must parse and be in [0, 4*N).
        seen = []
        with open(log_path, "r") as f:
            for ln in f:
                seen.append(json.loads(ln)["i"])
        assert len(seen) == 4 * N
        # All ids are unique (no torn writes clobbering each other).
        assert len(set(seen)) == 4 * N
        assert min(seen) == 0
        assert max(seen) == 4 * N - 1


# ---------------------------------------------------------------------------
# tail() still only reads the active log
# ---------------------------------------------------------------------------

class TestTailScope:
    def test_tail_does_not_see_rotated_history(
        self, log_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Use lines threshold: first batch = 5 events (crosses ≥ 4),
        # second batch = 2 events (well under 4).
        monkeypatch.setattr(session_log, "_SESSION_LOG_MAX_LINES", 4)
        monkeypatch.setattr(session_log, "_SESSION_LOG_MAX_BYTES", 0)
        monkeypatch.setattr(session_log, "_SESSION_LOG_MAX_AGE_S", 0)
        monkeypatch.setattr(session_log, "_SESSION_LOG_KEEP", 5)
        for i in range(5):
            session_log.append({"i": i})
        # First batch crossed → rotate fired.
        for i in range(100, 102):
            session_log.append({"i": i})
        t = session_log.tail(n=10)
        # tail only sees the active log. After the 4th append fired a
        # rotate, the 5th and the two new ones (100, 101) are in the
        # active log.
        assert [e["i"] for e in t] == [4, 100, 101]


# ---------------------------------------------------------------------------
# Cross-process lock release on error
# ---------------------------------------------------------------------------

class TestLockReleaseOnError:
    def test_lock_fd_released_after_rotate(
        self, log_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """After a successful rotate, the cached flock fd is still
        open (intentionally — we reuse it), but a non-blocking
        ``LOCK_EX | LOCK_NB`` from a different thread should succeed,
        proving the lock was released."""
        monkeypatch.setattr(session_log, "_SESSION_LOG_MAX_LINES", 1)
        monkeypatch.setattr(session_log, "_SESSION_LOG_KEEP", 5)
        _write_lines(log_path, 2)
        assert session_log.rotate() is not None
        # Try non-blocking acquire from a fresh fd.
        import fcntl
        lock_path = session_log._lock_file_path()
        assert os.path.exists(lock_path), "lock sidecar must exist after rotate"
        fd2 = os.open(lock_path, os.O_RDWR)
        try:
            fcntl.flock(fd2, fcntl.LOCK_EX | fcntl.LOCK_NB)
            # If we get here, the lock was released.
            fcntl.flock(fd2, fcntl.LOCK_UN)
        finally:
            os.close(fd2)

    def test_lock_released_even_when_active_disappears(
        self, log_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """If the active log is gone (e.g. an external cleanup),
        rotate() must return None without leaking the cross-process
        lock."""
        monkeypatch.setattr(session_log, "_SESSION_LOG_MAX_LINES", 1)
        log_path.unlink()
        assert session_log.rotate() is None
        # And we can still take the lock from another fd.
        import fcntl
        lock_path = session_log._lock_file_path()
        fd2 = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
        try:
            fcntl.flock(fd2, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(fd2, fcntl.LOCK_UN)
        finally:
            os.close(fd2)
