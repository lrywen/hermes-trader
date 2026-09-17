"""Tests for the P0 risk-tuning shadow-arm panel (Audit 2026-09-12, M-呈现).

Covers dashboard_routes/risk_tuning.py:
  * GET /api/dashboard/risk-tuning/arms — three arms with correct filtering
    (rule= for risk_tuning_shadow.jsonl, layer= for ta_late_entry_shadow.jsonl),
    counts / last_7d / daily buckets / recent-N (newest first) / outcome+pnl
    stats from reconcile backfill,
  * mode resolution mirroring the runtime dual-boolean semantics
    (enabled/shadow_mode → off/shadow/enforce; missing block → canonical
    default shadow),
  * 60s TTL cache (second read served from cache),
  * anonymous-safe read posture (no operator token required).
"""
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hermes_trader.dashboard import register_routes

_OP_TOKEN = "test-op-secret-rt"


def _ts(days_ago: float = 0.0) -> str:
    """动态 ISO 时间戳（相对当前），避免 7 天窗口断言随真实日期漂移。"""
    import time as _time
    from datetime import datetime, timezone
    return datetime.fromtimestamp(_time.time() - days_ago * 86400.0,
                                  tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _write_jsonl(path, rows):
    with open(path, "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")


@pytest.fixture()
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_OPERATOR_TOKEN", _OP_TOKEN)
    risk_file = tmp_path / "risk_tuning_shadow.jsonl"
    late_file = tmp_path / "ta_late_entry_shadow.jsonl"
    monkeypatch.setenv("HERMES_RISK_TUNING_SHADOW_FILE", str(risk_file))
    monkeypatch.setenv("HERMES_TA_LATE_ENTRY_SHADOW_FILE", str(late_file))
    _write_jsonl(risk_file, [
        {"timestamp": _ts(0.1), "rule": "sigma_burst_gate",
         "would": "surface", "coin": "SUI", "side": "",
         "detail": {"score": 50.38, "enforced": False}},
        {"timestamp": _ts(0.05), "rule": "sigma_burst_gate",
         "would": "surface", "coin": "ETH", "side": "",
         "detail": {"score": 49.0, "enforced": False}},
        {"timestamp": _ts(0.04), "rule": "research_cooldown_adaptive",
         "would": "re_research", "coin": "SUI",
         "detail": {"elapsed_min": 3.2, "enforced": False}},
        # 其它 rule（前序臂）不得混入三臂统计
        {"timestamp": _ts(0.03), "rule": "confidence_decay",
         "would": "shrink", "coin": "DOGE", "detail": {}},
        {"timestamp": _ts(10), "rule": "sigma_burst_gate",
         "would": "surface", "coin": "BTC", "side": "",
         "detail": {"score": 51.0, "enforced": False}},
    ])
    _write_jsonl(late_file, [
        {"timestamp": _ts(1), "coin": "FIL", "side": "long",
         "layer": "prefilter_breakout_exemption", "blocked": True,
         "enforced": False, "would": "downgrade_reject", "breakout_rvol": 4.14,
         "outcome": "loss", "pnl_pct": -3.09},
        {"timestamp": _ts(0.2), "coin": "ETH", "side": "long",
         "layer": "prefilter_breakout_exemption", "blocked": True,
         "enforced": False, "would": "downgrade_reject", "breakout_rvol": 5.2,
         "outcome": "win", "pnl_pct": 2.4},
        # 其它 layer（常规 late-entry 记录）不得混入
        {"timestamp": _ts(0.1), "coin": "SOL", "side": "long",
         "layer": "executor_late_entry", "blocked": True, "outcome": "win"},
    ])
    from hermes_trader import dashboard
    dashboard._TTL_CACHE.clear()
    app = FastAPI()
    register_routes(app)
    return TestClient(app, raise_server_exceptions=False)


def _arms_by_id(data):
    return {a["id"]: a for a in data["arms"]}


def test_arms_endpoint_three_arms_filtered(client):
    r = client.get("/api/dashboard/risk-tuning/arms")
    assert r.status_code == 200
    data = r.json()
    arms = _arms_by_id(data)
    assert set(arms) == {"sigma_burst_gate", "research_cooldown_adaptive",
                         "breakout_exemption"}
    assert data["cache_ttl_s"] == 60.0

    sb = arms["sigma_burst_gate"]
    assert sb["total"] == 3  # confidence_decay 行不计入
    assert sb["mode"] == "shadow"  # 配置缺失 → canonical 默认 shadow
    assert sb["enforced"] == 0

    rca = arms["research_cooldown_adaptive"]
    assert rca["total"] == 1

    be = arms["breakout_exemption"]
    assert be["total"] == 2  # executor_late_entry 行不计入
    assert be["outcomes"] == {"win": 1, "loss": 1, "open": 0, "pending": 0}
    assert be["pnl"]["n"] == 2
    assert be["pnl"]["win_rate"] == 0.5
    assert be["pnl"]["min_pct"] == -3.09


def test_arms_recent_newest_first_and_cap(client):
    r = client.get("/api/dashboard/risk-tuning/arms?recent=1")
    assert r.status_code == 200
    arms = _arms_by_id(r.json())
    recent = arms["sigma_burst_gate"]["recent"]
    assert len(recent) == 1
    # 文件末行（最新追加，BTC 记录）在前
    assert recent[0]["coin"] == "BTC"


def test_arms_last_7d_excludes_old_records(client):
    r = client.get("/api/dashboard/risk-tuning/arms")
    arms = _arms_by_id(r.json())
    sb = arms["sigma_burst_gate"]
    # 09-01 的记录超出 7 天窗口；total 含它，last_7d 不含
    assert sb["total"] == 3
    assert sb["last_7d"] == 2


def test_arms_mode_resolution(client, monkeypatch):
    from hermes_trader.dashboard_routes import risk_tuning
    spec_sb = risk_tuning._ARMS[0]
    spec_be = risk_tuning._ARMS[2]
    # 未配置 → canonical 默认 shadow
    assert risk_tuning._arm_mode({}, spec_sb) == "shadow"
    # disabled → off
    assert risk_tuning._arm_mode({"sigma_burst_gate": {"enabled": False}}, spec_sb) == "off"
    # enabled + 非 shadow → enforce
    assert risk_tuning._arm_mode(
        {"sigma_burst_gate": {"enabled": True, "shadow_mode": False}}, spec_sb) == "enforce"
    # 嵌套块（ta_late_entry.breakout_exemption）
    assert risk_tuning._arm_mode(
        {"ta_late_entry": {"breakout_exemption": {"enabled": True, "shadow_mode": False}}},
        spec_be) == "enforce"
    assert risk_tuning._arm_mode({"ta_late_entry": {}}, spec_be) == "shadow"


def test_arms_endpoint_cached(client):
    r1 = client.get("/api/dashboard/risk-tuning/arms")
    assert r1.status_code == 200
    # TTL 内追加一行，第二次读必须命中缓存（计数不变）
    import os
    with open(os.environ["HERMES_RISK_TUNING_SHADOW_FILE"], "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"timestamp": "2026-09-15T13:00:00Z",
                             "rule": "sigma_burst_gate", "coin": "X"}) + "\n")
    r2 = client.get("/api/dashboard/risk-tuning/arms")
    assert r2.status_code == 200
    assert _arms_by_id(r2.json())["sigma_burst_gate"]["total"] == 3


