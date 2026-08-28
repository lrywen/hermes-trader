"""Append-only JSONL activity log — the trading system's visible heartbeat.

The trading loop and the FastAPI server append events here; `status.py` and the
hourly cron report read them back. One line per event, each tagged with a `ts`
(epoch ms). Path is overridable via the `SESSION_LOG_PATH` env var.
"""
from __future__ import annotations

import json
import os
import time
from typing import Any, Dict, List

SESSION_LOG_FILE = os.environ.get(
    "SESSION_LOG_PATH",
    os.path.expanduser("~/.hermes-trader-session-log.jsonl"),
)


def append(event: Dict[str, Any]) -> None:
    """Append one event as a JSONL line. A `ts` field is added automatically.

    Best-effort: a logging failure must never interrupt trading, so disk errors
    are swallowed.
    """
    record = {"ts": int(time.time() * 1000), **event}
    try:
        with open(SESSION_LOG_FILE, "a") as f:
            f.write(json.dumps(record) + "\n")
    except OSError:
        pass
    # Fork outcome-relevant events (execute/exit/risk) into the authoritative
    # events.jsonl so the post-trade event log has a signal→order→close trace.
    # The high-volume heartbeat (scan/heartbeat/research/ta_skip) stays here
    # only. This bridges the pre-2026-08-22 architecture split where order/
    # close events never reached events.jsonl and memory rebuild found 0 orders.
    try:
        from hermes_trader import event_log
        event_log.fork_from_session(record)
    except Exception:
        pass
    # Best-effort Feishu notification dispatch. Imported lazily to avoid a
    # circular import (notify_dispatch may import modules that log events at
    # import time). Dispatched AFTER the disk write so a notification failure
    # can never lose the durable record.
    try:
        from hermes_trader import notify_dispatch
        notify_dispatch.dispatch(record)
    except Exception:
        pass


def tail(n: int = 10) -> List[Dict[str, Any]]:
    """Return the last `n` parseable events, oldest first.

    Reads backward from the end of the file in chunks instead of loading the
    whole log into memory (the log can grow to several MB over a session).
    """
    try:
        with open(SESSION_LOG_FILE, "rb") as f:
            f.seek(0, 2)  # end
            block = 8192
            data = b""
            while data.count(b"\n") <= n and f.tell() > 0:
                step = min(block, f.tell())
                f.seek(-step, 1)
                data = f.read(step) + data
                f.seek(-step, 1)
        lines = [ln for ln in data.splitlines() if ln.strip()]
    except (FileNotFoundError, OSError):
        return []
    out: List[Dict[str, Any]] = []
    for ln in lines[-n:]:
        try:
            out.append(json.loads(ln))
        except (json.JSONDecodeError, UnicodeDecodeError):
            pass
    return out
