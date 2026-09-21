# -*- coding: utf-8 -*-
"""独立 dead-man 看门狗（hermes_trader.watchdog）单元测试。"""
from __future__ import annotations

import json

import pytest

from hermes_trader.watchdog import check, latest_heartbeat_ms


def test_session_log_fresh(tmp_path):
    p = tmp_path / "session-log.jsonl"
    now_ms = 1_000_000_000_000
    with open(p, "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"event": "scan", "ts": now_ms - 10_000}) + "\n")
        fh.write(json.dumps({"event": "loop_heartbeat", "ts": now_ms}) + "\n")
    r = check(str(p), max_age_s=300, now_ms=now_ms)
    assert r.ok is True
    assert r.last_ts_ms == now_ms


def test_session_log_stale(tmp_path):
    p = tmp_path / "session-log.jsonl"
    now_ms = 1_000_000_000_000
    with open(p, "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"event": "loop_heartbeat",
                             "ts": now_ms - 400_000}) + "\n")  # 400s 前
    r = check(str(p), max_age_s=300, now_ms=now_ms)
    assert r.ok is False
    assert r.age_s == pytest.approx(400.0)
    assert "过期" in r.reason


def test_digit_heartbeat_file(tmp_path):
    p = tmp_path / "beat.txt"
    now_ms = 1_000_000_000_000
    p.write_text(str(now_ms - 5_000), encoding="utf-8")
    ts, source = latest_heartbeat_ms(str(p))
    assert ts == now_ms - 5_000
    assert source == "heartbeat-file"


def test_mtime_fallback(tmp_path):
    p = tmp_path / "beat.txt"
    p.write_text("not json and not digit", encoding="utf-8")
    ts, source = latest_heartbeat_ms(str(p))
    assert ts > 0
    assert source == "mtime"
    # 用真实当前时间检查：刚写入应判新鲜
    r = check(str(p), max_age_s=300)
    assert r.ok is True


def test_missing_heartbeat_source(tmp_path):
    with pytest.raises(FileNotFoundError):
        latest_heartbeat_ms(str(tmp_path / "nope.jsonl"))


def test_future_timestamp_is_clock_skew(tmp_path):
    p = tmp_path / "session-log.jsonl"
    now_ms = 1_000_000_000_000
    with open(p, "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"event": "loop_heartbeat",
                             "ts": now_ms + 60_000}) + "\n")
    r = check(str(p), max_age_s=300, now_ms=now_ms)
    assert r.ok is False
    assert "时钟漂移" in r.reason


def test_ignores_non_loop_events(tmp_path):
    p = tmp_path / "session-log.jsonl"
    now_ms = 1_000_000_000_000
    # 只有非循环事件，且内容非数字 → 退回 mtime（不报错）
    with open(p, "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"event": "something_else",
                             "ts": now_ms - 999_999}) + "\n")
    # 非循环事件退回真实 mtime；用真实当前时间（不注入过去的 now_ms）
    r = check(str(p), max_age_s=300)
    assert r.ok is True
