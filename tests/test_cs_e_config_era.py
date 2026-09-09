"""CS-E (2026-09-09): config-era 指纹与事件 fork 白名单。

覆盖：
1. era hash 稳定（canonical JSON）、长度 12；只对 ERA_TRACKED_PATHS 内阈值
   敏感，无关键（mode 等）改动不换 era；子树任一叶变化换 era。
2. compute_config_era 纯读、异常返回 era_id=None 而不抛出。
3. config_write / config_snapshot / config_rollback 在 fork 白名单，经
   fork_from_session 进入 events.jsonl 且审计字段在 payload 顶层。

config_write 审计（old/new/via）测试见 test_cs_a_config_write_audit.py，
随 CS-A 的 via= 参数一同落地。
"""

from __future__ import annotations

import json

from hermes_trader import event_log, session_log
from hermes_trader.agents import config_store as cs
from hermes_trader.agents.config_store import (
    _era_id_from_subset,
    _extract_tracked_subset,
    compute_config_era,
)

# ── 1. era 指纹 ────────────────────────────────────────────────────────────


def test_era_id_stable_length_and_canonical_ordering():
    a = {"leverage": 10, "min_ai_confidence": 0.6}
    b = {"min_ai_confidence": 0.6, "leverage": 10}  # 键序不同
    id_a = _era_id_from_subset(_extract_tracked_subset(a))
    id_b = _era_id_from_subset(_extract_tracked_subset(b))
    assert id_a == id_b
    assert len(id_a) == 12
    int(id_a, 16)  # hex


def test_era_only_sensitive_to_tracked_keys():
    base = {"leverage": 10, "mode": "PAPER", "min_ai_confidence": 0.6}
    # 无关键改动（mode / 日志路径）不换 era。
    noop = dict(base, mode="LIVE", log_path="/tmp/other.log")
    assert compute_config_era(base)["era_id"] == compute_config_era(noop)["era_id"]
    # 跟踪的标量阈值改动换 era。
    changed = dict(base, leverage=20)
    assert compute_config_era(base)["era_id"] != compute_config_era(changed)["era_id"]


def test_era_subtree_wildcard_sensitive():
    base = {"market_circuit": {"daily_loss_limit": 0.05}}
    changed = {"market_circuit": {"daily_loss_limit": 0.03}}
    assert compute_config_era(base)["era_id"] != compute_config_era(changed)["era_id"]
    # 缺失整条子树不报错（跳过而非记 None）。
    missing = compute_config_era({"leverage": 10})
    assert missing["era_id"] and len(missing["era_id"]) == 12
    assert "market_circuit" not in missing["tracked"]


def test_compute_config_era_never_raises(monkeypatch):
    # 记账埋点在下单热路径上，任何异常都必须被吞掉。
    monkeypatch.setattr(cs, "_extract_tracked_subset", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    out = compute_config_era({"leverage": 10})
    assert out["era_id"] is None
    assert out["tracked"] == {}


def _read_jsonl(path):
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


# ── 3. fork 白名单 ──────────────────────────────────────────────────────────


def test_config_events_fork_to_events_jsonl(monkeypatch, tmp_path):
    for name in ("config_write", "config_snapshot", "config_rollback"):
        assert name in event_log._FORKABLE_EVENTS

    sess_file = tmp_path / "session.jsonl"
    ev_file = tmp_path / "events.jsonl"
    monkeypatch.setattr(session_log, "SESSION_LOG_FILE", str(sess_file))
    monkeypatch.setattr(event_log, "EVENTS_FILE", str(ev_file))

    session_log.append(
        {
            "event": "config_write",
            "via": "web",
            "changed_keys": ["leverage"],
            "old": {"leverage": 10},
            "new": {"leverage": 20},
            "prev_era_id": "aaaa1111bbbb",
            "era_id": "cccc2222dddd",
        }
    )
    session_log.append({"event": "config_snapshot", "snapshot_id": 1700000000})
    session_log.append({"event": "config_rollback", "target": "snapshot"})

    rows = _read_jsonl(ev_file)
    names = [r["event"] for r in rows]
    assert names == ["config_write", "config_snapshot", "config_rollback"]
    # fork 后审计字段位于 payload 顶层（离线脚本的读取契约）。
    p = rows[0]["payload"]
    assert p["new"] == {"leverage": 20}
    assert p["old"] == {"leverage": 10}
    assert p["era_id"] == "cccc2222dddd"
    assert p["prev_era_id"] == "aaaa1111bbbb"