def test_arms_enforced_count_reads_nested_detail_flag(client):
    """enforced 标志两臂位置不一：sigma/cooldown 在 detail 内层、breakout 在
    顶层，两种写法都必须计入 enforced 统计（翻牌后观测依赖此计数）。"""
    import os
    with open(os.environ["HERMES_RISK_TUNING_SHADOW_FILE"], "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"timestamp": _ts(0.01), "rule": "sigma_burst_gate",
                             "would": "surface", "coin": "SOL",
                             "detail": {"score": 52.0, "enforced": True}}) + "\n")
    with open(os.environ["HERMES_TA_LATE_ENTRY_SHADOW_FILE"], "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"timestamp": _ts(0.01), "coin": "ARB", "side": "long",
                             "layer": "prefilter_breakout_exemption",
                             "enforced": True, "would": "downgrade_reject"}) + "\n")
    from hermes_trader import dashboard
    dashboard._TTL_CACHE.clear()
    r = client.get("/api/dashboard/risk-tuning/arms")
    assert r.status_code == 200
    arms = _arms_by_id(r.json())
    assert arms["sigma_burst_gate"]["enforced"] == 1  # detail.enforced=True 计入
    assert arms["breakout_exemption"]["enforced"] == 1  # 顶层 enforced=True 计入
    assert arms["research_cooldown_adaptive"]["enforced"] == 0


def test_arms_endpoint_missing_files_ok(client, monkeypatch, tmp_path):
    # 文件不存在（臂未触发过）也要 200，计数为 0
    monkeypatch.setenv("HERMES_RISK_TUNING_SHADOW_FILE",
                       str(tmp_path / "nope1.jsonl"))
    monkeypatch.setenv("HERMES_TA_LATE_ENTRY_SHADOW_FILE",
                       str(tmp_path / "nope2.jsonl"))
    from hermes_trader import dashboard
    dashboard._TTL_CACHE.clear()
    r = client.get("/api/dashboard/risk-tuning/arms")
    assert r.status_code == 200
    arms = _arms_by_id(r.json())
    assert arms["sigma_burst_gate"]["total"] == 0
    assert arms["sigma_burst_gate"]["file_exists"] is False
