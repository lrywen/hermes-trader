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


def test_keep_event_filters_rows_before_window(tmp_path: Path) -> None:
    """Regression: events.jsonl is dominated by execute/signal rows. A plain
    tail-N window trimmed away old sparse `close` rows, shrinking the trades
    timeline. keep_event must drop the high-frequency rows at parse time so the
    retained sparse events never count against the window and full history
    survives even when max_lines is tiny."""
    path = tmp_path / "events.jsonl"
    with path.open("a") as f:
        # An old close, then a wall of high-frequency events, then a new close.
        f.write(json.dumps({"event": "close", "i": 0}) + "\n")
        for i in range(1, 51):
            f.write(json.dumps({"event": "execute", "i": i}) + "\n")
        f.write(json.dumps({"event": "dsl_exit", "i": 51}) + "\n")

    cache, lock = _new_cache(), threading.Lock()
    keep = lambda r: r.get("event") in ("close", "dsl_exit")

    rows = dashboard._read_jsonl_incremental(
        path, cache, lock, max_lines=4, keep_event=keep,
    )

    # Both sparse rows survive despite max_lines=4 and 50 intervening executes;
    # no execute row ever enters the cache.
    assert [(r["event"], r["i"]) for r in rows] == [("close", 0), ("dsl_exit", 51)]
    assert cache["offset"] == path.stat().st_size


def test_keep_event_filters_incremental_appends(tmp_path: Path) -> None:
    """Filtered rows on later appends stay out, and previously retained rows
    remain until the window trims among the kept types only."""
    path = tmp_path / "events.jsonl"
    cache, lock = _new_cache(), threading.Lock()
    keep = lambda r: r.get("event") == "close"

    with path.open("a") as f:
        f.write(json.dumps({"event": "close", "i": 0}) + "\n")
        for i in range(1, 20):
            f.write(json.dumps({"event": "signal", "i": i}) + "\n")
    dashboard._read_jsonl_incremental(path, cache, lock, max_lines=10, keep_event=keep)

    with path.open("a") as f:
        f.write(json.dumps({"event": "close", "i": 20}) + "\n")
    rows = dashboard._read_jsonl_incremental(path, cache, lock, max_lines=10, keep_event=keep)

    assert [r["i"] for r in rows] == [0, 20]


# ── outcome execute (long-lived open legs) whitelist + pairing ────────────

def test_keep_outcome_event_retains_only_filled_executes() -> None:
    """closes/dsl_exit always survive; execute survives only when its nested
    payload really filled, so the thousands of unfilled attempts stay out."""
    keep = dashboard._keep_outcome_event
    assert keep({"event": "close", "payload": {}})
    assert keep({"event": "dsl_exit", "payload": {}})
    assert keep({"event": "execute", "payload": {"executed": True}})
    assert not keep({"event": "execute", "payload": {"executed": False}})
    assert not keep({"event": "execute", "payload": {}})
    assert not keep({"event": "signal", "payload": {}})


def test_open_row_from_outcome_flattens_payload_and_iso_ts() -> None:
    rec = {
        "event": "execute",
        "timestamp": "2026-08-23T09:11:07Z",
        "payload": {
            "coin": "ETHFI", "side": "long", "executed": True,
            "entry_px": 0.60294, "size_usd": 11.63, "stop_px": 0.58,
            "tp_px": 0.66, "detail": "oid-1",
        },
    }
    row = dashboard._open_row_from_outcome(rec)
    assert row is not None
    assert row["kind"] == "open"
    assert row["coin"] == "ETHFI"
    assert row["entry_px"] == 0.60294
    assert row["notional_usd"] == 11.63
    # ISO timestamp converted to epoch ms.
    assert row["ts"] == dashboard._iso_to_ms("2026-08-23T09:11:07Z")


def test_open_row_from_outcome_skips_unfilled() -> None:
    rec = {"event": "execute", "timestamp": "2026-08-23T09:11:07Z",
           "payload": {"coin": "X", "executed": False}}
    assert dashboard._open_row_from_outcome(rec) is None


def _close_row(ts: int, coin: str = "ETHFI", side: str = "long") -> dict:
    return {"kind": "close", "ts": ts, "coin": coin, "side": side,
            "source": "reconcile", "pair_id": None}


def test_pairing_uses_outcome_open_when_session_log_rotated() -> None:
    """An old close has no session-log execute (rotated away) but the
    long-lived events.jsonl execute still pairs with it."""
    open_ts = dashboard._iso_to_ms("2026-08-23T09:11:07Z")
    close_ts = open_ts + 300_000
    outcome = [{
        "event": "execute",
        "timestamp": "2026-08-23T09:11:07Z",
        "payload": {"coin": "ETHFI", "side": "long", "executed": True,
                    "entry_px": 0.6, "size_usd": 10.0},
    }]
    timeline = dashboard._pair_opens_and_closes(
        [_close_row(close_ts)], events=[], outcome_records=outcome,
    )
    kinds = {r.get("kind") for r in timeline}
    assert kinds == {"open", "close"}
    close = next(r for r in timeline if r["kind"] == "close")
    open_row = next(r for r in timeline if r["kind"] == "open")
    assert close["pair_id"] is not None
    assert close["open_ts"] == open_ts
    assert open_row["pair_id"] == close["pair_id"]
    assert open_row["close_ts"] == close_ts


def test_pairing_dedups_open_present_in_both_sources() -> None:
    """A recent open exists in both the session log (ms ts) and events.jsonl
    (whole-second ts). It must render once, not twice."""
    sec_ms = dashboard._iso_to_ms("2026-09-11T17:16:06Z")
    session_ts = sec_ms + 800  # session log keeps sub-second precision
    close_ts = session_ts + 2_000_000
    session_event = {"event": "execute", "executed": True, "ts": session_ts,
                     "coin": "ETHFI", "side": "long", "entry_px": 0.71,
                     "size_usd": 30.0}
    outcome = [{
        "event": "execute",
        "timestamp": "2026-09-11T17:16:06Z",
        "payload": {"coin": "ETHFI", "side": "long", "executed": True,
                    "entry_px": 0.71, "size_usd": 30.0},
    }]
    timeline = dashboard._pair_opens_and_closes(
        [_close_row(close_ts)], events=[session_event], outcome_records=outcome,
    )
    opens = [r for r in timeline if r["kind"] == "open"]
    assert len(opens) == 1
    # Session-log twin (ms precision) wins over the whole-second outcome row.
    assert opens[0]["ts"] == session_ts
