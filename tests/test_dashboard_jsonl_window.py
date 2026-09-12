"""Bounded in-memory window for the incremental JSONL reader.

The dashboard parses two append-only logs (session-log / events.jsonl) into a
cache that grows on every poll. These tests pin the row-window behaviour:

  * rows past the window fall out of the head, newest rows survive;
  * trimming the in-memory head never rewinds the byte offset (rows that left
    the window are not re-parsed on the next poll);
  * rotation (inode change) resets to a full bounded read;
  * a partially-flushed last line keeps the old offset and completes on the
    next call.
"""

import json
import threading
from pathlib import Path

from hermes_trader import dashboard


def _new_cache() -> dict:
    return {"lines": [], "inode": None, "size": -1, "offset": 0}


def _write_rows(path: Path, start: int, count: int) -> None:
    with path.open("a") as f:
        for i in range(start, start + count):
            f.write(json.dumps({"i": i}) + "\n")


def test_window_keeps_newest_rows(tmp_path: Path) -> None:
    path = tmp_path / "log.jsonl"
    _write_rows(path, 0, 10)
    cache, lock = _new_cache(), threading.Lock()

    rows = dashboard._read_jsonl_incremental(path, cache, lock, max_lines=4)

    assert [r["i"] for r in rows] == [6, 7, 8, 9]
    assert len(cache["lines"]) == 4
    # Offset still covers the whole file even though the head was trimmed.
    assert cache["offset"] == path.stat().st_size


def test_trimmed_head_not_reparsed_on_append(tmp_path: Path) -> None:
    path = tmp_path / "log.jsonl"
    cache, lock = _new_cache(), threading.Lock()

    _write_rows(path, 0, 10)
    dashboard._read_jsonl_incremental(path, cache, lock, max_lines=4)
    _write_rows(path, 10, 2)
    rows = dashboard._read_jsonl_incremental(path, cache, lock, max_lines=4)

    # Only the newly appended rows are parsed; trimmed head rows never return.
    assert [r["i"] for r in rows] == [8, 9, 10, 11]


def test_rotation_resets_to_bounded_full_read(tmp_path: Path) -> None:
    path = tmp_path / "log.jsonl"
    cache, lock = _new_cache(), threading.Lock()

    _write_rows(path, 0, 10)
    dashboard._read_jsonl_incremental(path, cache, lock, max_lines=100)

    # Simulate rotation: a brand-new file reuses the path (new inode).
    path.unlink()
    _write_rows(path, 100, 5)
    rows = dashboard._read_jsonl_incremental(path, cache, lock, max_lines=3)

    assert [r["i"] for r in rows] == [102, 103, 104]


def test_partial_last_line_re_read_on_next_call(tmp_path: Path) -> None:
    path = tmp_path / "log.jsonl"
    cache, lock = _new_cache(), threading.Lock()

    _write_rows(path, 0, 2)
    with path.open("a") as f:
        f.write('{"i": 2')  # no newline / incomplete JSON
    rows = dashboard._read_jsonl_incremental(path, cache, lock, max_lines=100)
    assert [r["i"] for r in rows] == [0, 1]

    # Complete the partially-flushed line.
    with path.open("a") as f:
        f.write("}\n")
    rows = dashboard._read_jsonl_incremental(path, cache, lock, max_lines=100)
    assert [r["i"] for r in rows] == [0, 1, 2]


def test_missing_file_empties_cache(tmp_path: Path) -> None:
    path = tmp_path / "absent.jsonl"
    cache, lock = _new_cache(), threading.Lock()
    assert dashboard._read_jsonl_incremental(path, cache, lock, max_lines=10) == []
    assert cache["inode"] is None
