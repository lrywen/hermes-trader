"""跨 session-log 轮转的成交历史读取测试。

背景：session-log 按 24h/50MB 轮转成 .gz 后，真实成交会落到归档里，仅读当前
活跃 log 会让 /portal/trades 的开平仓历史"消失"。这里用临时目录构造一个活跃
log + 两个轮转归档（旧格式 executed 在顶层），验证：
  - 归档与活跃 log 中的真实成交都被读出，按时间顺序（旧→新）；
  - SHADOW 未实跑的 execute（executed 缺失/false）被过滤；
  - 非成交事件（心跳/scan/ta_skip）不进入结果。
"""
from __future__ import annotations

import gzip
import json

from hermes_trader import dashboard, session_log


def _exe(ts: int, coin: str, executed: bool | None) -> dict:
    # 旧归档格式：executed 为顶层字段。
    return {
        "ts": ts,
        "event": "execute",
        "coin": coin,
        "side": "long",
        "executed": executed,
        "size_usd": 30.0,
        "entry_px": 1.0,
    }


def _write_gz(path, records) -> None:
    with gzip.open(path, "wt", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")


def test_trade_history_spans_rotated_archives(monkeypatch, tmp_path):
    active = tmp_path / "session-log.jsonl"
    gz_old = tmp_path / "session-log.jsonl.100.gz"
    gz_new = tmp_path / "session-log.jsonl.200.gz"

    # 旧归档：1 笔真实成交 + 1 笔噪声事件。
    _write_gz(gz_old, [
        _exe(100, "AAA", True),
        {"ts": 101, "event": "loop_heartbeat"},
    ])
    # 较新归档：1 笔真实成交 + 1 笔 executed=false（非真实成交）。
    _write_gz(gz_new, [
        _exe(200, "BBB", True),
        _exe(201, "CCC", False),
    ])
    # 活跃 log：新格式 executed 嵌套在 payload 下 + 海量 ta_skip。
    with active.open("w", encoding="utf-8") as f:
        f.write(json.dumps({"ts": 300, "event": "ta_skip"}) + "\n")
        f.write(json.dumps({
            "ts": 301, "event": "execute", "coin": "DDD",
            "payload": {"executed": True},
        }) + "\n")

    # 让 dashboard 与 session_log 都指向临时活跃 log；归档 glob 与该文件同目录。
    monkeypatch.setattr(session_log, "SESSION_LOG_FILE", str(active))
    monkeypatch.setattr(dashboard, "_LOG_PATH", active)
    # 归档缓存若被其它用例污染会导致跳过解析，这里强制复位。
    dashboard._ARCHIVE_CACHE.update(sig=None, lines=[])

    lines = dashboard._read_trade_log_lines()
    # 归档侧只含真实成交 execute；活跃 log 完整保留（含 ta_skip 噪声与 DDD 成交）。
    exe_coins = [r.get("coin") for r in lines if r["event"] == "execute"]
    assert exe_coins == ["AAA", "BBB", "DDD"]  # 旧→新，CCC(false) 在归档侧被过滤
    assert any(r["event"] == "ta_skip" for r in lines)  # 活跃 log 未被裁剪


def test_trade_history_no_archives_returns_active_only(monkeypatch, tmp_path):
    active = tmp_path / "session-log.jsonl"
    with active.open("w", encoding="utf-8") as f:
        f.write(json.dumps(_exe(100, "AAA", True)) + "\n")
        f.write(json.dumps({"ts": 101, "event": "scan"}) + "\n")

    monkeypatch.setattr(session_log, "SESSION_LOG_FILE", str(active))
    monkeypatch.setattr(dashboard, "_LOG_PATH", active)
    dashboard._ARCHIVE_CACHE.update(sig=None, lines=[])

    lines = dashboard._read_trade_log_lines()
    exe_coins = [r.get("coin") for r in lines if r["event"] == "execute"]
    assert exe_coins == ["AAA"]
    assert any(r["event"] == "scan" for r in lines)  # 活跃 log 完整未裁剪
